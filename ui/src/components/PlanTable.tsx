import { useMemo, useState } from "react";
import type { IndexMismatch } from "../types";

interface Props {
  rows: IndexMismatch[];
  selected: Set<string>;
  onToggle: (name: string) => void;
  onSetAll: (names: string[]) => void;
}

type SortKey = "index" | "src_count" | "tgt_count" | "gap";

const fmt = (n: number) => n.toLocaleString();

export function PlanTable({ rows, selected, onToggle, onSetAll }: Props) {
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "gap", dir: -1 });

  const visible = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const list = f ? rows.filter((r) => r.index.toLowerCase().includes(f)) : rows.slice();
    list.sort((a, b) => {
      const av = a[sort.key], bv = b[sort.key];
      return (av < bv ? -1 : av > bv ? 1 : 0) * sort.dir;
    });
    return list;
  }, [rows, filter, sort]);

  const allVisibleSelected = visible.length > 0 && visible.every((r) => selected.has(r.index));
  const selectedGap = rows.filter((r) => selected.has(r.index)).reduce((s, r) => s + r.gap, 0);

  function header(key: SortKey, label: string, num = false) {
    const active = sort.key === key;
    return (
      <th className={num ? "num" : undefined} onClick={() => setSort({ key, dir: active ? (sort.dir === 1 ? -1 : 1) : -1 })}>
        {label}{active ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
      </th>
    );
  }

  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <input placeholder="filter indices…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <button className="btn btn-sm fixed" onClick={() => onSetAll(allVisibleSelected ? [] : visible.map((r) => r.index))}>
          {allVisibleSelected ? "Deselect visible" : "Select visible"}
        </button>
        <button className="btn btn-sm fixed" onClick={() => onSetAll(visible.filter((r) => !r.source_only).map((r) => r.index))}>
          Only gaps
        </button>
      </div>
      <div className="tablewrap">
        <table>
          <thead>
            <tr>
              <th style={{ width: 28 }}>
                <input type="checkbox" checked={allVisibleSelected} onChange={() => onSetAll(allVisibleSelected ? [] : visible.map((r) => r.index))} />
              </th>
              {header("index", "Index")}
              {header("src_count", "Source", true)}
              {header("tgt_count", "Target", true)}
              {header("gap", "Gap", true)}
              <th>Kind</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((r) => (
              <tr key={r.index}>
                <td><input type="checkbox" checked={selected.has(r.index)} onChange={() => onToggle(r.index)} /></td>
                <td className="mono">{r.index}</td>
                <td className="num">{fmt(r.src_count)}</td>
                <td className="num">{fmt(r.tgt_count)}</td>
                <td className={`num ${r.gap > 100000 ? "gap-hi" : r.gap > 1000 ? "gap-mid" : "gap-lo"}`}>{fmt(r.gap)}</td>
                <td>
                  {r.source_only
                    ? <span className="tag so">SOURCE-ONLY</span>
                    : r.target_alias
                      ? <span className="tag alias" title={`target alias → ${r.target_alias}`}>ALIAS</span>
                      : <span className="tag gap">GAP</span>}
                </td>
              </tr>
            ))}
            {visible.length === 0 && <tr><td colSpan={6} className="muted" style={{ padding: 14, textAlign: "center" }}>no matches</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="muted mt" style={{ fontSize: 12 }}>
        {selected.size} of {rows.length} selected · selected gap {fmt(selectedGap)} docs
      </div>
    </div>
  );
}
