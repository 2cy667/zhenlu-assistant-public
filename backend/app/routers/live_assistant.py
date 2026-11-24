from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app.errors import ApiError
from app.follow_up_schemas import AdviceHistoryOut, AdviceStatusOut, LiveSnapshotOut
from app.models import AdvisorRun
from app.schemas import LiveAssistantControl
from app.serializers import transcript_dict
from app.services.live_assistant import advice_status, control_advisor, run_snapshot
from app.services.session_service import get_session_or_404
from app.services.follow_up import list_items

router = APIRouter(prefix="/sessions", tags=["实时临床助手"])


def _status(db: Session, session_id: str, app) -> dict:
    settings = app.state.settings
    return advice_status(db, session_id, available=settings.live_assistant_enabled,
                         worker_enabled=settings.job_worker_enabled)


@router.get("/{session_id}/assistant/advice", response_model=AdviceStatusOut)
def latest(session_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    return _status(db, session_id, request.app)


@router.put("/{session_id}/assistant/advice/settings", response_model=AdviceStatusOut)
def configure(session_id: str, payload: LiveAssistantControl, request: Request, db: Session = Depends(get_db)) -> dict:
    control_advisor(db, session_id, enabled=payload.enabled)
    return _status(db, session_id, request.app)


@router.post("/{session_id}/assistant/advice/refresh", status_code=202, response_model=AdviceStatusOut)
def refresh(session_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    if not request.app.state.settings.live_assistant_enabled:
        raise ApiError(409, "live_assistant_disabled", "服务配置尚未启用实时助手。")
    if not request.app.state.settings.job_worker_enabled:
        raise ApiError(409, "live_assistant_worker_disabled", "后台任务执行器尚未启用。")
    control_advisor(db, session_id)
    return _status(db, session_id, request.app)


@router.get("/{session_id}/assistant/advice/history", response_model=AdviceHistoryOut)
def history(session_id: str, limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)) -> dict:
    get_session_or_404(db, session_id)
    runs = db.scalars(select(AdvisorRun).where(AdvisorRun.session_id == session_id).order_by(AdvisorRun.started_at.desc(), AdvisorRun.id).offset(offset).limit(limit))
    return {"items": [run_snapshot(run) for run in runs]}


def event_snapshot(app, session_id: str) -> dict:
    with app.state.db.session_factory() as db:
        session = get_session_or_404(db, session_id)
        return LiveSnapshotOut.model_validate({
            "transcript": transcript_dict(session.transcript) if session.transcript else None,
            "assistant": _status(db, session_id, app),
            "items": list_items(db, session, advisor_enabled=app.state.settings.live_assistant_enabled),
        }).model_dump()


@router.get("/{session_id}/live/snapshot", response_model=LiveSnapshotOut)
def snapshot(session_id: str, request: Request) -> dict:
    return event_snapshot(request.app, session_id)


@router.get("/{session_id}/live/events", response_class=StreamingResponse,
            responses={200: {"description": "SSE snapshot 事件的 data 与 GET /live/snapshot 响应结构相同",
                             "content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def live_events(session_id: str, request: Request) -> StreamingResponse:
    # Check existence before headers; each subsequent snapshot uses a short-lived DB session.
    initial = await run_in_threadpool(event_snapshot, request.app, session_id)

    async def events():
        snapshot, last, idle = initial, None, 0
        while not await request.is_disconnected():
            data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            if data != last:
                yield f"event: snapshot\ndata: {data}\n\n"
                last, idle = data, 0
            elif idle >= 15:
                yield ": heartbeat\n\n"
                idle = 0
            await asyncio.sleep(1)
            idle += 1
            snapshot = await run_in_threadpool(event_snapshot, request.app, session_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
