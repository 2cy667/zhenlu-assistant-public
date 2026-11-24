from __future__ import annotations

import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.constants import SessionStatus
from app.errors import ApiError
from app.models import ProcessingJob, Transcript, TranscriptBatch, TranscriptSegment, new_id, utcnow
from app.schemas import TranscriptAppend
from app.services.advice_state import lock_session, queue_advice
from app.services.job_state import ensure_no_active_job
from app.services.session_service import ensure_editable, get_session_or_404


def open_stream(db: Session, session_id: str) -> Transcript:
    ensure_no_active_job(db, session_id)
    db.expire_all()
    session = get_session_or_404(db, session_id)
    ensure_editable(session)
    if session.transcript:
        if session.transcript.provider == "live-input" and session.transcript.state == "streaming":
            return session.transcript
        raise ApiError(409, "transcript_exists", "该会话已有转录，请新建会话。")
    session.transcript = Transcript(id=new_id(), session_id=session_id,
        audio_file_id=session.audio_file.id if session.audio_file else None,
        provider="live-input", is_mock=False, state="streaming", revision=0,
        language="zh-CN", notice="外部转录的最终句子逐批写入，原文需核对。")
    session.status = SessionStatus.TRANSCRIBING
    session.error_message = None
    db.commit()
    return session.transcript


def _persist_batch(db: Session, transcript: Transcript, request_id: str, payload: dict, segments: list[dict]) -> bool:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    existing = db.scalar(select(TranscriptBatch).where(
        TranscriptBatch.transcript_id == transcript.id, TranscriptBatch.request_id == request_id,
    ))
    if existing:
        if existing.payload_hash != digest:
            raise ApiError(409, "idempotency_conflict", "该转录批次编号已用于不同内容。")
        return False
    if transcript.state != "streaming":
        raise ApiError(409, "transcript_closed", "转录已结束，不能继续追加。")
    if len(transcript.segments) + len(segments) > 2000 or sum(len(s.text) for s in transcript.segments) + sum(len(s["text"]) for s in segments) > 100000:
        raise ApiError(413, "transcript_too_large", "当前转录超过 2000 句或 100000 字符限制，已保存内容保留。")
    seen = {s.seq for s in transcript.segments}
    for item in segments:
        if item["seq"] in seen:
            raise ApiError(409, "segment_sequence_conflict", "句子序号已存在，请使用原批次重试或检查顺序。")
        seen.add(item["seq"])
        transcript.segments.append(TranscriptSegment(id=new_id(), **item))
    transcript.revision += 1
    db.add(TranscriptBatch(transcript_id=transcript.id, request_id=request_id, payload_hash=digest, revision=transcript.revision))
    queue_advice(db, transcript.session_id, transcript.revision)
    return True


def append_stream(db: Session, session_id: str, payload: TranscriptAppend) -> Transcript:
    session = lock_session(db, session_id)
    transcript = session.transcript
    if transcript is None or transcript.provider != "live-input":
        raise ApiError(409, "live_transcript_required", "请先创建外部实时转录会话。")
    data = payload.model_dump()
    # Replayed batches remain idempotent after stream completion.
    existing = db.scalar(select(TranscriptBatch).where(TranscriptBatch.transcript_id == transcript.id, TranscriptBatch.request_id == payload.request_id))
    if not existing:
        ensure_editable(session)
        if transcript.revision != payload.expected_revision:
            raise ApiError(409, "transcript_revision_conflict", "转录版本已变化，请读取最新版本后提交。")
        next_seq = max((s.seq for s in transcript.segments), default=-1) + 1
        if [s.seq for s in payload.segments] != list(range(next_seq, next_seq + len(payload.segments))):
            raise ApiError(409, "segment_sequence_conflict", "新增句子序号必须连续且按顺序递增。")
    _persist_batch(db, transcript, payload.request_id, data, [s.model_dump() for s in payload.segments])
    session.updated_at = utcnow()
    db.commit()
    return transcript


def complete_stream(db: Session, session_id: str, expected_revision: int) -> Transcript:
    session = lock_session(db, session_id)
    transcript = session.transcript
    if transcript is None or transcript.provider != "live-input":
        raise ApiError(409, "live_transcript_required", "当前没有外部实时转录。")
    if transcript.state == "final" and expected_revision in (transcript.revision, transcript.revision - 1):
        return transcript
    ensure_editable(session)
    if expected_revision != transcript.revision:
        raise ApiError(409, "transcript_revision_conflict", "转录版本已变化，请刷新后结束转录。")
    if not transcript.segments:
        raise ApiError(409, "empty_transcript", "转录没有内容，不能生成病历。")
    transcript.state = "final"
    transcript.revision += 1
    session.status, session.updated_at = SessionStatus.TRANSCRIBED, utcnow()
    queue_advice(db, session_id, transcript.revision)
    db.commit()
    return transcript


def store_asr_sentence(factory, session_id: str, job_id: str | None, provider, seq: int, segment) -> None:
    """Called in the receiver thread, always with its own DB connection/transaction."""
    with factory() as db:
        if job_id:
            fenced = db.execute(update(ProcessingJob).where(
                ProcessingJob.id == job_id, ProcessingJob.session_id == session_id,
                ProcessingJob.status == "running", ProcessingJob.lease_expires_at > utcnow(),
                ProcessingJob.active_session_id == session_id,
            ).values(lease_expires_at=ProcessingJob.lease_expires_at))
            if fenced.rowcount != 1:
                raise ApiError(409, "job_expired", "转写任务已失效，新增句子未保存。")
        session = lock_session(db, session_id)
        ensure_editable(session)
        if session.transcript is None:
            session.transcript = Transcript(id=new_id(), session_id=session_id,
                audio_file_id=session.audio_file.id, provider=provider.name, is_mock=provider.is_mock,
                language="zh-CN", notice="实时转写中，仅保存已确定的识别句子。",
                state="streaming", revision=0)
            db.flush()
        transcript = session.transcript
        if transcript.provider != provider.name:
            raise ApiError(409, "transcript_provider_conflict", "转录来源已改变。")
        start, end = segment.start_ms, segment.end_ms
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            start, end = None, None
        item = {"seq": seq, "text": segment.text.strip(), "speaker": segment.speaker.strip() or "未知", "start_ms": start, "end_ms": end}
        existing = next((s for s in transcript.segments if s.seq == seq), None)
        if existing:
            # Retrying the same recording preserves all source IDs. Never replace evidence.
            saved = {key: getattr(existing, key) for key in item}
            if saved != item:
                raise ApiError(409, "asr_retry_conflict", "重试识别与已保存句子不一致，原文保留，请核对录音或新建会话。")
            return
        _persist_batch(db, transcript, f"{job_id or 'asr'}:{seq}", item, [item])
        session.updated_at = utcnow()
        db.commit()
