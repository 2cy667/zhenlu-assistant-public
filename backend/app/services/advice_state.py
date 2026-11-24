from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import AdvisorRun, AdvisorState, ConsultationSession, utcnow


def lock_session(db: Session, session_id: str) -> ConsultationSession:
    """Lock and reload without running recovery (which may commit and release the lock)."""
    db.execute(update(ConsultationSession).where(ConsultationSession.id == session_id).values(
        updated_at=ConsultationSession.updated_at,
    ).execution_options(synchronize_session=False))
    db.expire_all()
    session = db.get(ConsultationSession, session_id)
    if session is None:
        raise ApiError(404, "session_not_found", "会话不存在，请刷新会话列表后重试。")
    return session


def queue_advice(db: Session, session_id: str, revision: int) -> None:
    """Called in the same transaction as final-sentence persistence. Never calls a model."""
    state = db.get(AdvisorState, session_id)
    if state is None:
        state = AdvisorState(session_id=session_id, enabled=True, target_revision=revision)
        db.add(state)
    else:
        state.target_revision = revision
    if state.enabled and state.pending_since is None:
        state.pending_since = utcnow()
    state.error_code = None
    state.error_message = None


def recover_advisor_runs(db: Session) -> int:
    now = utcnow()
    states = list(db.scalars(select(AdvisorState).where(
        AdvisorState.active_run_id.is_not(None), AdvisorState.lease_expires_at <= now,
    )))
    count = 0
    for state in states:
        run_id = state.active_run_id
        changed = db.execute(update(AdvisorState).where(
            AdvisorState.session_id == state.session_id, AdvisorState.active_run_id == run_id,
            AdvisorState.lease_expires_at <= now,
        ).values(active_run_id=None, lease_expires_at=None, error_code="advice_interrupted",
                 error_message="上次助手分析已中断。可手动重试，或等待新增转录。"))
        if changed.rowcount:
            db.execute(update(AdvisorRun).where(AdvisorRun.id == run_id, AdvisorRun.status == "running").values(
                status="interrupted", finished_at=now, error_code="advice_interrupted",
                error_message="任务中断，迟到结果不会作为当前建议。",
            ))
            count += 1
    if states:
        db.commit()
        db.expire_all()
    return count
