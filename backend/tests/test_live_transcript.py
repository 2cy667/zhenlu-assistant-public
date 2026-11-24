from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.errors import ApiError
from app.models import AdvisorState
from app.providers.base import AsrSegment
from app.routers.live_assistant import live_events
from app.services.live_transcript import store_asr_sentence
from tests.test_fun_asr import FakeConnection, failed_event, finished_event, provider_for, result_event, wav_bytes


def new_stream(client):
    sid = client.post("/api/sessions", json={"patient_name": "虚构实时问诊"}).json()["id"]
    opened = client.post(f"/api/sessions/{sid}/transcript/stream")
    assert opened.status_code == 200, opened.text
    assert opened.json()["revision"] == 0
    return sid


def append(client, sid, *, revision=0, seq=0, text="患者：咳嗽三天。", request_id=None):
    return client.post(f"/api/sessions/{sid}/transcript/stream/segments", json={
        "request_id": request_id or f"batch-{seq}", "expected_revision": revision,
        "segments": [{"seq": seq, "text": text, "speaker": "未知"}],
    })


def test_stream_replay_order_revision_and_completion(client):
    sid = new_stream(client)
    base = f"/api/sessions/{sid}"
    assert client.post(base + "/transcript/stream").json()["revision"] == 0
    assert client.post(base + "/transcript/stream/complete", json={"expected_revision": 0}).status_code == 409
    first = append(client, sid).json()
    assert first["state"] == "streaming" and first["revision"] == 1
    assert client.get(base + "/transcript").json() == first
    assert append(client, sid).json() == first
    assert append(client, sid, text="修改旧内容").json()["code"] == "idempotency_conflict"
    assert append(client, sid, seq=1).json()["code"] == "transcript_revision_conflict"
    assert append(client, sid, revision=1, seq=3).json()["code"] == "segment_sequence_conflict"
    assert client.post(base + "/draft").json()["code"] == "transcript_not_final"
    assert client.post(base + "/jobs", json={"operation": "structure"}).json()["code"] == "transcript_not_final"
    final = client.post(base + "/transcript/stream/complete", json={"expected_revision": 1}).json()
    assert final["state"] == "final" and final["revision"] == 2
    assert final["segments"] == first["segments"]
    assert client.post(base + "/transcript/stream/complete", json={"expected_revision": 1}).json() == final
    assert append(client, sid).json() == final
    assert append(client, sid, revision=2, seq=1).json()["code"] == "transcript_closed"
    assert client.post(base + "/draft").status_code == 200


@pytest.mark.parametrize("changes", [
    {"text": " "}, {"seq": True}, {"seq": -1}, {"speaker": ""},
    {"start_ms": 10}, {"start_ms": 20, "end_ms": 10}, {"is_final": False},
])
def test_live_input_rejects_malformed_segments(client, changes):
    sid = new_stream(client)
    response = client.post(f"/api/sessions/{sid}/transcript/stream/segments", json={
        "request_id": "batch", "expected_revision": 0,
        "segments": [{"seq": 0, "text": "患者：咳嗽。", **changes}],
    })
    assert response.status_code == 422
    assert client.get(f"/api/sessions/{sid}/transcript").json()["segments"] == []


def test_concurrent_batch_replay_is_idempotent(client):
    sid = new_stream(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: append(client, sid), range(2)))
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json()
    assert len(results[0].json()["segments"]) == 1


def test_external_stream_and_pending_advice_survive_restart(settings):
    settings.job_worker_enabled = False
    with TestClient(create_app(settings)) as client:
        sid = new_stream(client)
        first = append(client, sid).json()
    with TestClient(create_app(settings)) as client:
        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["status"] == "transcribing"
        assert detail["transcript"] == first
        assert append(client, sid).json() == first
        response = append(client, sid, revision=1, seq=1, text="医生：有没有发热？")
        assert response.status_code == 200
        with client.app.state.db.session_factory() as db:
            state = db.get(AdvisorState, sid)
            assert state.pending_since and state.target_revision == 2


def test_sse_initial_and_reconnect_snapshots(client):
    sid = new_stream(client)

    class Request:
        app = client.app

        async def is_disconnected(self):
            return False

    async def snapshot():
        response = await live_events(sid, Request())
        assert response.media_type == "text/event-stream"
        assert response.headers["Cache-Control"] == "no-store"
        iterator = response.body_iterator
        try:
            event = await anext(iterator)
            assert event.startswith("event: snapshot\ndata: ")
            return json.loads(event.split("data: ", 1)[1])
        finally:
            await iterator.aclose()

    assert asyncio.run(snapshot())["transcript"]["revision"] == 0
    append(client, sid)
    assert asyncio.run(snapshot())["transcript"]["revision"] == 1
    assert client.get("/api/sessions/missing/live/events").status_code == 404


def test_partial_asr_failure_preserves_sources_and_can_retry(client):
    sid = client.post("/api/sessions", json={"patient_name": "虚构音频病例"}).json()["id"]
    base = f"/api/sessions/{sid}"
    client.post(base + "/audio", files={"file": ("demo.wav", wav_bytes(), "audio/wav")})
    first_event = result_event("患者：咳嗽三天。")
    client.app.state.asr_provider = provider_for(FakeConnection([first_event, failed_event()]))
    assert client.post(base + "/transcript").status_code == 502
    partial = client.get(base + "/transcript").json()
    assert partial["state"] == "interrupted" and len(partial["segments"]) == 1
    assert client.post(base + "/draft").json()["code"] == "transcript_not_final"
    client.app.state.asr_provider = provider_for(FakeConnection([
        first_event, result_event("患者：没有发热。", sentence_id=2), finished_event(),
    ]))
    retried = client.post(base + "/transcript")
    assert retried.status_code == 200, retried.text
    final = retried.json()["transcript"]
    assert final["state"] == "final" and len(final["segments"]) == 2
    assert final["segments"][0] == partial["segments"][0]
    assert final["revision"] > partial["revision"]
    job = client.get(base + "/jobs").json()["items"][0]
    with pytest.raises(ApiError) as error:
        store_asr_sentence(client.app.state.db.session_factory, sid, job["id"],
                           client.app.state.asr_provider, 3, AsrSegment("未知", "迟到句子。"))
    assert error.value.code == "job_expired"
    assert client.get(base + "/transcript").json() == final


def test_partial_asr_retry_cannot_overwrite_saved_evidence(client):
    sid = client.post("/api/sessions", json={"patient_name": "虚构音频病例"}).json()["id"]
    base = f"/api/sessions/{sid}"
    client.post(base + "/audio", files={"file": ("demo.wav", wav_bytes(), "audio/wav")})
    client.app.state.asr_provider = provider_for(FakeConnection([result_event("没有发热。"), failed_event()]))
    client.post(base + "/transcript")
    saved = client.get(base + "/transcript").json()["segments"]
    client.app.state.asr_provider = provider_for(FakeConnection([result_event("有发热。"), finished_event()]))
    assert client.post(base + "/transcript").status_code == 502
    after = client.get(base + "/transcript").json()
    assert after["state"] == "interrupted" and after["segments"] == saved
