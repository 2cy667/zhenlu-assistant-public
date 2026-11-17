import { request } from "./api";
import type { SessionDetail, TranscriptSegment } from "./types";

// Proposed server contract: docs/realtime-api.md. No simulation or legacy upload fallback.
export async function createRealtimeSession(
  patient_name: string,
  patient_ref: string,
  key: string,
) {
  const next = await request<SessionDetail>("/sessions/realtime", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: JSON.stringify({ patient_name, patient_ref: patient_ref || null }),
    signal: AbortSignal.timeout(15000),
  });
  return validateRealtime(next);
}
export function validateRealtime(next: SessionDetail): SessionDetail {
  if (
    !next?.id ||
    !next.realtime ||
    !Number.isSafeInteger(next.realtime.version) ||
    next.realtime.version < 0 ||
    !Array.isArray(next.realtime.chunks)
  )
    throw new Error("实时服务返回的数据不完整，请联系管理员。");
  const ids = new Set<string>(),
    seqs = new Set<number>();
  const r = next.realtime;
  if (
    ![
      "idle",
      "starting",
      "recording",
      "stopping",
      "ended",
      "disconnected",
    ].includes(r.capture) ||
    !["web", "device"].includes(r.control_mode) ||
    typeof r.end_received !== "boolean" ||
    typeof r.final_draft_ready !== "boolean" ||
    !Number.isSafeInteger(r.transcript_revision) ||
    r.transcript_revision < 0 ||
    (r.draft_transcript_revision !== null &&
      (!Number.isSafeInteger(r.draft_transcript_revision) ||
        r.draft_transcript_revision < 0))
  )
    throw new Error("实时服务状态不完整，暂不能继续审核。");
  for (const c of next.realtime.chunks) {
    if (
      !c.id ||
      ids.has(c.id) ||
      seqs.has(c.seq) ||
      !Number.isSafeInteger(c.seq) ||
      c.seq < 1 ||
      !Number.isFinite(c.offset_ms) ||
      c.offset_ms < 0 ||
      !Number.isFinite(c.duration_ms) ||
      c.duration_ms <= 0 ||
      !["receiving", "transcribing", "completed", "failed"].includes(c.status)
    )
      throw new Error("音频片段顺序或时间信息无效。");
    ids.add(c.id);
    seqs.add(c.seq);
  }
  next.realtime.chunks.sort((a, b) => a.seq - b.seq);
  if (next.transcript) {
    const unique = new Map<string, TranscriptSegment>();
    for (const s of next.transcript.segments) {
      const old = unique.get(s.id);
      if (!old || (s.revision ?? 0) > (old.revision ?? 0)) unique.set(s.id, s);
    }
    next.transcript.segments = [...unique.values()].sort(
      (a, b) => a.seq - b.seq,
    );
  }
  return next;
}
export async function readRealtime(id: string, signal: AbortSignal) {
  const next = validateRealtime(
    await request<SessionDetail>(
      `/sessions/${encodeURIComponent(id)}/realtime`,
      { signal },
    ),
  );
  if (next.id !== id) throw new Error("实时结果与当前问诊不匹配。");
  return next;
}
export async function controlRealtime(
  id: string,
  command: "start" | "stop" | "retry",
  version: number,
  key: string,
  chunkId?: string,
) {
  const next = validateRealtime(
    await request<SessionDetail>(
      `/sessions/${encodeURIComponent(id)}/realtime/${command}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": key },
        body: JSON.stringify({
          expected_version: version,
          ...(chunkId ? { chunk_id: chunkId } : {}),
        }),
        signal: AbortSignal.timeout(15000),
      },
    ),
  );
  if (next.id !== id) throw new Error("实时结果与当前问诊不匹配。");
  return next;
}
export function realtimeReady(s: SessionDetail): boolean {
  const r = s.realtime;
  if (!r) return true;
  if (
    r.capture !== "ended" ||
    !r.end_received ||
    !Number.isSafeInteger(r.last_chunk_seq) ||
    !r.last_chunk_seq ||
    r.chunks.length !== r.last_chunk_seq ||
    !r.final_draft_ready ||
    r.draft_transcript_revision !== r.transcript_revision ||
    !s.draft ||
    !s.transcript ||
    !s.transcript.segments.length ||
    s.transcript.segments.some((t) => t.is_final !== true)
  )
    return false;
  return [...r.chunks]
    .sort((a, b) => a.seq - b.seq)
    .every((c, i) => c.seq === i + 1 && c.status === "completed");
}
export function newerSession(
  current: SessionDetail | null,
  next: SessionDetail,
): SessionDetail {
  if (
    current?.id === next.id &&
    current.realtime &&
    (!next.realtime || next.realtime.version < current.realtime.version)
  )
    return current;
  return next;
}
export function sourcePlayable(
  s: SessionDetail,
  source: TranscriptSegment,
): boolean {
  if (
    !s.transcript?.has_reliable_timestamps ||
    source.is_final === false ||
    source.start_ms == null ||
    source.end_ms == null ||
    source.end_ms <= source.start_ms
  )
    return false;
  if (!s.realtime) return !!s.audio;
  if (source.is_final !== true) return false;
  return (
    !!source.source_ranges?.length &&
    source.source_ranges.every((r) => {
      const c = s.realtime!.chunks.find((c) => c.id === r.chunk_id);
      return (
        c?.status === "completed" &&
        !!c.audio_url &&
        Number.isFinite(r.start_ms) &&
        Number.isFinite(r.end_ms) &&
        r.start_ms >= 0 &&
        r.end_ms > r.start_ms &&
        r.end_ms <= c.duration_ms + 100
      );
    })
  );
}
