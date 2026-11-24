import { useEffect, useRef, useState } from "react";
import type { BusyAction, GuidanceItem, SessionDetail } from "../types";
import { guidanceAnchor, validGuidance } from "../guidance";
import { ConsultationGuidance } from "./ConsultationGuidance";
import { formatTimestamp } from "../utils";
import type { Advice, LiveSnapshot, TrackedItem } from "../consultation";
import type { GuidanceStatus } from "../types";

interface TranscriptPanelProps {
  live: LiveSnapshot | null;
  liveError: string;
  itemBusy: boolean;
  onItemAction: (item: TrackedItem, status: GuidanceStatus) => void;
  workspace: SessionDetail;
  busy: BusyAction;
  highlightIds: string[];
  focusToken: number;
  onTranscribe: () => void;
  onManualSave: (text: string) => Promise<boolean>;
  onDirtyChange: (dirty: boolean) => void;
  guidanceFresh: boolean;
  onGuidanceUpdate: (next: SessionDetail) => void;
  onGuidanceLocate: (ids: string[]) => void;
}

function EmptyWaveform() {
  return (
    <div className="empty-wave" aria-hidden="true">
      <span />
      <span />
      <span />
      <span />
      <span />
      <span />
      <span />
    </div>
  );
}

export function TranscriptPanel({
  workspace,
  busy,
  highlightIds,
  focusToken,
  onTranscribe,
  onManualSave,
  onDirtyChange,
  guidanceFresh,
  onGuidanceUpdate,
  onGuidanceLocate,
  live,
  liveError,
  itemBusy,
  onItemAction,
}: TranscriptPanelProps) {
  const transcript = workspace.transcript;
  const assistant = live?.assistant;
  const current =
    assistant?.current?.transcript_revision === transcript?.revision
      ? assistant?.current
      : null;
  const older =
    !workspace.confirmed_version &&
    !current &&
    assistant?.enabled &&
    assistant.available
      ? assistant.previous
      : null;
  const shown = current || older;
  const nativeBySentence = new Map<string, Advice[]>();
  const order = new Map(transcript?.segments.map((s) => [s.id, s.seq]));
  for (const suggestion of shown?.suggestions ?? []) {
    const anchor = suggestion.sources
      .filter((s) => order.has(s.id))
      .sort((a, b) => b.seq - a.seq)[0];
    if (anchor)
      nativeBySentence.set(anchor.id, [
        ...(nativeBySentence.get(anchor.id) || []),
        suggestion,
      ]);
  }
  const rawGuidance = workspace.realtime?.guidance;
  const guidance = validGuidance(rawGuidance) ? rawGuidance : null;
  const sentenceHints = new Map<string, GuidanceItem[]>();
  let unlinkedCount = 0;
  for (const item of guidance?.items ?? []) {
    const anchor = guidanceAnchor(item, transcript?.segments ?? []);
    if (anchor)
      sentenceHints.set(anchor, [...(sentenceHints.get(anchor) ?? []), item]);
    else if (item.status === "open" || item.status === "asked") unlinkedCount++;
  }
  const activeCount = (guidance?.items ?? []).filter(
    (item) => item.status === "open" || item.status === "asked",
  ).length;
  const guidanceStatus = !rawGuidance
    ? "问诊提醒未启用"
    : !guidance
      ? "问诊提醒暂不可用"
      : !guidanceFresh
        ? "提醒同步中"
        : guidance.status === "failed"
          ? "提醒更新失败，保留上次结果"
          : guidance.status !== "ready" ||
              guidance.transcript_revision !==
                workspace.realtime?.transcript_revision
            ? "正在分析新对话"
            : unlinkedCount
              ? `${unlinkedCount} 条提醒等待关联原句`
              : activeCount
                ? "提醒已标在对应原句下方"
                : "暂无待处理提醒";
  const [follow, setFollow] = useState(true);
  const [manualOpen, setManualOpen] = useState(false);
  const [manualText, setManualText] = useState("");
  const lineCount = manualText
    .split(/\r?\n/)
    .filter((line) => line.trim()).length;
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (focusToken === 0) return;
    setFollow(false);
    const container = listRef.current;
    if (!container) return;
    const target = container.querySelector<HTMLElement>(
      '[data-highlighted="true"]',
    );
    if (!target) return;
    const containerRect = container.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const nextScrollTop =
      container.scrollTop + targetRect.top - containerRect.top - 12;
    container.scrollTo({ top: Math.max(0, nextScrollTop), behavior: "smooth" });
  }, [focusToken]);

  useEffect(() => {
    const el = listRef.current;
    if (
      (workspace.realtime || transcript?.state === "streaming") &&
      follow &&
      el
    )
      el.scrollTop = el.scrollHeight;
  }, [workspace.realtime?.transcript_revision, transcript?.revision, follow]);
  const highlightSet = new Set(highlightIds);

  return (
    <section className="card transcript-card">
      <header className="card-header">
        <div>
          <div className="heading-row">
            <h2>{workspace.realtime ? "实时转写" : "问诊转写"}</h2>
            {transcript?.provider === "manual" ? (
              <span className="badge badge-outline">手动录入</span>
            ) : transcript?.is_mock ? (
              <span className="badge badge-history">历史模拟转写</span>
            ) : transcript ? (
              <span className="badge badge-outline">语音识别</span>
            ) : null}
          </div>
        </div>
        {(workspace.realtime || transcript?.state === "streaming") && (
          <label className="follow-transcript">
            <input
              type="checkbox"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
            />
            跟随最新
          </label>
        )}
        {transcript ? (
          <span className="revision-chip">{transcript.segments.length} 句</span>
        ) : null}
      </header>
      {transcript?.state === "interrupted" && workspace.audio && (
        <button
          className="btn btn-secondary"
          disabled={!!busy}
          onClick={onTranscribe}
        >
          重试中断的转写
        </button>
      )}
      {!workspace.realtime && (
        <div className="sentence-guidance-status" role="status">
          <strong>会诊助手</strong>
          <span>
            {liveError ||
              (!guidanceFresh
                ? "正在同步"
                : !assistant?.available || !assistant.enabled
                  ? "未启用"
                  : assistant.error?.message ||
                    (workspace.latest_record
                      ? "记录已确认"
                      : older
                        ? "基于较早转录，等待最新分析"
                        : !assistant.worker_enabled
                          ? "后台分析未启用"
                          : assistant.pending || assistant.active_run_id
                            ? "正在分析新对话"
                            : current
                              ? current.suggestions.length
                                ? "提醒已标在原句下方"
                                : "本轮无新增提醒"
                              : "等待问诊内容"))}
          </span>
        </div>
      )}
      {workspace.realtime && (
        <div className="sentence-guidance-status" role="status">
          <strong>
            {activeCount > 0 ? `${activeCount} 条问诊提醒` : "AI 问诊提醒"}
          </strong>
          <span>{guidanceStatus}</span>
        </div>
      )}

      {!transcript && workspace.realtime ? (
        <div className="empty-panel">
          <EmptyWaveform />
          <h3>等待实时转写</h3>
          <p>采集端上传片段，识别完成的对话会在这里逐段显示。</p>
        </div>
      ) : !transcript ? (
        <div className="empty-panel">
          <EmptyWaveform />
          <h3>{workspace.audio ? "音频已就绪" : "录入问诊内容"}</h3>
          <p>
            {workspace.audio
              ? "开始语音识别，或手动录入问诊内容。"
              : "可直接输入问诊文字，也可以先上传音频。"}
          </p>
          {manualOpen ? (
            <div className="manual-transcript">
              <label htmlFor="manual-transcript">问诊内容（每行一段）</label>
              <textarea
                id="manual-transcript"
                className="field-textarea"
                rows={7}
                maxLength={20000}
                value={manualText}
                disabled={busy !== null}
                onChange={(event) => {
                  setManualText(event.target.value);
                  onDirtyChange(Boolean(event.target.value.trim()));
                }}
                placeholder="请输入问诊内容，每行一段。"
              />
              <p>手动内容不会自动关联音频时间；保存后不可编辑。</p>
              {lineCount > 200 ? (
                <p className="field-error" role="alert">
                  最多输入 200 行。
                </p>
              ) : null}
              <div className="manual-actions">
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={busy !== null}
                  onClick={() => {
                    setManualOpen(false);
                    setManualText("");
                    onDirtyChange(false);
                  }}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={
                    busy !== null || !manualText.trim() || lineCount > 200
                  }
                  onClick={async () => {
                    if (await onManualSave(manualText)) onDirtyChange(false);
                  }}
                >
                  {busy === "transcribe" ? "保存中…" : "保存问诊内容"}
                </button>
              </div>
            </div>
          ) : (
            <>
              {workspace.audio ? (
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={onTranscribe}
                  disabled={busy !== null}
                >
                  {busy === "transcribe" ? (
                    <>
                      <span className="spinner" aria-hidden="true" />
                      正在转写…
                    </>
                  ) : (
                    "开始转写"
                  )}
                </button>
              ) : null}
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => setManualOpen(true)}
                disabled={busy !== null}
              >
                手动录入问诊内容
              </button>
            </>
          )}
        </div>
      ) : (
        <>
          {transcript.provider === "manual" ? (
            <p className="transcript-notice">
              手动录入，未经过语音识别；无音频时间定位。
            </p>
          ) : transcript.notice ? (
            <p className="transcript-notice">{transcript.notice}</p>
          ) : null}

          <div
            className="transcript-list"
            ref={listRef}
            onScroll={() => {
              const el = listRef.current;
              if (el && el.scrollHeight - el.scrollTop - el.clientHeight > 80)
                setFollow(false);
            }}
          >
            {transcript.segments.map((segment) => {
              const highlighted = highlightSet.has(segment.id);
              const hints = sentenceHints.get(segment.id) ?? [];
              const activeHints = hints.filter(
                (item) => item.status === "open" || item.status === "asked",
              );
              const hintTone = activeHints.some(
                (item) => item.kind === "verify",
              )
                ? "verify"
                : "follow_up";
              const speakerClass =
                segment.speaker === "医生"
                  ? "doctor"
                  : segment.speaker === "患者"
                    ? "patient"
                    : "unknown";
              return (
                <article
                  key={segment.id}
                  id={`segment-${segment.id}`}
                  className={`segment${highlighted ? " highlighted" : ""}${activeHints.length ? ` segment-has-hint segment-hint-${hintTone}` : ""}`}
                  data-highlighted={highlighted ? "true" : "false"}
                >
                  <div className="segment-side">
                    <span className={`speaker-chip ${speakerClass}`}>
                      {segment.speaker || "未区分说话人"}
                    </span>
                    {segment.start_ms != null && segment.end_ms != null ? (
                      <span className="segment-time">
                        {formatTimestamp(segment.start_ms)}–
                        {formatTimestamp(segment.end_ms)}
                      </span>
                    ) : null}
                  </div>
                  <div className="segment-content">
                    <p className="segment-text">
                      {segment.text}
                      {segment.is_final === false && (
                        <span className="badge badge-outline">识别中</span>
                      )}
                    </p>
                    {hints.map((item) => (
                      <ConsultationGuidance
                        key={item.id}
                        workspace={workspace}
                        item={item}
                        fresh={guidanceFresh}
                        onUpdate={onGuidanceUpdate}
                        onLocate={onGuidanceLocate}
                      />
                    ))}
                    {nativeBySentence.get(segment.id)?.map((s) => {
                      const item = live?.items.find((i) => i.id === s.item_id);
                      const categories: Record<string, string> = {
                        follow_up: "建议追问",
                        clarification: "待核实",
                        red_flag: "重要线索",
                        examination: "检查参考",
                        treatment: "治疗方向",
                        diagnostic_consideration: "鉴别思路",
                      };
                      return (
                        <aside
                          key={s.id}
                          className={`sentence-hint hint-${s.category === "follow_up" ? "follow_up" : "verify"}`}
                        >
                          <strong>
                            {older ? "较早转录 · " : ""}
                            {categories[s.category] || "会诊建议"} · {s.title}
                          </strong>
                          <p>{s.suggestion}</p>
                          <p>{s.rationale}</p>
                          {s.missing_information.length > 0 && (
                            <p>待核实：{s.missing_information.join("、")}</p>
                          )}
                          {s.history_sources.map((h) => (
                            <p key={h.field_key ?? h.label}>
                              上次确认记录 · {h.confirmed_at.slice(0, 10)} · 第{" "}
                              {h.version} 版 · {h.label}：{h.value}
                            </p>
                          ))}
                          <div className="sentence-hint-actions">
                            <button
                              onClick={() =>
                                onGuidanceLocate(s.sources.map((src) => src.id))
                              }
                            >
                              核对 {s.sources.length} 句原文
                            </button>
                            {current && item && !workspace.latest_record && (
                              <>
                                <button
                                  disabled={!guidanceFresh || itemBusy}
                                  onClick={() => onItemAction(item, "asked")}
                                >
                                  标记已问
                                </button>
                                <button
                                  disabled={!guidanceFresh || itemBusy}
                                  onClick={() => onItemAction(item, "resolved")}
                                >
                                  已核实
                                </button>
                                <button
                                  disabled={!guidanceFresh || itemBusy}
                                  onClick={() =>
                                    onItemAction(item, "dismissed")
                                  }
                                >
                                  略过
                                </button>
                              </>
                            )}
                          </div>
                        </aside>
                      );
                    })}
                  </div>
                </article>
              );
            })}
          </div>
        </>
      )}
    </section>
  );
}
