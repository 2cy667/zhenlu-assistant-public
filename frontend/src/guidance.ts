import { request } from "./api";
import { validateRealtime } from "./realtime";
import type {
  ConsultationGuidance,
  GuidanceStatus,
  SessionDetail,
  GuidanceItem,
  TranscriptSegment,
} from "./types";

// Optional extension: an unavailable guidance service must not interrupt transcription.
export function validGuidance(value: unknown): value is ConsultationGuidance {
  if (!value || typeof value !== "object") return false;
  const g = value as ConsultationGuidance;
  if (
    !Number.isSafeInteger(g.revision) ||
    g.revision < 0 ||
    !Number.isSafeInteger(g.transcript_revision) ||
    g.transcript_revision < 0 ||
    !["waiting", "updating", "ready", "failed"].includes(g.status) ||
    !Array.isArray(g.items)
  )
    return false;
  const ids = new Set<string>();
  return g.items.every((item) => {
    if (
      !item ||
      typeof item.id !== "string" ||
      !item.id ||
      ids.has(item.id) ||
      !["follow_up", "verify"].includes(item.kind) ||
      !["open", "asked", "resolved", "dismissed"].includes(item.status) ||
      !["routine", "important"].includes(item.priority) ||
      typeof item.question !== "string" ||
      !item.question.trim() ||
      typeof item.reason !== "string" ||
      !Array.isArray(item.source_ids) ||
      (item.anchor_segment_id != null && typeof item.anchor_segment_id !== "string") ||
      item.source_ids.some((id) => typeof id !== "string")
    )
      return false;
    ids.add(item.id);
    return true;
  });
}

// An anchor is display context, not evidence. Do not attach orphans to unrelated sentences.
export function guidanceAnchor(item: GuidanceItem, segments: TranscriptSegment[]): string | null {
  if (item.anchor_segment_id && segments.some(s => s.id === item.anchor_segment_id)) return item.anchor_segment_id;
  const sources = segments.filter(s => item.source_ids.includes(s.id));
  return sources.reduce<TranscriptSegment | null>((latest, s) => !latest || s.seq > latest.seq ? s : latest, null)?.id ?? null;
}

export async function updateGuidance(
  id: string,
  itemId: string,
  revision: number,
  status: GuidanceStatus,
) {
  const next = validateRealtime(
    await request<SessionDetail>(
      `/sessions/${encodeURIComponent(id)}/realtime/guidance/${encodeURIComponent(itemId)}`,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({ expected_guidance_revision: revision, status }),
        signal: AbortSignal.timeout(10000),
      },
    ),
  );
  if (next.id !== id || !validGuidance(next.realtime?.guidance))
    throw new Error("提醒结果不完整，请等待同步后重试。");
  return next;
}
