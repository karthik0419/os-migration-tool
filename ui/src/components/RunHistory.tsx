import type { Run } from "../types";

interface Props {
  runs: Run[];
  activeId: string | null;
  onSelect: (run: Run) => void;
  onRefresh: () => void;
}

const host = (u: string) => u.replace(/^https?:\/\//, "").split(/[:/]/)[0];

export function RunHistory({ runs, activeId, onSelect, onRefresh }: Props) {
  return (
    <div className="card">
      <h2>History <span className="spacer" style={{ flex: 1 }} /><button className="btn btn-sm" onClick={onRefresh}>refresh</button></h2>
      <div className="history">
        {runs.length === 0 && <div className="muted" style={{ fontSize: 12 }}>No runs yet.</div>}
        {runs.map((r) => (
          <div key={r.run_id} className={`hist-row ${r.run_id === activeId ? "active" : ""}`} onClick={() => onSelect(r)}>
            <span className="mono">{r.run_id}</span>
            <span className={`status ${r.status}`} style={{ justifySelf: "start" }}>{r.status}</span>
            <span className="mono muted" title={`${r.source_url} → ${r.target_url}`}>
              {host(r.source_url)} → {host(r.target_url)} · {r.indices ? `${r.indices.length} idx` : "discover"}{r.dry_run ? " · dry" : ""}
            </span>
            <span className="muted">{r.summary ? `${r.summary.repaired}/${r.summary.total}` : ""}</span>
            <span className="muted">{r.started_at.slice(0, 16).replace("T", " ")}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
