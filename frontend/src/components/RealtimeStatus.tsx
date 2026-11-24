import { useEffect, useRef, useState } from "react";
import { ApiRequestError } from "../api";
import { readRealtime, controlRealtime, realtimeReady } from "../realtime";
import type { SessionDetail } from "../types";
import { errorMessage, formatTimestamp } from "../utils";
import { Modal } from "./Modal";

export function RealtimeStatus({
  workspace,
  disconnected,
  onUpdate,
  onFreshness,
}: {
  workspace: SessionDetail;
  disconnected: boolean;
  onUpdate: (s: SessionDetail) => void;
  onFreshness: (fresh: boolean) => void;
}) {
  const latest = useRef(workspace),
    update = useRef(onUpdate),
    freshness = useRef(onFreshness);
  latest.current = workspace;
  update.current = onUpdate;
  freshness.current = onFreshness;
  const [error, setError] = useState(""),
    [commandError, setCommandError] = useState(""),
    [busy, setBusy] = useState(false),
    [detailsOpen, setDetailsOpen] = useState(false),
    [stopOpen, setStopOpen] = useState(false),
    [retry, setRetry] = useState(0);
  const running = useRef(false);
  useEffect(() => {
    let disposed = false,
      timer: ReturnType<typeof setTimeout>,
      controller: AbortController | undefined;
    freshness.current(false);
    async function poll() {
      controller = new AbortController();
      const deadline = setTimeout(() => controller?.abort(), 10000);
      try {
        const next = await readRealtime(workspace.id, controller.signal);
        if (disposed) return;
        update.current(next);
        setError("");
        freshness.current(true);
      } catch (e) {
        if (disposed) return;
        freshness.current(false);
        setError(
          e instanceof ApiRequestError && e.status === 404
            ? "实时服务尚未接通，请联系管理员。"
            : errorMessage(e),
        );
      } finally {
        clearTimeout(deadline);
        if (!disposed) timer = setTimeout(() => void poll(), 2000);
      }
    }
    void poll();
    const pause = () => {
      controller?.abort();
      freshness.current(false);
    };
    window.addEventListener("zhenlu:unauthorized", pause);
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
      window.removeEventListener("zhenlu:unauthorized", pause);
    };
  }, [workspace.id, retry]);
  async function command(action: "start" | "stop" | "retry", chunkId?: string) {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setCommandError("");
    freshness.current(false);
    const id = workspace.id;
    try {
      const next = await controlRealtime(
        id,
        action,
        latest.current.realtime!.version,
        crypto.randomUUID(),
        chunkId,
      );
      if (latest.current.id === id) {
        update.current(next);
        setStopOpen(false);
      }
    } catch (e) {
      if (latest.current.id === id) setCommandError(errorMessage(e));
    } finally {
      running.current = false;
      setBusy(false);
      setRetry((n) => n + 1);
    }
  }
  const r = workspace.realtime!;
  const labels = {
    idle: "等待开始",
    starting: "等待设备响应",
    recording: "正在接收",
    stopping: "等待最后片段",
    ended: realtimeReady(workspace) ? "转写完成" : "正在收尾",
    disconnected: "采集设备断连",
  };
  const failedChunks = r.chunks.some((chunk) => chunk.status === "failed");
  const needsAttention = !!error || disconnected || r.capture === "disconnected" || failedChunks;
  const compactLabel = error ? "连接异常" : disconnected ? "设备断连" : failedChunks ? "片段处理异常" : labels[r.capture];
  return (
    <>
      <button
        type="button"
        className={`realtime-status-chip${needsAttention ? " needs-attention" : ""}`}
        aria-haspopup="dialog"
        aria-expanded={detailsOpen || stopOpen}
        aria-label={`${compactLabel}，查看接收状态与操作`}
        title={`已接收 ${r.chunks.length} 段 · 已转写 ${r.chunks.filter(c => c.status === "completed").length} 段`}
        onClick={() => setDetailsOpen(true)}
      >
        <span className="realtime-chip-dot" aria-hidden="true" />
        <span role="status">{compactLabel}</span>
        <span className="realtime-chip-chevron" aria-hidden="true">⌄</span>
      </button>
      {detailsOpen && <Modal
        title="实时音频接收"
        onClose={() => setDetailsOpen(false)}
        closeDisabled={busy}
        className="realtime-details-modal"
      >
    <section className="realtime-status" aria-label="实时音频接收">
      <div className="row-between">
        <div>
          <strong>{labels[r.capture]}</strong>
          <span className="realtime-counts">
            已接收 {r.chunks.length} 段 · 已转写{" "}
            {r.chunks.filter((c) => c.status === "completed").length} 段
          </span>
        </div>
        <div className="page-actions">
          {r.control_mode === "web" && r.capture === "idle" && (
            <button
              className="btn btn-primary btn-small"
              disabled={busy || disconnected || !!error}
              onClick={() => void command("start")}
            >
              开始接收
            </button>
          )}
          {r.control_mode === "web" &&
            ["starting", "recording", "disconnected"].includes(r.capture) && (
              <button
                className="btn btn-secondary btn-small"
                disabled={busy || !!error}
                onClick={() => { setDetailsOpen(false); setStopOpen(true); }}
              >
                结束接收
              </button>
            )}
          {error && (
            <button
              className="text-btn"
              disabled={busy}
              onClick={() => setRetry((n) => n + 1)}
            >
              重新连接
            </button>
          )}
        </div>
      </div>
      <p role="status">
        {error ||
          (r.control_mode === "device" && r.capture === "idle"
            ? "请在采集设备上开始录音，识别后的文字会自动显示。"
            : realtimeReady(workspace)
              ? "全部片段与最终草稿已就绪，请逐项核对后确认。"
              : "接收结束后仍需等待剩余片段和最终草稿处理完成。")}
      </p>
      {commandError && (
        <p className="field-error" role="alert">
          {commandError} 操作结果以最新接收状态为准。
        </p>
      )}
      <details>
        <summary>接收明细</summary>
        {r.chunks.map((c) => (
          <div className="realtime-chunk" key={c.id}>
            <span>片段 {c.seq}</span>
            <span>
              {formatTimestamp(c.offset_ms)}–
              {formatTimestamp(c.offset_ms + c.duration_ms)}
            </span>
            <span>
              {
                {
                  receiving: "上传中",
                  transcribing: "识别中",
                  completed: "转写完成",
                  failed: "识别失败",
                }[c.status]
              }
            </span>
            {c.status === "failed" && c.retryable ? (
              <button
                className="text-btn"
                disabled={busy || !!error}
                onClick={() => void command("retry", c.id)}
              >
                重新识别
              </button>
            ) : (
              <span />
            )}
            {c.error_message && (
              <p className="field-error">{c.error_message}</p>
            )}
          </div>
        ))}
      </details>
    </section>
    </Modal>}
      {stopOpen && (
        <Modal
          title="结束本次音频接收？"
          onClose={() => setStopOpen(false)}
          closeDisabled={busy}
          footer={
            <>
              <button
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => setStopOpen(false)}
              >
                继续问诊
              </button>
              <button
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void command("stop")}
              >
                {busy ? "正在发送…" : "请求结束接收"}
              </button>
            </>
          }
        >
          <p>
            后端将通知设备停止录音，接收最后一段音频并继续处理。设备确认停止前，页面保持等待状态。
          </p>
          {commandError && <p className="field-error" role="alert">{commandError} 操作结果以最新接收状态为准。</p>}
        </Modal>
      )}
    </>
  );
}
