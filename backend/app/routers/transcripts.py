from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import ApiError
from app.schemas import ManualTranscriptInput, RevisionRequest, SessionDetailOut, TranscriptAppend, TranscriptOut
from app.serializers import session_detail_dict, transcript_dict
from app.services.session_service import get_session_or_404
from app.services.workflow import create_manual_transcript
from app.services.jobs import execute_inline
from app.routers.assistant import IdempotencyKey
from app.services.live_transcript import append_stream, complete_stream, open_stream

router = APIRouter(prefix="/sessions", tags=["转写"])


@router.post("/{session_id}/transcript/stream", response_model=TranscriptOut)
def begin_live_transcript(session_id: str, db: Session = Depends(get_db)) -> dict:
    return transcript_dict(open_stream(db, session_id))


@router.post("/{session_id}/transcript/stream/segments", response_model=TranscriptOut)
def append_live_transcript(session_id: str, payload: TranscriptAppend, db: Session = Depends(get_db)) -> dict:
    return transcript_dict(append_stream(db, session_id, payload))


@router.post("/{session_id}/transcript/stream/complete", response_model=TranscriptOut)
def finish_live_transcript(session_id: str, payload: RevisionRequest, db: Session = Depends(get_db)) -> dict:
    return transcript_dict(complete_stream(db, session_id, payload.expected_revision))


@router.post("/{session_id}/transcript/manual", response_model=SessionDetailOut)
def manual_transcript(
    session_id: str, payload: ManualTranscriptInput, db: Session = Depends(get_db)
) -> dict:
    return session_detail_dict(create_manual_transcript(db, session_id, payload.text))


@router.post("/{session_id}/transcript", response_model=SessionDetailOut)
def transcribe(
    session_id: str, request: Request, idempotency_key: IdempotencyKey = None, db: Session = Depends(get_db)
) -> dict:
    execute_inline(db, request.app, session_id, "transcribe", request_key=idempotency_key)
    session = get_session_or_404(db, session_id)
    return session_detail_dict(session)


@router.get("/{session_id}/transcript", response_model=TranscriptOut)
def get_transcript(
    session_id: str, db: Session = Depends(get_db)
) -> dict:
    session = get_session_or_404(db, session_id)
    if session.transcript is None:
        raise ApiError(404, "transcript_not_found", "该会话尚未完成转写。")
    return transcript_dict(session.transcript)
