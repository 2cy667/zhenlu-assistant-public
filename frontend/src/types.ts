export type SessionStatus =
  | "awaiting_audio"
  | "audio_ready"
  | "transcribing"
  | "transcribed"
  | "generating_draft"
  | "draft_ready"
  | "confirmed"
  | "failed";

export type FieldOrigin = "ai" | "doctor";

export type BusyAction =
  | "upload"
  | "transcribe"
  | "generate"
  | "save"
  | "regenerate"
  | "confirm"
  | null;

export interface AudioInfo {
  id: string;
  filename: string;
  content_type: string | null;
  size_bytes: number;
  duration_ms: number | null;
  uploaded_at: string;
  url: string;
}

export interface TranscriptSegment {
  revision?: number;
  is_final?: boolean;
  source_ranges?: AudioSourceRange[];
  id: string;
  seq: number;
  speaker: string;
  text: string;
  start_ms: number | null;
  end_ms: number | null;
}

export interface Transcript {
  id: string;
  provider: string;
  is_mock: boolean;
  language: string;
  notice: string;
  created_at: string;
  has_reliable_timestamps: boolean;
  segments: TranscriptSegment[];
}

export interface FieldSuggestion {
  value: string;
  sources: TranscriptSegment[];
  created_at: string;
}

export interface DraftField {
  key: string;
  label: string;
  value: string;
  ai_value: string;
  origin: FieldOrigin;
  is_doctor_edited: boolean;
  edited_at: string | null;
  is_unmentioned: boolean;
  sources: TranscriptSegment[];
  suggestion: FieldSuggestion | null;
}

export interface Draft {
  id: string;
  provider: string;
  is_mock: boolean;
  notice: string;
  revision: number;
  status: "editing" | "confirmed";
  created_at: string;
  updated_at: string;
  has_doctor_edits: boolean;
  has_suggestions: boolean;
  fields: DraftField[];
}

export interface ConfirmedField {
  key: string;
  label: string;
  value: string;
  origin: FieldOrigin;
  sources: TranscriptSegment[];
}

export interface ConfirmedRecord {
  id: string;
  version: number;
  draft_revision: number;
  confirmed_at: string;
  text: string;
  fields: ConfirmedField[];
}

export interface SessionSummary {
  id: string;
  patient_name: string;
  patient_ref: string | null;
  status: SessionStatus;
  status_label: string;
  created_at: string;
  updated_at: string;
  has_audio: boolean;
  has_transcript: boolean;
  has_draft: boolean;
  confirmed_version: number;
}

export interface SessionDetail extends SessionSummary {
  realtime?: RealtimeState | null;
  error_message: string | null;
  audio: AudioInfo | null;
  transcript: Transcript | null;
  draft: Draft | null;
  latest_record: ConfirmedRecord | null;
}

export interface AudioSourceRange {
  chunk_id: string;
  start_ms: number;
  end_ms: number;
}
export interface AudioChunk {
  id: string;
  seq: number;
  offset_ms: number;
  duration_ms: number;
  status: "receiving" | "transcribing" | "completed" | "failed";
  audio_url: string | null;
  error_message: string | null;
  retryable: boolean;
}
export interface RealtimeState {
  guidance?: ConsultationGuidance | null;
  version: number;
  capture:
    | "idle"
    | "starting"
    | "recording"
    | "stopping"
    | "ended"
    | "disconnected";
  control_mode: "web" | "device";
  end_received: boolean;
  last_chunk_seq: number | null;
  transcript_revision: number;
  draft_transcript_revision: number | null;
  final_draft_ready: boolean;
  chunks: AudioChunk[];
}

export type GuidanceStatus = "open" | "asked" | "resolved" | "dismissed";
export interface GuidanceItem {
  anchor_segment_id?: string | null;
  id: string;
  kind: "follow_up" | "verify";
  question: string;
  reason: string;
  priority: "routine" | "important";
  status: GuidanceStatus;
  source_ids: string[];
}
export interface ConsultationGuidance {
  revision: number;
  transcript_revision: number;
  status: "waiting" | "updating" | "ready" | "failed";
  items: GuidanceItem[];
}

export interface SessionListResponse {
  items: SessionSummary[];
}
