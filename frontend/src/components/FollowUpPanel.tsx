import { useEffect, useRef, useState } from "react";
import { request } from "../api";
import {
  linkPatient,
  sessionPath,
  write,
  type FollowUpSummary,
  type LiveSnapshot,
  type Patient,
  type TrackedItem,
} from "../consultation";
import type { GuidanceStatus, SessionDetail } from "../types";
import { errorMessage } from "../utils";
import { PatientPicker } from "./PatientPicker";

const labels: Record<GuidanceStatus, string> = {
  open: "待核实",
  asked: "已问待答",
  resolved: "已解决",
  dismissed: "已忽略",
};
type Answer = {
  id: string;
  question: string;
  status: string;
  context_version: { transcript_revision: number; record_kind: string };
  answers: {
    text: string;
    sources: { id: string; text?: string; value?: string; label?: string }[];
  }[];
};

export function FollowUpPanel({
  session,
  actor,
  live,
  fresh,
  disabled,
  refresh,
  onUpdate,
  onAction,
  onDirtyChange,
}: {
  session: SessionDetail;
  actor: string;
  live: LiveSnapshot | null;
  fresh: boolean;
  disabled: boolean;
  refresh: () => void;
  onUpdate: (s: SessionDetail) => void;
  onDirtyChange: (dirty: boolean) => void;
  onAction: (
    i: TrackedItem,
    status: GuidanceStatus,
    result?: string,
    carry?: boolean,
  ) => Promise<boolean>;
}) {
  const path = sessionPath(session.id);
  const [summary, setSummary] = useState<FollowUpSummary | null>(null);
  const [retry, setRetry] = useState(0),
    [loadError, setLoadError] = useState("");
  const [patient, setPatient] = useState<Patient | null>(null);
  const [title, setTitle] = useState(""),
    [carry, setCarry] = useState(false);
  const [editing, setEditing] = useState<TrackedItem | null>(null),
    [result, setResult] = useState(""),
    [carryEdit, setCarryEdit] = useState(false);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [question, setQuestion] = useState(""),
    [answers, setAnswers] = useState<Answer[]>([]);
  const [qaOpen, setQaOpen] = useState(false);
  const requestId = useRef(crypto.randomUUID());
  const readOnly = !!session.confirmed_version;
  useEffect(() => {
    const c = new AbortController();
    setLoadError("");
    request<FollowUpSummary>(path + "/follow-up-summary", { signal: c.signal })
      .then((v) => {
        if (!c.signal.aborted) setSummary(v);
      })
      .catch((e) => {
        if (!c.signal.aborted) setLoadError(errorMessage(e));
      });
    return () => c.abort();
  }, [
    path,
    session.patient_id,
    session.draft?.revision,
    session.confirmed_version,
    retry,
  ]);
  useEffect(() => {
    if (!qaOpen) return;
    const c = new AbortController();
    request<{ items: Answer[] }>(path + "/assistant/questions", {
      signal: c.signal,
    })
      .then((v) => {
        if (!c.signal.aborted)
          setAnswers((current) => [
            ...new Map([...current, ...v.items].map((a) => [a.id, a])).values(),
          ]);
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(errorMessage(e));
      });
    return () => c.abort();
  }, [path, qaOpen]);
  async function run(task: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await task();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  const previous = summary?.previous_record;
  const locked = disabled || busy;
  const unsaved = Boolean(
    title.trim() ||
    question.trim() ||
    (editing &&
      (result !== editing.result || carryEdit !== editing.carry_forward)),
  );
  useEffect(() => onDirtyChange(unsaved), [unsaved, onDirtyChange]);
  return (
    <section className="card follow-up-panel">
      <details>
        <summary>
          复诊回顾与随访事项
          {live?.items.length ? `（${live.items.length}）` : ""}
        </summary>
        {loadError && (
          <p role="alert">
            {loadError}{" "}
            <button className="text-btn" onClick={() => setRetry((n) => n + 1)}>
              重试
            </button>
          </p>
        )}
        {!session.patient_id ? (
          <div>
            <p>此问诊尚未关联患者档案。</p>
            {(!session.transcript || readOnly) && (
              <>
                <PatientPicker selected={patient} onSelect={setPatient} />
                <button
                  className="btn btn-secondary"
                  disabled={!patient || locked}
                  onClick={() =>
                    void run(async () => {
                      onUpdate(await linkPatient(session.id, patient!.id));
                      refresh();
                    })
                  }
                >
                  确认关联此患者
                </button>
              </>
            )}
          </div>
        ) : previous ? (
          <>
            <h3>上次确认病历</h3>
            <p>
              {previous.confirmed_at.slice(0, 10)} · 第 {previous.version} 版
            </p>
            {previous.fields.map((f) => {
              const pair = summary?.comparisons.find(
                (c) => c.field_key === f.key,
              );
              return (
                <details key={f.key}>
                  <summary>
                    {f.label} · {f.value}
                  </summary>
                  <p>本次记录：{pair?.current_value ?? "待生成草稿"}</p>
                  {f.sources.map((s) => (
                    <p key={s.id}>
                      上次原话 · {s.speaker}：{s.text}
                    </p>
                  ))}
                  {pair?.current_sources.map((s) => (
                    <p key={s.id}>
                      本次原话 · {s.speaker}：{s.text}
                    </p>
                  ))}
                </details>
              );
            })}
          </>
        ) : (
          summary && <p>此次就诊前没有已确认的病历。</p>
        )}
        <h3>会诊事项</h3>
        {live && !live.items.length && <p>暂无事项。</p>}
        {live?.items.map((i) => (
          <div className="follow-up-item" key={i.id}>
            <strong>{i.title}</strong> · {labels[i.status]}{" "}
            {i.origin_session_id !== session.id ? "· 上次随访" : ""}{" "}
            {i.carry_forward ? "· 下次随访" : ""}
            {i.result && <p>{i.result}</p>}
            {!readOnly && (
              <button
                className="text-btn"
                disabled={locked || !fresh}
                onClick={() => {
                  setEditing(i);
                  setResult(i.result);
                  setCarryEdit(i.carry_forward);
                }}
              >
                处理事项
              </button>
            )}
            {editing?.id === i.id && !readOnly && (
              <div>
                {editing.revision !== i.revision && (
                  <p role="status">
                    事项已更新，请核对最新状态。
                    <button className="text-btn" onClick={() => setEditing(i)}>
                      按最新版本继续编辑
                    </button>
                  </p>
                )}
                <label className="form-label">
                  核实结果
                  <input
                    maxLength={1000}
                    value={result}
                    onChange={(e) => setResult(e.target.value)}
                  />
                </label>
                {session.patient_id && (
                  <label>
                    <input
                      type="checkbox"
                      checked={carryEdit}
                      onChange={(e) => setCarryEdit(e.target.checked)}
                    />{" "}
                    下次随访
                  </label>
                )}
                <div className="page-actions">
                  {(Object.keys(labels) as GuidanceStatus[]).map((status) => (
                    <button
                      key={status}
                      className="btn btn-small"
                      disabled={
                        locked || !fresh || editing.revision !== i.revision
                      }
                      onClick={() =>
                        void run(async () => {
                          if (
                            await onAction(editing, status, result, carryEdit)
                          )
                            setEditing(null);
                        })
                      }
                    >
                      {labels[status]}
                    </button>
                  ))}
                </div>
                <button className="text-btn" onClick={() => setEditing(null)}>
                  取消
                </button>
              </div>
            )}
          </div>
        ))}
        {!readOnly && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                await write(path + "/items", "POST", {
                  request_id: requestId.current,
                  title: title.trim(),
                  actor,
                  carry_forward: carry,
                });
                setTitle("");
                setCarry(false);
                requestId.current = crypto.randomUUID();
                refresh();
              });
            }}
          >
            <label className="form-label">
              新增事项
              <input
                value={title}
                disabled={busy}
                required
                maxLength={160}
                onChange={(e) => {
                  setTitle(e.target.value);
                  requestId.current = crypto.randomUUID();
                }}
              />
            </label>
            {session.patient_id && (
              <label>
                <input
                  type="checkbox"
                  checked={carry}
                  disabled={busy}
                  onChange={(e) => {
                    setCarry(e.target.checked);
                    requestId.current = crypto.randomUUID();
                  }}
                />{" "}
                下次随访
              </label>
            )}
            <button
              className="btn btn-secondary btn-small"
              disabled={locked || !title.trim()}
            >
              保存事项
            </button>
          </form>
        )}
      </details>
      {session.transcript && (
        <details onToggle={(e) => setQaOpen(e.currentTarget.open)}>
          <summary>查询本次会话</summary>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                const answer = await write<Answer>(
                  path + "/assistant/questions",
                  "POST",
                  { question: question.trim() },
                );
                setAnswers((old) => [
                  answer,
                  ...old.filter((a) => a.id !== answer.id),
                ]);
                setQuestion("");
              });
            }}
          >
            <label className="form-label">
              向会话助手提问
              <input
                value={question}
                disabled={busy}
                onChange={(e) => setQuestion(e.target.value)}
                maxLength={2000}
              />
            </label>
            <button
              className="btn btn-secondary btn-small"
              disabled={locked || !question.trim()}
            >
              提交问题
            </button>
          </form>
          {answers.map((a) => (
            <div key={a.id}>
              <strong>{a.question}</strong>
              <small>
                {" "}
                · 转录第 {a.context_version.transcript_revision} 版
              </small>
              {a.status !== "answered" && (
                <p>
                  {a.status === "insufficient"
                    ? "当前记录依据不足。"
                    : "超出本次记录范围。"}
                </p>
              )}
              {a.answers.map((entry, n) => (
                <div key={n}>
                  <p>{entry.text}</p>
                  <details>
                    <summary>回答依据</summary>
                    {entry.sources.map((s) => (
                      <p key={s.id}>
                        {s.label} {s.text || s.value}
                      </p>
                    ))}
                  </details>
                </div>
              ))}
            </div>
          ))}
        </details>
      )}
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
