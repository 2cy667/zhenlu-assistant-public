from __future__ import annotations

import hashlib
from collections import defaultdict

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.constants import UNMENTIONED
from app.follow_up_schemas import ItemCreate, ItemUpdate
from app.models import AdvisorRun, AdvisorState, ConfirmedRecord, ConsultationItem, ConsultationSession, ItemAction
from app.serializers import confirmed_record_dict, draft_dict, iso_utc
from app.services.advice_state import lock_session
from app.services.session_service import ensure_editable


OPEN = ("open", "asked")


def prior_records(session: ConsultationSession):
    # Use a visit-start cutoff so a later confirmation cannot rewrite an earlier visit's context.
    return (select(ConfirmedRecord, ConsultationSession.created_at)
            .join(ConsultationSession, ConsultationSession.id == ConfirmedRecord.session_id)
            .where(ConsultationSession.patient_id == session.patient_id,
                   ConsultationSession.id != session.id, ConfirmedRecord.confirmed_at <= session.created_at))


def previous_record(db: Session, session: ConsultationSession) -> dict | None:
    if not session.patient_id:
        return None
    row = db.execute(prior_records(session).order_by(
        ConsultationSession.created_at.desc(), ConfirmedRecord.version.desc(), ConfirmedRecord.id
    ).limit(1)).first()
    if row is None:
        return None
    record, started = row
    return {"session_id": record.session_id, "record_id": record.id, "version": record.version,
            "visit_started_at": iso_utc(started), "confirmed_at": iso_utc(record.confirmed_at),
            "fields": confirmed_record_dict(record)["fields"]}


def scoped_items(session: ConsultationSession):
    visible = ConsultationItem.origin_session_id == session.id
    if session.patient_id:
        previous = prior_records(session).with_only_columns(ConfirmedRecord.session_id)
        handled_here = select(ItemAction.id).where(ItemAction.item_id == ConsultationItem.id,
                                                   ItemAction.session_id == session.id).exists()
        visible = or_(visible, and_(ConsultationItem.origin_session_id.in_(previous),
                                    or_(and_(ConsultationItem.carry_forward.is_(True),
                                             ConsultationItem.status.in_(OPEN)), handled_here)))
    return select(ConsultationItem).where(visible).order_by(ConsultationItem.created_at, ConsultationItem.id)


def current_item_ids(db: Session, session: ConsultationSession) -> set[str]:
    state = db.get(AdvisorState, session.id)
    run = db.get(AdvisorRun, state.latest_run_id) if state and state.enabled and state.latest_run_id else None
    transcript = session.transcript
    if (not run or run.status != "succeeded" or session.confirmed_version or not transcript
            or transcript.state == "interrupted" or run.transcript_id != transcript.id
            or run.transcript_revision != transcript.revision):
        return set()
    return {s["item_id"] for s in run.suggestions if s.get("item_id")}


def item_dicts(db: Session, items) -> list[dict]:
    items = list(items)
    if not items:
        return []
    history = defaultdict(list)
    for a in db.scalars(select(ItemAction).where(ItemAction.item_id.in_([i.id for i in items]))
                        .order_by(ItemAction.item_id, ItemAction.revision)):
        history[a.item_id].append({"actor": a.actor, "session_id": a.session_id, "status": a.status,
                                   "revision": a.revision, "result": a.result, "carry_forward": a.carry_forward,
                                   "created_at": iso_utc(a.created_at)})
    return [{"id": i.id, "origin_session_id": i.origin_session_id, "title": i.title,
             "origin": i.origin, "status": i.status, "revision": i.revision, "result": i.result,
             "carry_forward": i.carry_forward, "created_at": iso_utc(i.created_at), "actions": history[i.id]}
            for i in items]


def list_items(db: Session, session: ConsultationSession, *, advisor_enabled: bool) -> list[dict]:
    current = current_item_ids(db, session) if advisor_enabled else set()
    return item_dicts(db, (i for i in db.scalars(scoped_items(session))
                           if i.origin == "doctor" or i.revision > 1 or i.id in current))


def follow_up_summary(db: Session, session: ConsultationSession) -> dict:
    carried = scoped_items(session).where(ConsultationItem.origin_session_id != session.id,
                                         ConsultationItem.carry_forward.is_(True), ConsultationItem.status.in_(OPEN))
    previous = previous_record(db, session)
    current = {}
    if previous and session.draft:
        current = {f["key"]: f for f in draft_dict(session.draft, session.transcript)["fields"]}
    comparisons = []
    for old in previous["fields"] if previous else []:
        field = current.get(old["key"])
        state = "awaiting_draft"
        if field:
            if field["value"].strip() == UNMENTIONED:
                state = "not_mentioned"
            else:
                state = "same_text" if old["value"] == field["value"] else "different_text"
        comparisons.append({"field_key": old["key"], "previous_value": old["value"],
                            "current_value": field["value"] if field else None, "state": state,
                            "current_sources": field["sources"] if field else []})
    return {"session_id": session.id, "patient_id": session.patient_id, "previous_record": previous,
            "comparisons": comparisons,
            "pending_items": item_dicts(db, db.scalars(carried))}


def historical_fields(db: Session, session: ConsultationSession) -> list[dict]:
    record = previous_record(db, session)
    if not record:
        return []
    return [{"session_id": record["session_id"], "record_id": record["record_id"], "version": record["version"],
             "confirmed_at": record["confirmed_at"], "field_key": f["key"], "label": f["label"], "value": f["value"]}
            for f in record["fields"] if f["key"] and f["value"]]


def record_action(db: Session, item: ConsultationItem, session_id: str, actor: str) -> None:
    db.add(ItemAction(item_id=item.id, session_id=session_id, actor=actor, revision=item.revision,
                      status=item.status, result=item.result, carry_forward=item.carry_forward))


def create_item(db: Session, sid: str, payload: ItemCreate) -> dict:
    session = lock_session(db, sid)
    digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    item = db.scalar(select(ConsultationItem).where(ConsultationItem.origin_session_id == sid,
                                                    ConsultationItem.request_id == payload.request_id))
    if item:
        if item.request_hash != digest:
            raise ApiError(409, "idempotency_conflict", "该请求编号已用于其他事项。")
        return item_dicts(db, [item])[0]
    ensure_editable(session)
    if payload.carry_forward and not session.patient_id:
        raise ApiError(409, "patient_link_required", "跨次随访需要先关联患者档案。")
    item = ConsultationItem(origin_session_id=sid, origin="doctor", title=payload.title,
                            carry_forward=payload.carry_forward, request_id=payload.request_id, request_hash=digest)
    db.add(item)
    db.flush()
    record_action(db, item, sid, payload.actor)
    db.commit()
    return item_dicts(db, [item])[0]


def update_item(db: Session, sid: str, item_id: str, payload: ItemUpdate) -> dict:
    session = lock_session(db, sid)
    ensure_editable(session)
    item = db.scalar(scoped_items(session).where(ConsultationItem.id == item_id).with_for_update())
    if item is None:
        raise ApiError(404, "item_not_found", "当前会话中不存在该事项。")
    if item.revision != payload.expected_revision:
        raise ApiError(409, "item_revision_conflict", "事项已更新，请刷新后重试。")
    carry = item.carry_forward if payload.carry_forward is None else payload.carry_forward
    if carry and payload.status in OPEN and not session.patient_id:
        raise ApiError(409, "patient_link_required", "跨次随访需要先关联患者档案。")
    item.status = payload.status
    item.carry_forward = carry and payload.status in OPEN
    if payload.result is not None:
        item.result = payload.result.strip()
    item.revision += 1
    record_action(db, item, sid, payload.actor)
    db.commit()
    return item_dicts(db, [item])[0]
