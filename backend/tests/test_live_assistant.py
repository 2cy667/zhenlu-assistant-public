from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AdvisorState, utcnow
from app.providers.clinical_advisor import AdvisorResult
from app.providers.qwen_client import DraftProviderError
from app.services.advice_state import recover_advisor_runs
from app.services.live_assistant import ClinicalAdvisorWorker, claim_advice, control_advisor, execute_advice
from tests.test_fun_asr import FakeConnection, finished_event, provider_for, result_event, wav_bytes
from tests.test_live_transcript import append, new_stream


class RecordingAdvisor:
    name = "test-clinical-advisor"

    def __init__(self):
        self.calls = []
        self.during_call = None
        self.error = None

    def suggest(self, context):
        self.calls.append(context)
        if self.during_call:
            self.during_call()
        if self.error:
            raise self.error
        return AdvisorResult(({
            "category": "examination", "title": "核实发热线索",
            "topic_key": None, "existing_item_id": None, "history_field_keys": [],
            "suggestion": "可核实体温并结合查体评估。", "rationale": "当前有咳嗽描述，病因仍需核实。",
            "source_segment_ids": [context["segments"][0]["id"]],
            "missing_information": ["当前转录未记录实测体温"],
        },), {"model": "test", "usage": {"prompt_tokens": 100, "completion_tokens": 20},
              "estimated_cost_cny": 0.01, "cost_status": "estimated"})


@pytest.fixture
def advisor_client(settings, monkeypatch):
    advisor = RecordingAdvisor()
    monkeypatch.setattr("app.main.build_clinical_advisor", lambda settings: advisor)
    settings.live_assistant_enabled = True
    settings.live_assistant_debounce_seconds = 0
    settings.live_assistant_min_interval_seconds = 0
    settings.job_worker_enabled = False
    with TestClient(create_app(settings)) as client:
        yield client, advisor


def analyze(client, sid):
    claimed = claim_advice(client.app, sid)
    assert claimed is not None
    execute_advice(client.app, *claimed)
    return claimed


def status(client, sid):
    return client.get(f"/api/sessions/{sid}/assistant/advice").json()


def history(client, sid):
    return client.get(f"/api/sessions/{sid}/assistant/advice/history").json()["items"]


def test_advice_sources_are_session_scoped_and_do_not_modify_record(advisor_client):
    client, advisor = advisor_client
    sid, other = new_stream(client), new_stream(client)
    append(client, sid)
    append(client, other, text="另一个病例的私有内容。")
    analyze(client, sid)
    result = status(client, sid)["current"]
    assert result["requires_clinician_review"] and not result["medical_references_retrieved"]
    assert result["transcript_revision"] == 1 and result["transcript_state"] == "streaming"
    suggestion = result["suggestions"][0]
    assert suggestion["basis_kind"] == "model_reasoning"
    assert suggestion["sources"][0]["text"] == "患者：咳嗽三天。"
    assert "私有内容" not in json.dumps(advisor.calls, ensure_ascii=False)
    assert "patient_name" not in advisor.calls[0]
    assert client.get(f"/api/sessions/{sid}").json()["draft"] is None
    assert history(client, other) == []
    # No need to spend again for an already successful, unchanged snapshot.
    with client.app.state.db.session_factory() as db:
        control_advisor(db, sid)
    assert claim_advice(client.app, sid) is None


def test_new_sentences_coalesce_and_superseded_results_never_become_current(advisor_client):
    client, advisor = advisor_client
    sid = new_stream(client)
    append(client, sid)

    def newer_text():
        assert append(client, sid, revision=1, seq=1, text="患者：没有发热。").status_code == 200
        assert append(client, sid, revision=2, seq=2, text="医生：是否气促？").status_code == 200

    advisor.during_call = newer_text
    analyze(client, sid)
    assert status(client, sid)["current"] is None
    assert status(client, sid)["pending"]
    assert status(client, sid)["previous"]["transcript_revision"] == 1
    assert history(client, sid)[0]["status"] == "superseded"
    advisor.during_call = None
    analyze(client, sid)
    current = status(client, sid)["current"]
    assert current["transcript_revision"] == 3
    assert len(advisor.calls) == 2 and len(advisor.calls[-1]["segments"]) == 3
    usage = client.get(f"/api/sessions/{sid}/usage").json()
    assert usage["advisor_run_count"] == 2
    assert usage["advisor_runs_by_status"] == {"superseded": 1, "succeeded": 1}
    assert usage["reported_tokens"]["prompt_tokens"] == 200
    assert usage["known_estimated_cost_cny"] == 0.02


def test_previous_advice_is_marked_stale_on_new_transcription(advisor_client):
    client, _ = advisor_client
    sid = new_stream(client)
    append(client, sid)
    analyze(client, sid)
    append(client, sid, revision=1, seq=1)
    state = status(client, sid)
    assert state["current"] is None and state["previous"]["transcript_revision"] == 1
    client.post(f"/api/sessions/{sid}/transcript/stream/complete", json={"expected_revision": 2})
    analyze(client, sid)
    assert status(client, sid)["current"]["transcript_state"] == "final"


def test_disable_fences_late_results_and_preserves_usage(advisor_client):
    client, advisor = advisor_client
    sid = new_stream(client)
    append(client, sid)
    advisor.during_call = lambda: client.put(f"/api/sessions/{sid}/assistant/advice/settings", json={"enabled": False})
    analyze(client, sid)
    state = status(client, sid)
    assert not state["enabled"] and not state["pending"] and state["current"] is None
    run = history(client, sid)[0]
    assert run["status"] == "cancelled" and run["metrics"]["usage"]["prompt_tokens"] == 100
    assert claim_advice(client.app, sid) is None
    advisor.during_call = None
    client.put(f"/api/sessions/{sid}/assistant/advice/settings", json={"enabled": True})
    analyze(client, sid)
    assert status(client, sid)["current"]


def test_confirmed_record_fences_in_flight_advice(advisor_client):
    client, advisor = advisor_client
    sid = client.post("/api/sessions", json={"patient_name": "虚构病例"}).json()["id"]
    base = f"/api/sessions/{sid}"
    client.post(base + "/transcript/manual", json={"text": "患者：咳嗽三天。"})
    draft = client.post(base + "/draft").json()["draft"]

    def confirm():
        assert client.post(base + "/confirm", json={"expected_revision": draft["revision"]}).status_code == 200

    advisor.during_call = confirm
    analyze(client, sid)
    assert status(client, sid)["current"] is None
    assert history(client, sid)[0]["status"] == "superseded"
    assert client.get(base).json()["draft"]["fields"] == draft["fields"]


def test_model_failure_can_be_retried_without_affecting_transcript(advisor_client):
    client, advisor = advisor_client
    sid = new_stream(client)
    before = append(client, sid).json()
    advisor.error = DraftProviderError("draft_timeout", "模型请求超时。", {
        "attempts": [{"usage": {"prompt_tokens": 75}}, {"usage": {"prompt_tokens": 50}}],
        "cost_status": "unknown",
    })
    analyze(client, sid)
    assert client.get(f"/api/sessions/{sid}/transcript").json() == before
    assert status(client, sid)["error"]["code"] == "draft_timeout"
    assert not status(client, sid)["pending"]
    assert claim_advice(client.app, sid) is None
    advisor.error = None
    with client.app.state.db.session_factory() as db:
        control_advisor(db, sid)
    analyze(client, sid)
    assert status(client, sid)["current"]
    usage = client.get(f"/api/sessions/{sid}/usage").json()
    assert usage["reported_tokens"]["prompt_tokens"] == 225
    assert usage["advisor_runs_with_unknown_or_partial_cost"] == 1


def test_advice_claim_is_atomic_and_expired_result_is_fenced(advisor_client):
    client, _ = advisor_client
    sid = new_stream(client)
    append(client, sid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim_advice(client.app, sid), range(2)))
    claimed = [c for c in claims if c]
    assert len(claimed) == 1
    append(client, sid, revision=1, seq=1)
    with client.app.state.db.session_factory() as db:
        state = db.get(AdvisorState, sid)
        state.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        assert recover_advisor_runs(db) == 1
    execute_advice(client.app, *claimed[0])
    state = status(client, sid)
    assert state["current"] is None and state["pending"] and state["active_run_id"] is None
    assert history(client, sid)[0]["status"] == "interrupted"
    analyze(client, sid)
    assert status(client, sid)["current"]["transcript_revision"] == 2


def test_debounce_throttle_and_session_budget(advisor_client):
    client, advisor = advisor_client
    cfg = client.app.state.settings
    cfg.live_assistant_debounce_seconds = 2
    cfg.live_assistant_min_interval_seconds = 10
    cfg.live_assistant_max_runs_per_session = 2
    sid = new_stream(client)
    append(client, sid)
    assert claim_advice(client.app, sid) is None

    def age_queue(*, age_last=False):
        with client.app.state.db.session_factory() as db:
            state = db.get(AdvisorState, sid)
            state.pending_since = utcnow() - timedelta(seconds=3)
            if age_last:
                state.last_started_at = utcnow() - timedelta(seconds=11)
            db.commit()

    age_queue()
    analyze(client, sid)
    append(client, sid, revision=1, seq=1)
    age_queue()
    assert claim_advice(client.app, sid) is None
    age_queue(age_last=True)
    analyze(client, sid)
    append(client, sid, revision=2, seq=2)
    age_queue(age_last=True)
    assert claim_advice(client.app, sid) is None
    assert status(client, sid)["error"]["code"] == "advice_run_limit"
    assert len(advisor.calls) == 2
    assert len(client.get(f"/api/sessions/{sid}/transcript").json()["segments"]) == 3


def test_disabled_global_setting_never_calls_provider(client):
    sid = new_stream(client)
    append(client, sid)
    assert claim_advice(client.app, sid) is None
    assert not status(client, sid)["available"]
    assert client.post(f"/api/sessions/{sid}/assistant/advice/refresh").json()["code"] == "live_assistant_disabled"


def test_worker_shutdown_fences_in_flight_result(advisor_client):
    client, advisor = advisor_client
    sid = new_stream(client)
    append(client, sid)
    started, release = Event(), Event()

    def delayed_response():
        started.set()
        assert release.wait(5)

    advisor.during_call = delayed_response
    worker = ClinicalAdvisorWorker(client.app)
    worker.start()
    try:
        assert started.wait(3)
        worker.stop()
        assert status(client, sid)["active_run_id"] is None
        assert history(client, sid)[0]["status"] == "interrupted"
    finally:
        release.set()
        worker.thread.join(3)
    assert not worker.thread.is_alive()
    assert status(client, sid)["current"] is None
    assert history(client, sid)[0]["metrics"]["usage"]["prompt_tokens"] == 100


def test_background_advice_arrives_before_asr_completes(settings, monkeypatch):
    advisor = RecordingAdvisor()
    monkeypatch.setattr("app.main.build_clinical_advisor", lambda settings: advisor)
    settings.live_assistant_enabled = True
    settings.job_worker_enabled = True
    settings.job_poll_interval_seconds = 0.05
    settings.live_assistant_debounce_seconds = 0
    settings.live_assistant_min_interval_seconds = 0
    connection = FakeConnection([result_event("患者：咳嗽三天。")])
    with TestClient(create_app(settings)) as client:
        client.app.state.asr_provider = provider_for(connection)
        sid = client.post("/api/sessions", json={"patient_name": "虚构实时音频"}).json()["id"]
        base = f"/api/sessions/{sid}"
        client.post(base + "/audio", files={"file": ("demo.wav", wav_bytes(), "audio/wav")})
        job = client.post(base + "/jobs", json={"operation": "transcribe"}).json()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                advice = status(client, sid)
                if advice["current"]:
                    break
                time.sleep(0.02)
            assert advice["current"], advice
            assert client.get(base + f"/jobs/{job['id']}").json()["status"] == "running"
            assert client.get(base + "/transcript").json()["state"] == "streaming"
        finally:
            connection.incoming.put(json.dumps(finished_event()))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            done = client.get(base + f"/jobs/{job['id']}").json()
            if done["status"] != "running":
                break
            time.sleep(0.02)
        assert done["status"] == "succeeded", done
        assert client.get(base + "/transcript").json()["state"] == "final"
