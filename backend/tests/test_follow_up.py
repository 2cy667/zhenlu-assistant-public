import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from app.providers.clinical_advisor import AdvisorResult
from app.routers.live_assistant import event_snapshot, live_events
from app.services.live_assistant import claim_advice, execute_advice


def register(client, ref):
    response = client.post("/api/patients", json={"patient_name": "虚构同名", "patient_ref": ref})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def visit(client, patient_id):
    response = client.post("/api/sessions", json={"patient_id": patient_id})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_intake_creation_retries_preserve_patient_and_session(client):
    headers = {"Idempotency-Key": "intake-retry"}
    for path, body in [("/api/patients", {"patient_name": "虚构患者"}), ("/api/sessions", {"patient_name": "虚构患者"})]:
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda _: client.post(path, json=body, headers=headers), range(2)))
        assert all(r.status_code == 201 for r in responses)
        assert responses[0].json()["id"] == responses[1].json()["id"]
        assert client.post(path, json={"patient_name": "另一位"}, headers=headers).status_code == 409


def confirm(client, sid, text="患者：之前用过一种药，名字记不清。"):
    base = f"/api/sessions/{sid}"
    assert client.post(base + "/transcript/manual", json={"text": text}).status_code == 200
    draft = client.post(base + "/draft").json()["draft"]
    response = client.post(base + "/confirm", json={"expected_revision": draft["revision"]})
    assert response.status_code == 200, response.text
    return response.json()["latest_record"]


def test_identity_lookup_and_explicit_legacy_link(client):
    a, b = register(client, "TEST-A"), register(client, "TEST-B")
    assert a != b
    assert [x["id"] for x in client.get("/api/patients?query=TEST-A").json()["items"]] == [a]
    assert len(client.get("/api/patients", params={"query": "虚构同名"}).json()["items"]) == 2
    assert client.post("/api/patients", json={"patient_name": "另一个人", "patient_ref": "TEST-A"}).status_code == 409
    assert client.post("/api/sessions", json={"patient_id": a, "patient_ref": "TEST-B"}).status_code == 409
    legacy = client.post("/api/sessions", json={"patient_name": "虚构同名", "patient_ref": "TEST-A"}).json()
    assert legacy["patient_id"] is None
    record = confirm(client, legacy["id"])
    link = f"/api/sessions/{legacy['id']}/patient"
    for _ in range(2):
        assert client.put(link, json={"patient_id": a}).json()["latest_record"] == record
    assert client.put(link, json={"patient_id": b}).status_code == 409
    assert client.get(f"/api/sessions/{visit(client, b)}/follow-up-summary").json()["previous_record"] is None
    later = visit(client, a)
    summary = client.get(f"/api/sessions/{later}/follow-up-summary").json()
    assert summary["previous_record"]["record_id"] == record["id"]


def test_summary_uses_only_prior_confirmed_records_and_compares_saved_values(client):
    patient = register(client, "A")
    first, unfinished = visit(client, patient), visit(client, patient)
    base = f"/api/sessions/{unfinished}"
    client.post(base + "/transcript/manual", json={"text": "虚构：没量过体温。"})
    client.post(base + "/draft")
    too_early = visit(client, patient)
    record = confirm(client, first)
    assert client.get(f"/api/sessions/{too_early}/follow-up-summary").json()["previous_record"] is None
    current = visit(client, patient)
    url = f"/api/sessions/{current}/follow-up-summary"
    before = client.get(url).json()
    assert before["previous_record"]["record_id"] == record["id"]
    assert before["pending_items"] == []
    assert [x["state"] for x in before["comparisons"]] == ["awaiting_draft"] * 5
    current_record = confirm(client, current, text="患者：今天只是复诊。")
    after = client.get(url).json()
    expected = {f["key"]: f["value"] for f in current_record["fields"]}
    assert {f["field_key"]: f["current_value"] for f in after["comparisons"]} == expected
    assert after["previous_record"] == before["previous_record"]


def test_carried_item_replay_parallel_updates_and_patient_isolation(client):
    p, other = register(client, "A"), register(client, "B")
    first = visit(client, p)
    path = f"/api/sessions/{first}/items"
    body = {"request_id": "follow-001", "title": "下次核实是否已复查", "actor": "医生甲", "carry_forward": True}
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post(path, json=body), range(2)))
    assert all(r.status_code == 201 for r in responses)
    item = responses[0].json()
    assert responses[1].json() == item and item["revision"] == 1
    assert client.post(path, json={**body, "title": "不同的事项"}).status_code == 409
    confirm(client, first)
    next_sid = visit(client, p)
    current = f"/api/sessions/{next_sid}"
    other_base = f"/api/sessions/{visit(client, other)}"
    patch = {"actor": "医生乙", "expected_revision": 1, "status": "resolved", "result": "已核对报告"}
    assert client.get(current + "/follow-up-summary").json()["pending_items"] == [item]
    assert client.get(other_base + "/items").json()["items"] == []
    assert client.patch(other_base + "/items/" + item["id"], json=patch).status_code == 404
    url = current + "/items/" + item["id"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: client.patch(url, json=patch), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    updated = client.get(current + "/items").json()["items"][0]
    assert not updated["carry_forward"] and updated["origin_session_id"] == first
    assert [(a["session_id"], a["actor"]) for a in updated["actions"]] == [(first, "医生甲"), (next_sid, "医生乙")]
    assert client.get(current + "/follow-up-summary").json()["pending_items"] == []
    assert event_snapshot(client.app, next_sid)["items"] == [updated]


def test_reconnect_delivers_latest_handled_items_with_model_unavailable(client):
    sid = visit(client, register(client, "OFFLINE"))
    base = f"/api/sessions/{sid}"

    class Subscriber:
        app = client.app

        async def is_disconnected(self):
            return False

    async def connect_once():
        stream = await live_events(sid, Subscriber())
        events = stream.body_iterator
        try:
            event = await anext(events)
            assert event.splitlines()[0] == "event: snapshot"
            return json.loads(event.splitlines()[1].removeprefix("data: "))
        finally:
            await events.aclose()

    assert asyncio.run(connect_once()) == client.get(base + "/live/snapshot").json()
    item = client.post(base + "/items", json={
        "request_id": "offline-1", "actor": "医生甲", "title": "核对是否复查"}).json()
    result = client.patch(base + "/items/" + item["id"], json={
        "expected_revision": 1, "actor": "医生甲", "status": "dismissed"}).json()
    restored = asyncio.run(connect_once())
    assert restored == client.get(base + "/live/snapshot").json()
    assert restored["items"] == [result] and not restored["assistant"]["available"]


def test_advisor_separates_sources_tracks_rephrasing_and_preserves_doctor_decision(client):
    patient = register(client, "A")
    first = visit(client, patient)
    previous = confirm(client, first)
    sid = visit(client, patient)
    base = f"/api/sessions/{sid}"
    settings = client.app.state.settings
    settings.live_assistant_enabled = True
    settings.live_assistant_min_interval_seconds = settings.live_assistant_debounce_seconds = 0
    assert client.post(base + "/transcript/stream").status_code == 200

    def append(seq, text):
        response = client.post(base + "/transcript/stream/segments", json={
            "request_id": f"r{seq}", "expected_revision": seq, "segments": [{"seq": seq, "text": text}]})
        assert response.status_code == 200

    class Advisor:
        name = "test-advisor"

        def suggest(self, context):
            assert {f["record_id"] for f in context["historical_fields"]} == {previous["id"]}
            return AdvisorResult(({"category": "clarification", "title": "核对用药" if len(context["segments"]) == 1 else "再核对药名",
                                   "topic_key": "patient.medication.name", "existing_item_id": None,
                                   "history_field_keys": ["medication_history"], "missing_information": ["药名"],
                                   "suggestion": "请确认用药记录。", "rationale": "本次提到药物。",
                                   "source_segment_ids": [context["segments"][-1]["id"]]},))

    client.app.state.clinical_advisor = Advisor()
    append(0, "患者：我今天停药了。")
    execute_advice(client.app, *claim_advice(client.app, sid))
    first_card = client.get(base + "/assistant/advice").json()["current"]["suggestions"][0]
    assert first_card["history_sources"][0]["record_id"] == previous["id"]
    assert first_card["sources"][0]["text"] == "患者：我今天停药了。"
    item_id = first_card["item_id"]
    append(1, "患者：药名我得查一下。")
    assert client.get(base + "/items").json()["items"] == []
    execute_advice(client.app, *claim_advice(client.app, sid))
    assert client.get(base + "/assistant/advice").json()["current"]["suggestions"][0]["item_id"] == item_id
    append(2, "患者：我目前记不清。")
    claim = claim_advice(client.app, sid)
    response = client.patch(base + "/items/" + item_id, json={
        "expected_revision": 1, "actor": "医生甲", "status": "asked", "result": "患者记不清"})
    assert response.status_code == 200, response.text
    execute_advice(client.app, *claim)
    snapshot = event_snapshot(client.app, sid)
    assert snapshot["assistant"]["current"]["suggestions"] == []
    assert [i["status"] for i in snapshot["items"]] == ["asked"]
    assert client.get(base).json()["draft"] is None
