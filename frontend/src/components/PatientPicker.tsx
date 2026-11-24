import { useEffect, useState } from "react";
import { request } from "../api";
import type { Patient } from "../consultation";
import { errorMessage } from "../utils";

export function PatientPicker({
  selected,
  onSelect,
}: {
  selected: Patient | null;
  onSelect: (p: Patient | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<Patient[]>([]);
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (selected) return;
    const controller = new AbortController();
    setItems([]);
    setMessage("正在查询…");
    const timer = setTimeout(async () => {
      try {
        const result = await request<{ items: Patient[] }>(
          "/patients?limit=15&query=" + encodeURIComponent(query.trim()),
          { signal: controller.signal },
        );
        if (!controller.signal.aborted) {
          setItems(result.items);
          setMessage(result.items.length ? "" : "未找到匹配的档案。");
        }
      } catch (e) {
        if (!controller.signal.aborted) setMessage(errorMessage(e));
      }
    }, 150);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, selected]);
  if (selected)
    return (
      <p>
        已选 {selected.patient_name} · {selected.patient_ref || selected.id}{" "}
        <button
          type="button"
          className="text-btn"
          onClick={() => onSelect(null)}
        >
          重新选择
        </button>
      </p>
    );
  return (
    <div>
      <label className="form-label">
        查找已有患者
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="姓名、档案编号或 ID"
        />
      </label>
      <div className="patient-results">
        {items.map((p) => (
          <button
            className="btn btn-secondary btn-small"
            key={p.id}
            type="button"
            onClick={() => onSelect(p)}
          >
            {p.patient_name} · {p.patient_ref || p.id}
          </button>
        ))}
      </div>
      {message && <p role="status">{message}</p>}
    </div>
  );
}
