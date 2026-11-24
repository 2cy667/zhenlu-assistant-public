from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from threading import Event, Thread

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import AdvisorRun, AdvisorState, ConsultationItem, ConsultationSession, new_id, utcnow
from app.providers.clinical_advisor import validate_advice
from app.providers.qwen_client import DraftProviderError
from app.serializers import iso_utc, segment_dict
from app.services.advice_state import lock_session, queue_advice, recover_advisor_runs
from app.services.session_service import get_session_or_404
from app.services.follow_up import historical_fields, scoped_items

logger = logging.getLogger(__name__)
NOTICE = "供医生评估的辅助思路，需结合完整病史、查体及检查判断。原文引用支持病例线索，不代表诊断成立或已检索医学指南。"


def advice_context(db: Session, session: ConsultationSession) -> dict:
    transcript = session.transcript
    if not transcript or not transcript.segments:
        raise ApiError(409, "transcript_required", "尚无可分析的转录内容。")
    context = {
        "transcript_revision": transcript.revision,
        "transcript_complete": transcript.state == "final",
        "segments": [segment_dict(s) for s in transcript.segments],
        "historical_fields": historical_fields(db, session),
        "tracked_items": [{"id": i.id, "topic_key": i.topic_key, "title": i.title, "status": i.status,
                           "from_this_visit": i.origin_session_id == session.id,
                           "result": i.result} for i in db.scalars(scoped_items(session))],
    }
    if len(json.dumps(context, ensure_ascii=False)) > 150000:
        raise ApiError(413, "advice_context_too_large", "当前转录超出助手处理范围，原文已保存，请由医生核对。")
    return context


def run_snapshot(run: AdvisorRun) -> dict:
    return {
        "id": run.id, "session_id": run.session_id, "transcript_id": run.transcript_id,
        "transcript_revision": run.transcript_revision, "transcript_state": run.transcript_state,
        "provider": run.provider, "status": run.status, "suggestions": run.suggestions,
        "metrics": run.metrics, "started_at": iso_utc(run.started_at), "finished_at": iso_utc(run.finished_at),
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code else None,
        "requires_clinician_review": True, "medical_references_retrieved": False,
    }


def advice_status(db: Session, session_id: str, *, available: bool = True, worker_enabled: bool = True) -> dict:
    session = get_session_or_404(db, session_id)
    state = db.get(AdvisorState, session_id)
    latest = db.get(AdvisorRun, state.latest_run_id) if state and state.latest_run_id else None
    transcript = session.transcript
    current = bool(available and state and state.enabled and not session.confirmed_version and latest
                   and transcript and transcript.state != "interrupted" and latest.status == "succeeded"
                   and latest.transcript_id == transcript.id and latest.transcript_revision == transcript.revision)
    current_run = run_snapshot(latest) if current else None
    if current_run:
        item_ids = [s["item_id"] for s in current_run["suggestions"] if s.get("item_id")]
        if item_ids:
            open_ids = set(db.scalars(scoped_items(session).with_only_columns(ConsultationItem.id).where(
                ConsultationItem.id.in_(item_ids), ConsultationItem.status == "open")))
            current_run["suggestions"] = [s for s in current_run["suggestions"]
                                          if not s.get("item_id") or s["item_id"] in open_ids]
    return {
        "enabled": state.enabled if state else True, "available": available,
        "worker_enabled": available and worker_enabled,
        "transcript_revision": transcript.revision if transcript else None,
        "transcript_state": transcript.state if transcript else None,
        "pending": bool(state and state.pending_since), "active_run_id": state.active_run_id if state else None,
        "run_count": state.run_count if state else 0,
        "current": current_run,
        "previous": run_snapshot(latest) if latest and not current else None,
        "error": {"code": state.error_code, "message": state.error_message} if state and state.error_code else None,
        "notice": NOTICE,
    }


def control_advisor(db: Session, session_id: str, *, enabled: bool | None = None) -> None:
    get_session_or_404(db, session_id)
    session = lock_session(db, session_id)
    state = db.get(AdvisorState, session_id)
    if enabled is None:
        if session.confirmed_version:
            raise ApiError(409, "session_confirmed", "会话已确认，停止自动临床建议。")
        if not session.transcript or not session.transcript.segments:
            raise ApiError(409, "transcript_required", "尚无可分析的转录内容。")
        if session.transcript.state == "interrupted":
            raise ApiError(409, "transcript_interrupted", "请先恢复中断的转录，再请求助手分析。")
        if state and not state.enabled:
            raise ApiError(409, "live_assistant_disabled", "本会话的实时助手已关闭，请先启用。")
    if state is None:
        state = AdvisorState(session_id=session_id, enabled=True, target_revision=0)
        db.add(state)
        db.flush()
    if enabled is not None:
        state.enabled = enabled
    if not state.enabled:
        state.pending_since = None
        if state.active_run_id:
            run = db.get(AdvisorRun, state.active_run_id)
            run.status, run.finished_at = "cancelled", utcnow()
            state.active_run_id, state.lease_expires_at = None, None
    elif not session.confirmed_version and session.transcript and session.transcript.segments:
        latest = db.get(AdvisorRun, state.latest_run_id) if state.latest_run_id else None
        active = db.get(AdvisorRun, state.active_run_id) if state.active_run_id else None
        already_running = active and active.transcript_revision == session.transcript.revision
        if not already_running and (not latest or latest.transcript_revision != session.transcript.revision or latest.status != "succeeded"):
            queue_advice(db, session_id, session.transcript.revision)
    db.commit()


def claim_advice(app, session_id: str) -> tuple[str, dict] | None:
    settings = app.state.settings
    if not settings.live_assistant_enabled or app.state.clinical_advisor is None:
        return None
    with app.state.db.session_factory() as db:
        session = lock_session(db, session_id)
        state = db.get(AdvisorState, session_id)
        now = utcnow()
        if (not state or not state.enabled or state.active_run_id or not state.pending_since
                or state.pending_since > now - timedelta(seconds=settings.live_assistant_debounce_seconds)
                or (state.last_started_at and state.last_started_at > now - timedelta(seconds=settings.live_assistant_min_interval_seconds))):
            db.rollback()
            return None
        if session.confirmed_version or not session.transcript or not session.transcript.segments or session.transcript.state == "interrupted":
            state.pending_since = None
            db.commit()
            return None
        if state.run_count >= settings.live_assistant_max_runs_per_session:
            state.pending_since = None
            state.error_code, state.error_message = "advice_run_limit", "本次会话已达到自动分析次数上限，转录继续保存。"
            db.commit()
            return None
        try:
            context = advice_context(db, session)
        except ApiError as exc:
            state.pending_since = None
            state.error_code, state.error_message = exc.code, exc.message
            db.commit()
            return None
        rid = new_id()
        state.active_run_id = rid
        state.lease_expires_at = now + timedelta(seconds=settings.qwen_timeout_seconds + 30)
        state.last_started_at, state.pending_since = now, None
        state.run_count += 1
        state.error_code, state.error_message = None, None
        run = AdvisorRun(
            id=rid, session_id=session_id, transcript_id=session.transcript.id,
            transcript_revision=session.transcript.revision, transcript_state=session.transcript.state,
            provider=app.state.clinical_advisor.name,
            input_hash=hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            status="running", started_at=now,
        )
        db.add(run)
        db.commit()
        return rid, context


def materialize_suggestions(db, session, run, context, suggestions, fresh):
    sources = {s["id"]: s for s in context["segments"]}
    history = {f["field_key"]: f for f in context.get("historical_fields", [])}
    tracked = list(db.scalars(scoped_items(session))) if fresh else []
    by_id = {i.id: i for i in tracked}
    by_key = {i.topic_key: i for i in tracked if i.topic_key and i.origin_session_id == session.id}
    # Prefer a local topic. A previous visit may already own a carried item for the same question.
    known_keys = {i["topic_key"]: i["id"] for i in context.get("tracked_items", []) if i["topic_key"]}
    emitted, output = set(), []
    for index, suggestion in enumerate(suggestions):
        item_id = None
        if fresh and suggestion["category"] in ("follow_up", "clarification"):
            key = suggestion["topic_key"]
            existing = suggestion["existing_item_id"]
            if not existing and key not in by_key:
                existing = known_keys.get(key)
            item = by_id.get(existing) if existing else by_key.get(key)
            # A carried task can have been closed in another visit during inference.
            if existing and item is None:
                continue
            if item is None:
                item = ConsultationItem(origin_session_id=session.id, origin="assistant",
                                        title=suggestion["title"], topic_key=key)
                db.add(item)
                db.flush()
                by_key[key] = item
            if item.status != "open" or item.id in emitted:
                continue
            item_id = item.id
            emitted.add(item_id)
        output.append({**suggestion, "id": f"{run.id}:{index}", "item_id": item_id,
                       "sources": [sources[s] for s in suggestion["source_segment_ids"]],
                       "history_sources": [history[k] for k in suggestion["history_field_keys"]],
                       "basis_kind": "model_reasoning", "requires_clinician_review": True})
    return output


def execute_advice(app, run_id: str, context: dict) -> None:
    result, error = None, None
    try:
        result = app.state.clinical_advisor.suggest(context)
        suggestions = validate_advice({"suggestions": list(result.suggestions)}, context)
    except DraftProviderError as exc:
        error = ApiError(502, exc.code, exc.message, metrics=exc.metadata)
    except (ValueError, TypeError):
        error = ApiError(502, "advice_invalid_response", "助手建议格式或来源无效。")
    except Exception as exc:
        logger.error("Clinical advisor failed (%s)", type(exc).__name__)
        error = ApiError(502, "advice_failed", "助手分析暂时不可用，转录继续保存。")
    with app.state.db.session_factory() as db:
        run = db.get(AdvisorRun, run_id)
        if run is None:
            return
        sid = run.session_id
        session = lock_session(db, sid)
        run = db.get(AdvisorRun, run_id)
        state = db.get(AdvisorState, sid)
        now = utcnow()
        # Usage remains auditable even if the provider returns after cancellation/expiry.
        run.metrics = result.metadata if result else (error.metrics if error else {})
        if not state or state.active_run_id != run_id or not state.lease_expires_at or state.lease_expires_at <= now:
            if run.status == "running":
                run.status, run.finished_at = "interrupted", now
            if state and state.active_run_id == run_id:
                state.active_run_id, state.lease_expires_at = None, None
                state.error_code, state.error_message = "advice_interrupted", "上次助手分析已超时，可重试或等待新增转录。"
            db.commit()
            return
        state.active_run_id, state.lease_expires_at = None, None
        run.finished_at = now
        t = session.transcript
        fresh = bool(state.enabled and not session.confirmed_version and t and t.state != "interrupted"
                     and t.id == run.transcript_id and t.revision == run.transcript_revision)
        if error:
            run.status, run.error_code, run.error_message = "failed", error.code, error.message
            state.error_code, state.error_message = error.code, error.message
        else:
            run.suggestions = materialize_suggestions(db, session, run, context, suggestions, fresh)
            run.status = "succeeded" if fresh else "superseded"
            if fresh:
                state.latest_run_id = run.id
                state.pending_since = None
            elif state.enabled and not session.confirmed_version and t and t.id == run.transcript_id:
                previous = db.get(AdvisorRun, state.latest_run_id) if state.latest_run_id else None
                if not previous or previous.transcript_revision < run.transcript_revision:
                    # Continuous speech may outpace a model call. Expose the last completed
                    # snapshot as previous, explicitly stale, while newer analysis is queued.
                    state.latest_run_id = run.id
        db.commit()


class ClinicalAdvisorWorker:
    def __init__(self, app) -> None:
        self.app, self.stop_event = app, Event()
        self.thread = Thread(target=self._loop, name="clinical-advisor-worker", daemon=True)
        self.active_id: str | None = None

    def start(self) -> None:
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                with self.app.state.db.session_factory() as db:
                    recover_advisor_runs(db)
                    now = utcnow()
                    ids = list(db.scalars(select(AdvisorState.session_id).where(
                        AdvisorState.enabled.is_(True), AdvisorState.active_run_id.is_(None),
                        AdvisorState.pending_since <= now - timedelta(seconds=self.app.state.settings.live_assistant_debounce_seconds),
                        or_(AdvisorState.last_started_at.is_(None), AdvisorState.last_started_at <= now - timedelta(seconds=self.app.state.settings.live_assistant_min_interval_seconds)),
                    ).order_by(AdvisorState.pending_since).limit(20)))
                for sid in ids:
                    if self.stop_event.is_set():
                        break
                    claimed = claim_advice(self.app, sid)
                    if claimed:
                        self.active_id, context = claimed
                        if self.stop_event.is_set():
                            self._interrupt_active()
                            break
                        execute_advice(self.app, self.active_id, context)
                        self.active_id = None
            except Exception as exc:
                logger.error("Advisor worker failed (%s)", type(exc).__name__)
            self.stop_event.wait(self.app.state.settings.job_poll_interval_seconds)

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1)
        self._interrupt_active()

    def _interrupt_active(self) -> None:
        if self.active_id:
            with self.app.state.db.session_factory() as db:
                db.execute(update(AdvisorState).where(AdvisorState.active_run_id == self.active_id).values(lease_expires_at=utcnow()))
                db.commit()
                recover_advisor_runs(db)
