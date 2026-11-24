import { createRealtimeSession } from "../realtime";
import {
  deviceGateway,
  registerPatient,
  sessionPath,
  type Patient,
} from "../consultation";
import { PatientPicker } from "./PatientPicker";
import { useEffect, useRef, useState } from "react";
import {
  createSession,
  generateDraft,
  getSession,
  saveManualTranscript,
  request,
} from "../api";
import type { SessionDetail } from "../types";
import { errorMessage } from "../utils";
export function NewConsultation({
  initialMode,
  disconnected,
  onComplete,
  onDirtyChange,
  onBusyChange,
}: {
  initialMode: "text" | "audio";
  disconnected: boolean;
  onComplete: (s: SessionDetail) => void;
  onDirtyChange: (v: boolean) => void;
  onBusyChange: (v: boolean) => void;
}) {
  const [mode, setMode] = useState<"text" | "audio" | "stream" | "device">(
      initialMode,
    ),
    [patient, setPatient] = useState(""),
    [reference, setReference] = useState(""),
    [text, setText] = useState("");
  const [existing, setExisting] = useState(false);
  const [chosen, setChosen] = useState<Patient | null>(null);
  const registered = useRef<Patient | null>(null);
  const idempotencyKey = useRef(crypto.randomUUID());
  const previousIdentity = useRef("");
  const [realtimeSubmitted, setRealtimeSubmitted] = useState(false);
  const [busy, setBusy] = useState(false),
    [progress, setProgress] = useState(""),
    [error, setError] = useState(""),
    [created, setCreated] = useState<SessionDetail | null>(null);
  const working = useRef(false);
  useEffect(() => {
    onDirtyChange(!!(patient || reference || text || chosen));
  }, [patient, reference, text, chosen, onDirtyChange]);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (working.current || disconnected) return;
    if (existing ? !chosen : !patient.trim()) {
      setError(existing ? "请选择患者档案。" : "请输入患者姓名或代号。");
      return;
    }
    if (
      mode === "text" &&
      (!text.trim() ||
        text.length > 20000 ||
        text.split(/\r?\n/).filter((s) => s.trim()).length > 200)
    ) {
      setError("请输入问诊文字，最多 200 行、20,000 字符。");
      return;
    }
    working.current = true;
    setBusy(true);
    onBusyChange(true);
    setError("");
    try {
      const identity = JSON.stringify(
        existing ? chosen?.id : [patient.trim(), reference.trim()],
      );
      if (previousIdentity.current !== identity) {
        previousIdentity.current = identity;
        idempotencyKey.current = crypto.randomUUID();
      }
      setProgress("正在创建问诊…");
      if (mode === "device") {
        setRealtimeSubmitted(true);
        const next = await createRealtimeSession(
          existing ? chosen!.patient_name : patient.trim(),
          existing ? chosen!.patient_ref || "" : reference.trim(),
          idempotencyKey.current,
        );
        onDirtyChange(false);
        onComplete(next);
        return;
      }
      let next = created;
      if (!next) {
        let profile = chosen;
        if (!existing) {
          if (
            registered.current?.patient_name !== patient.trim() ||
            registered.current.patient_ref !== (reference.trim() || null)
          )
            registered.current = await registerPatient(
              patient,
              reference,
              idempotencyKey.current,
            );
          profile = registered.current;
        }
        next = await createSession(
          { patient_id: profile!.id },
          idempotencyKey.current,
        );
      } else next = await getSession(next.id);
      setCreated(next);
      if (mode === "stream") {
        await request(sessionPath(next.id) + "/transcript/stream", {
          method: "POST",
        });
        next = await getSession(next.id);
      }
      if (mode !== "text") {
        onDirtyChange(false);
        onComplete(next);
        return;
      }
      if (!next.transcript) {
        setProgress("正在保存问诊原文…");
        next = await saveManualTranscript(next.id, text.trim());
        setCreated(next);
      }
      if (!next.draft) {
        setProgress("正在整理病历草稿…");
        next = await generateDraft(next.id);
        setCreated(next);
      }
      onDirtyChange(false);
      onComplete(next);
    } catch (e) {
      if (
        mode === "device" &&
        e instanceof Error &&
        "status" in e &&
        [400, 404, 422, 503].includes(Number(e.status))
      ) {
        setRealtimeSubmitted(false);
        idempotencyKey.current = crypto.randomUUID();
      }
      setError(
        mode === "device" &&
          e instanceof Error &&
          "status" in e &&
          e.status === 404
          ? "实时音频服务尚未接通，请联系管理员。"
          : errorMessage(e),
      );
    } finally {
      setBusy(false);
      working.current = false;
      onBusyChange(false);
      setProgress("");
    }
  }
  return (
    <div className="intake-layout">
      <section className="surface">
        <div className="input-modes" role="group" aria-label="问诊输入方式">
          {(
            [
              "text",
              "audio",
              "stream",
              ...(deviceGateway ? ["device" as const] : []),
            ] as const
          ).map((m) => (
            <button
              key={m}
              aria-pressed={mode === m}
              className={mode === m ? "active" : ""}
              disabled={busy || !!created || realtimeSubmitted}
              onClick={() => setMode(m)}
            >
              {
                {
                  text: "文字输入",
                  audio: "上传录音",
                  stream: "外部实时转录",
                  device: "设备分段采音",
                }[m]
              }
            </button>
          ))}
        </div>
        <form onSubmit={(e) => void submit(e)}>
          <fieldset disabled={busy || !!created || realtimeSubmitted}>
            <label className="form-label">
              患者档案
              <select
                value={existing ? "existing" : "new"}
                onChange={(e) => {
                  setExisting(e.target.value === "existing");
                  setChosen(null);
                }}
              >
                <option value="new">新建患者档案</option>
                <option value="existing">选择已有患者</option>
              </select>
            </label>
            {existing ? (
              <PatientPicker selected={chosen} onSelect={setChosen} />
            ) : (
              <div className="form-grid">
                <label className="form-label">
                  患者姓名或代号
                  <input
                    value={patient}
                    maxLength={80}
                    required
                    onChange={(e) => setPatient(e.target.value)}
                    placeholder="请输入患者姓名或代号"
                  />
                </label>
                <label className="form-label">
                  患者档案编号（选填）
                  <input
                    value={reference}
                    maxLength={120}
                    onChange={(e) => setReference(e.target.value)}
                  />
                </label>
              </div>
            )}
            {mode === "text" ? (
              <label className="form-label">
                问诊对话
                <textarea
                  rows={10}
                  maxLength={20000}
                  required
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  placeholder="医生：哪里不舒服？&#10;患者：……"
                />
                <small>
                  {text.length} / 20000 字符 · 每行一段，最多 200 行
                </small>
              </label>
            ) : mode === "audio" ? (
              <p>
                进入工作台后上传录音，转写过程中可查看已识别的原句和问诊提醒。
              </p>
            ) : mode === "stream" ? (
              <p>
                由外部识别端向本次会话发送最终句子。进入工作台后可查看会话
                ID，并在接收完成后生成草稿。
              </p>
            ) : (
              <div className="realtime-intake">
                <h3>让问诊文字随对话到达。</h3>
                <p>
                  采集设备分段上传音频，识别后的文字在原审核工作台持续显示，病历草稿同步整理。
                </p>
                <ol>
                  <li>关联本次问诊与采集设备</li>
                  <li>接收原文并逐项核对</li>
                  <li>结束录音后确认病历</li>
                </ol>
                <p>
                  无需选择录音文件。进入工作台后，根据采集设备状态开始问诊。
                </p>
              </div>
            )}
          </fieldset>
          <p className="service-notice">
            音频和问诊文字将发送至已配置的模型服务，病历草稿须经医生核对确认。
          </p>
          <p role="status">{progress}</p>
          <p className="field-error" role="alert">
            {error}
          </p>
          <div className="page-actions">
            {created && !busy && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => {
                  onDirtyChange(false);
                  onComplete(created);
                }}
              >
                打开已保存会话
              </button>
            )}
            <button className="btn btn-primary" disabled={busy || disconnected}>
              {busy
                ? "正在处理…"
                : created
                  ? "继续处理此会话"
                  : mode !== "text"
                    ? "进入问诊工作台 →"
                    : "生成病历并进入审核 →"}
            </button>
          </div>
          {created && (
            <p className="small-note">
              问诊已建立。重试会继续当前会话，不会重复创建。
            </p>
          )}
        </form>
      </section>
      <aside className="intake-guidance">
        <p className="eyebrow">医生始终掌握最终记录</p>
        <h2>
          输入只是开始，
          <br />
          确认才是定稿。
        </h2>
        <p>原文、病历和来源并排展示，支持逐项核对与原音回听。</p>
        <ol>
          <li>文字与音频均可开始</li>
          <li>医生修改优先保留</li>
          <li>确认后锁定为只读</li>
        </ol>
      </aside>
    </div>
  );
}
