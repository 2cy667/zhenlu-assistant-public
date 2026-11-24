import { RealtimeStatus } from "./RealtimeStatus";
import { Modal } from "./Modal";
import { RealtimeAudio } from "./RealtimeAudio";
import { realtimeReady } from "../realtime";
import { RecordExport } from "./RecordExport";
import { FollowUpPanel } from "./FollowUpPanel";
import {
  sessionPath,
  updateItem,
  useLiveConsultation,
  write,
  type TrackedItem,
} from "../consultation";
import type { TemplateSettings } from "../templates";
import { useEffect, useRef, useState } from "react";
import {
  confirmDraft,
  getSession,
  request,
  generateDraft,
  regenerateDraft,
  saveManualTranscript,
  saveDraftFields,
  startTranscription,
  uploadSessionAudio,
} from "../api";
import type { BusyAction, GuidanceStatus, SessionDetail } from "../types";
import { errorMessage, statusTone } from "../utils";
import { AudioPanel } from "./AudioPanel";
import { DraftPanel } from "./DraftPanel";
import { SourcePanel } from "./SourcePanel";
import { StatusStepper } from "./StatusStepper";
import { TranscriptPanel } from "./TranscriptPanel";
import type { ToastTone } from "./Toasts";

interface WorkspaceProps {
  actor: string;
  workspace: SessionDetail;
  templates: TemplateSettings;
  deviceDisconnected: boolean;
  onBusyChange: (busy: boolean) => void;
  notify: (tone: ToastTone, message: string) => void;
  onWorkspaceUpdated: (workspace: SessionDetail) => void;
  onDirtyChange: (dirty: boolean) => void;
}

export function Workspace({
  workspace: saved,
  actor,
  templates,
  deviceDisconnected,
  onBusyChange,
  notify,
  onWorkspaceUpdated,
  onDirtyChange,
}: WorkspaceProps) {
  const live = useLiveConsultation(saved.id, !saved.realtime);
  const synced = live.snapshot?.transcript;
  const transcript =
    synced &&
    (!saved.transcript ||
      (synced.id === saved.transcript.id &&
        synced.revision >= saved.transcript.revision))
      ? synced
      : saved.transcript;
  const workspace =
    transcript === saved.transcript ? saved : { ...saved, transcript };
  const [itemBusy, setItemBusy] = useState(false);
  const [draftDirty, setDraftDirty] = useState(false);
  const [manualDirty, setManualDirty] = useState(false);
  const [followUpDirty, setFollowUpDirty] = useState(false);
  useEffect(
    () => onDirtyChange(draftDirty || manualDirty || followUpDirty),
    [draftDirty, manualDirty, followUpDirty, onDirtyChange],
  );
  const itemWriting = useRef(false);
  async function actOnItem(
    item: TrackedItem,
    status: GuidanceStatus,
    result?: string,
    carry?: boolean,
  ) {
    if (
      itemWriting.current ||
      deviceDisconnected ||
      !live.fresh ||
      workspace.confirmed_version
    )
      return false;
    itemWriting.current = true;
    setItemBusy(true);
    try {
      await updateItem(workspace.id, item, actor, status, result, carry);
      notify("success", "事项已保存。");
      return true;
    } catch (e) {
      notify("error", errorMessage(e));
      return false;
    } finally {
      itemWriting.current = false;
      setItemBusy(false);
      live.refresh();
    }
  }
  const [realtimeFresh, setRealtimeFresh] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [mobilePane, setMobilePane] = useState("transcript");
  const [sourceOpen, setSourceOpen] = useState(false);
  const [transcriptOnly, setTranscriptOnly] = useState(false);
  const [guidanceSources, setGuidanceSources] = useState<string[] | null>(null);
  const actionRunning = useRef(false);
  const [busy, setBusy] = useState<BusyAction>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedFieldKey, setSelectedFieldKey] = useState<string | null>(null);
  const [sourceFocusToken, setSourceFocusToken] = useState(0);

  useEffect(() => {
    if (!workspace.draft) {
      setSelectedFieldKey(null);
      return;
    }
    setSelectedFieldKey((current) => {
      if (
        current &&
        workspace.draft?.fields.some((field) => field.key === current)
      ) {
        return current;
      }
      return (
        workspace.draft?.fields.find((field) => field.sources.length > 0)
          ?.key ??
        workspace.draft?.fields[0]?.key ??
        null
      );
    });
  }, [workspace.draft]);

  async function runAction(
    action: Exclude<BusyAction, null>,
    task: () => Promise<SessionDetail>,
    successMessage: string | ((next: SessionDetail) => string),
  ): Promise<boolean> {
    if (actionRunning.current) return false;
    if (
      workspace.realtime &&
      action !== "save" &&
      (!realtimeFresh || !realtimeReady(workspace))
    ) {
      notify("error", "请等待实时连接恢复、全部音频处理完成及最终草稿就绪。");
      return false;
    }
    if (deviceDisconnected && action !== "save") {
      notify("error", "设备已断开，请恢复连接后继续。");
      return false;
    }
    if (workspace.draft?.status === "confirmed") {
      notify("error", "当前记录已确认，只能查看与导出。");
      return false;
    }
    actionRunning.current = true;
    onBusyChange(true);
    setBusy(action);
    setActionError(null);
    try {
      const next = await task();
      onWorkspaceUpdated(next);
      notify(
        "success",
        typeof successMessage === "function"
          ? successMessage(next)
          : successMessage,
      );
      return true;
    } catch (error) {
      const message = errorMessage(error);
      setActionError(message);
      notify("error", message);
      return false;
    } finally {
      setBusy(null);
      actionRunning.current = false;
      onBusyChange(false);
    }
  }

  function handleUpload(file: File) {
    void runAction(
      "upload",
      () => uploadSessionAudio(workspace.id, file),
      "音频已上传并保存到本机。",
    );
  }

  function handleTranscribe() {
    void runAction(
      "transcribe",
      () => startTranscription(workspace.id),
      "转写已完成，请核对对话内容。",
    );
  }

  async function handleManualTranscript(text: string): Promise<boolean> {
    return runAction(
      "transcribe",
      () => saveManualTranscript(workspace.id, text),
      "问诊内容已保存，可以生成病历草稿。",
    );
  }

  function handleGenerate() {
    void runAction(
      "generate",
      () => generateDraft(workspace.id),
      "病历草稿已生成，请逐项核对。",
    );
  }

  function finishStream() {
    void runAction(
      "transcribe",
      async () => {
        await write(
          sessionPath(workspace.id) + "/transcript/stream/complete",
          "POST",
          { expected_revision: transcript!.revision },
        );
        return getSession(workspace.id);
      },
      "外部转录已结束，可以生成病历草稿。",
    );
  }

  async function handleSave(
    fields: Array<{
      field_key: string;
      value: string;
      accept_suggestion?: boolean;
    }>,
    expectedRevision: number,
  ): Promise<boolean> {
    return runAction(
      "save",
      () => saveDraftFields(workspace.id, expectedRevision, fields),
      "医生修改已保存。",
    );
  }

  async function handleRegenerate(expectedRevision: number): Promise<boolean> {
    return runAction(
      "regenerate",
      () => regenerateDraft(workspace.id, expectedRevision),
      (next) =>
        next.draft?.has_suggestions
          ? "已重新生成；医生修改未被覆盖，请查看系统新建议。"
          : "已重新生成；医生修改过的字段保持不变。",
    );
  }

  async function handleConfirm(expectedRevision: number): Promise<boolean> {
    return runAction(
      "confirm",
      () => confirmDraft(workspace.id, expectedRevision),
      (next) =>
        `病历已确认为第 ${next.latest_record?.version ?? 1} 版，可以复制纯文本。`,
    );
  }

  async function handleCopy(): Promise<void> {
    setExportOpen(true);
  }
  function handleSelectSource(fieldKey: string) {
    setGuidanceSources(null);
    setSelectedFieldKey(fieldKey);
    setSourceFocusToken((token) => token + 1);
    setSourceOpen(true);
  }

  const sourceField =
    workspace.draft?.fields.find((field) => field.key === selectedFieldKey) ??
    null;
  const highlightIds =
    guidanceSources ??
    (sourceOpen ? (sourceField?.sources.map((source) => source.id) ?? []) : []);

  return (
    <div
      className="workspace consultation-workspace"
      data-transcript-only={transcriptOnly}
    >
      <header className="workspace-header">
        <div className="workspace-title-block">
          <div className="title-line">
            <h1>{workspace.patient_name}</h1>
            {workspace.realtime && (
              <RealtimeStatus
                workspace={workspace}
                disconnected={deviceDisconnected}
                onUpdate={onWorkspaceUpdated}
                onFreshness={setRealtimeFresh}
              />
            )}
            {workspace.patient_ref ? (
              <span className="patient-ref">{workspace.patient_ref}</span>
            ) : null}
          </div>
          <p className="workspace-meta">
            <span title={workspace.id}>
              会话 {workspace.id.slice(0, 8).toUpperCase()}
            </span>
          </p>
        </div>
        <div className="workspace-header-actions">
          <button
            className="btn btn-secondary btn-small transcript-focus-toggle"
            aria-pressed={transcriptOnly}
            onClick={() => setTranscriptOnly((v) => !v)}
          >
            {transcriptOnly ? "展开病历区" : "专注转写"}
          </button>
          <span
            className={`status-chip tone-${statusTone(workspace.status)}`}
            data-status={workspace.status}
          >
            <span className="status-dot" aria-hidden="true" />
            {workspace.status_label}
          </span>
        </div>
      </header>

      <p className="service-notice">
        音频和问诊文字将发送至已配置的模型服务，病历草稿须经医生核对确认。
      </p>
      {!workspace.realtime && (
        <>
          {workspace.transcript?.provider === "live-input" && (
            <div className="service-notice">
              外部转录会话 <code>{workspace.id}</code>{" "}
              {workspace.transcript.state === "streaming" && (
                <button
                  className="btn btn-secondary btn-small"
                  disabled={
                    !live.fresh ||
                    deviceDisconnected ||
                    !!busy ||
                    !workspace.transcript.segments.length
                  }
                  onClick={finishStream}
                >
                  结束转录
                </button>
              )}
            </div>
          )}
          <FollowUpPanel
            session={workspace}
            actor={actor}
            live={live.snapshot}
            fresh={live.fresh}
            disabled={deviceDisconnected || itemBusy}
            refresh={live.refresh}
            onUpdate={onWorkspaceUpdated}
            onAction={actOnItem}
            onDirtyChange={setFollowUpDirty}
          />
          {workspace.transcript &&
            live.snapshot?.assistant.available &&
            !workspace.confirmed_version && (
              <div className="page-actions">
                <button
                  className="text-btn"
                  disabled={!live.fresh || itemBusy || deviceDisconnected}
                  onClick={async () => {
                    setItemBusy(true);
                    try {
                      await write(
                        sessionPath(workspace.id) +
                          "/assistant/advice/settings",
                        "PUT",
                        { enabled: !live.snapshot!.assistant.enabled },
                      );
                    } catch (e) {
                      notify("error", errorMessage(e));
                    } finally {
                      setItemBusy(false);
                      live.refresh();
                    }
                  }}
                >
                  {live.snapshot.assistant.enabled
                    ? "暂停会诊助手"
                    : "启用会诊助手"}
                </button>
                {live.snapshot.assistant.error && (
                  <button
                    className="text-btn"
                    disabled={!live.fresh || itemBusy || deviceDisconnected}
                    onClick={async () => {
                      setItemBusy(true);
                      try {
                        await request(
                          sessionPath(workspace.id) +
                            "/assistant/advice/refresh",
                          { method: "POST" },
                        );
                      } catch (e) {
                        notify("error", errorMessage(e));
                      } finally {
                        setItemBusy(false);
                        live.refresh();
                      }
                    }}
                  >
                    重新分析
                  </button>
                )}
              </div>
            )}
        </>
      )}

      {!workspace.realtime && (
        <StatusStepper
          status={workspace.status}
          hasAudio={workspace.audio !== null}
          hasTranscript={workspace.transcript !== null}
          hasDraft={workspace.draft !== null}
          busy={busy}
        />
      )}

      {workspace.status === "failed" && workspace.error_message ? (
        <div className="alert alert-error" role="alert">
          <div>
            <strong>上次处理失败</strong>
            <p>{workspace.error_message}</p>
          </div>
        </div>
      ) : null}

      {actionError ? (
        <div className="alert alert-error" role="alert">
          <div>
            <strong>操作未完成</strong>
            <p>{actionError}</p>
          </div>
          <button
            type="button"
            className="alert-close"
            onClick={() => setActionError(null)}
            aria-label="关闭错误提示"
          >
            ×
          </button>
        </div>
      ) : null}

      <div className="review-tabs" role="group" aria-label="工作台视图">
        {[
          ["transcript", "问诊与提醒"],
          ["draft", "病历审核"],
        ].map(([key, title]) => (
          <button
            key={key}
            className={mobilePane === key ? "active" : ""}
            aria-pressed={mobilePane === key}
            onClick={() => setMobilePane(key)}
          >
            {title}
          </button>
        ))}
      </div>
      <div className="review-grid" data-pane={mobilePane}>
        <div className="review-original">
          {workspace.realtime ? (
            <RealtimeAudio workspace={workspace} />
          ) : !workspace.transcript || workspace.audio ? (
            <AudioPanel
              workspace={workspace}
              busy={busy}
              onUpload={handleUpload}
            />
          ) : null}

          <TranscriptPanel
            workspace={workspace}
            busy={busy}
            highlightIds={highlightIds}
            focusToken={sourceFocusToken}
            onTranscribe={handleTranscribe}
            onManualSave={handleManualTranscript}
            onDirtyChange={setManualDirty}
            guidanceFresh={workspace.realtime ? realtimeFresh : live.fresh}
            live={live.snapshot}
            liveError={live.error}
            itemBusy={itemBusy || deviceDisconnected || !!busy}
            onItemAction={(item, status) => void actOnItem(item, status)}
            onGuidanceUpdate={onWorkspaceUpdated}
            onGuidanceLocate={(ids) => {
              setGuidanceSources(ids);
              setSourceFocusToken((v) => v + 1);
              setMobilePane("transcript");
            }}
          />
        </div>
        <DraftPanel
          workspace={workspace}
          busy={busy}
          selectedFieldKey={selectedFieldKey}
          deviceDisconnected={
            deviceDisconnected ||
            workspace.transcript?.state === "streaming" ||
            workspace.transcript?.state === "interrupted" ||
            (!!workspace.realtime &&
              (!realtimeFresh || !realtimeReady(workspace)))
          }
          onSelectField={handleSelectSource}
          onGenerate={handleGenerate}
          onSave={handleSave}
          onRegenerate={handleRegenerate}
          onConfirm={handleConfirm}
          onCopy={handleCopy}
          onDirtyChange={setDraftDirty}
        />
      </div>
      {sourceOpen && (
        <Modal
          className="source-drawer"
          title="来源核对"
          subtitle="核对字段原句，必要时回听对应音频。"
          onClose={() => setSourceOpen(false)}
        >
          <SourcePanel
            workspace={workspace}
            field={sourceField}
            onSelectField={handleSelectSource}
          />
        </Modal>
      )}
      {exportOpen && (
        <RecordExport
          workspace={workspace}
          templates={templates}
          onClose={() => setExportOpen(false)}
        />
      )}
    </div>
  );
}
