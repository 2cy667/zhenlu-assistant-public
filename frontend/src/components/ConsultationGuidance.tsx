import { useEffect, useRef, useState } from "react";
import { updateGuidance, validGuidance } from "../guidance";
import { ApiRequestError } from "../api";
import type { GuidanceItem, GuidanceStatus, SessionDetail } from "../types";
import { errorMessage } from "../utils";

interface Props {
  workspace: SessionDetail;
  item: GuidanceItem;
  fresh: boolean;
  onUpdate: (next: SessionDetail) => void;
  onLocate: (ids: string[]) => void;
}

// Rendered inside the associated sentence, never in a separate panel.
export function ConsultationGuidance({ workspace, item, fresh, onUpdate, onLocate }: Props) {
  const raw = workspace.realtime?.guidance;
  const g = validGuidance(raw) ? raw : null;
  const [pending, setPending] = useState(false), [error, setError] = useState("");
  const inFlight = useRef(false), mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const active = item.status === "open" || item.status === "asked";
  const stale = !!g && g.transcript_revision !== workspace.realtime?.transcript_revision;
  const locked = !fresh || stale || g?.status !== "ready" || workspace.draft?.status === "confirmed";
  const sources = item.source_ids.filter(id => workspace.transcript?.segments.some(s => s.id === id));
  async function act(status: GuidanceStatus) {
    if (inFlight.current || locked || !g) return;
    inFlight.current = true; setPending(true); setError("");
    try { const next = await updateGuidance(workspace.id, item.id, g.revision, status); if (mounted.current) onUpdate(next); }
    catch (e) { if (mounted.current) setError(e instanceof ApiRequestError && e.status === 404 ? "提醒操作暂不可用，请稍后重试。" : errorMessage(e)); }
    finally { inFlight.current = false; if (mounted.current) setPending(false); }
  }
  const actions = <div className="sentence-hint-actions">
    {sources.length > 1 && <button className="hint-evidence" onClick={() => onLocate(sources)}>对照 {sources.length} 句原文</button>}
    {active ? <>
      {item.kind === "follow_up" && item.status === "open" && <button disabled={locked || pending} onClick={() => void act("asked")}>标记已问</button>}
      {(item.kind === "verify" || item.status === "asked") && <button disabled={locked || pending} onClick={() => void act("resolved")}>已核实</button>}
      <button className="hint-dismiss" disabled={locked || pending} onClick={() => void act("dismissed")}>略过</button>
    </> : <button disabled={locked || pending} onClick={() => void act("open")}>重新打开</button>}
    {pending && <span role="status">保存中…</span>}
  </div>;
  const body = <>
    <p className="sentence-hint-reason">{item.reason}</p>
    {locked && active && <p className="sentence-hint-stale">{workspace.draft?.status === "confirmed" ? "病历已确认，提醒只读。" : "提醒待同步更新，暂不可处理。"}</p>}
    {actions}
    {error && <p className="field-error" role="alert">{error}</p>}
  </>;
  if (!active) return <details className="sentence-hint-completed" data-guidance-id={item.id}>
    <summary><span>{item.status === "resolved" ? "✓ 已核实" : "已略过"}</span> {item.question}</summary>{body}
  </details>;
  return <aside className={`sentence-hint hint-${item.kind}`} data-guidance-id={item.id} aria-label={`${item.kind === "verify" ? "待核实" : "建议追问"}：${item.question}`}>
    <div className="sentence-hint-heading"><span className="sentence-hint-icon" aria-hidden="true">{item.kind === "verify" ? "!" : "?"}</span>
      <strong>{item.kind === "verify" ? "这里需要核实" : "可以接着问"}</strong>
      <span className="sentence-hint-ai">AI 提醒</span>
      {item.status === "asked" && <span className="sentence-hint-waiting">已问 · 等待回答</span>}
    </div>
    <h3>{item.question}</h3>{body}
  </aside>;
}
