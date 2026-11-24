import { useEffect, useState } from "react";
import { request } from "./api";
import { assertAuthenticated } from "./auth";
import type {
  ConfirmedField,
  GuidanceStatus,
  SessionDetail,
  Transcript,
  TranscriptSegment,
} from "./types";
import { errorMessage } from "./utils";

export interface Patient {
  id: string;
  patient_name: string;
  patient_ref: string | null;
}
export interface TrackedItem {
  id: string;
  origin_session_id: string;
  title: string;
  revision: number;
  status: GuidanceStatus;
  carry_forward: boolean;
  result: string;
}
export interface Advice {
  id: string;
  item_id: string | null;
  category: string;
  title: string;
  suggestion: string;
  rationale: string;
  sources: TranscriptSegment[];
  missing_information: string[];
  history_sources: {
    session_id: string;
    confirmed_at: string;
    field_key: string;
    label: string;
    value: string;
    version: number;
  }[];
}
interface AdviceRun {
  transcript_revision: number;
  suggestions: Advice[];
}
export interface LiveSnapshot {
  transcript: Transcript | null;
  items: TrackedItem[];
  assistant: {
    current: AdviceRun | null;
    previous: AdviceRun | null;
    available: boolean;
    enabled: boolean;
    worker_enabled: boolean;
    pending: boolean;
    active_run_id: string | null;
    error: { message: string } | null;
  };
}
export interface FollowUpSummary {
  previous_record: {
    session_id: string;
    confirmed_at: string;
    version: number;
    fields: ConfirmedField[];
  } | null;
  comparisons: {
    field_key: string;
    current_value: string | null;
    state: string;
    current_sources: TranscriptSegment[];
  }[];
}
export const deviceGateway = import.meta.env.VITE_DEVICE_GATEWAY === "true";
export const sessionPath = (id: string) =>
  "/sessions/" + encodeURIComponent(id);
export function write<T>(
  path: string,
  method: "POST" | "PUT" | "PATCH",
  body: object,
  key?: string,
) {
  return request<T>(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(key ? { "Idempotency-Key": key } : {}),
    },
    body: JSON.stringify(body),
  });
}
export const registerPatient = (name: string, reference: string, key: string) =>
  write<Patient>(
    "/patients",
    "POST",
    { patient_name: name.trim(), patient_ref: reference.trim() || null },
    key,
  );
export const linkPatient = (sessionId: string, patientId: string) =>
  write<SessionDetail>(sessionPath(sessionId) + "/patient", "PUT", {
    patient_id: patientId,
  });
export const updateItem = (
  id: string,
  item: TrackedItem,
  actor: string,
  status: GuidanceStatus,
  result?: string,
  carry_forward?: boolean,
) =>
  write<TrackedItem>(
    sessionPath(id) + "/items/" + encodeURIComponent(item.id),
    "PATCH",
    { expected_revision: item.revision, actor, status, result, carry_forward },
  );

// SSE sends a complete initial snapshot and repeats it after reconnect. Poll only during an outage.
export function useLiveConsultation(id: string, enabled: boolean) {
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  const [fresh, setFresh] = useState(false);
  const [error, setError] = useState("");
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let feed: EventSource | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const path = sessionPath(id) + "/live/";
    function accept(next: LiveSnapshot) {
      if (stopped) return;
      setSnapshot(next);
      setFresh(true);
      setError("");
    }
    function stopPoll() {
      clearTimeout(timer);
      controller?.abort();
    }
    async function poll() {
      if (stopped) return;
      const call = new AbortController();
      controller = call;
      try {
        const next = await request<LiveSnapshot>(path + "snapshot", {
          signal: call.signal,
        });
        if (!call.signal.aborted) accept(next);
      } catch (e) {
        if (!stopped && !call.signal.aborted) {
          setError(errorMessage(e));
          setFresh(false);
        }
      } finally {
        if (!stopped && !call.signal.aborted) timer = setTimeout(poll, 3000);
      }
    }
    function pause() {
      stopped = true;
      feed?.close();
      stopPoll();
      setFresh(false);
    }
    function resume() {
      setEpoch((n) => n + 1);
    }
    setFresh(false);
    try {
      assertAuthenticated();
      if (typeof EventSource === "undefined") void poll();
      else {
        feed = new EventSource("/api" + path + "events");
        feed.addEventListener("snapshot", (event) => {
          if (stopped) return;
          stopPoll();
          try {
            accept(JSON.parse(event.data));
          } catch {
            setFresh(false);
            setError("实时数据格式异常。");
            void poll();
          }
        });
        feed.onerror = () => {
          if (!stopped) {
            stopPoll();
            setFresh(false);
            void poll();
          }
        };
      }
    } catch (e) {
      setError(errorMessage(e));
    }
    window.addEventListener("zhenlu:locked", pause);
    window.addEventListener("zhenlu:authenticated", resume);
    return () => {
      stopped = true;
      feed?.close();
      stopPoll();
      window.removeEventListener("zhenlu:locked", pause);
      window.removeEventListener("zhenlu:authenticated", resume);
    };
  }, [id, enabled, epoch]);
  return {
    snapshot,
    fresh,
    error,
    refresh: () => {
      setFresh(false);
      setEpoch((n) => n + 1);
    },
  };
}
