import { useEffect, useRef } from "react";
import type { LogEvent, Run } from "../types";

interface Props {
  run: Run | null;
  lines: LogEvent[];
  live: boolean;
  onCancel: () => void;
  onClear: () => void;
}

const LVL: Record<string, string> = { info: "INF", warn: "WRN", error: "ERR", success: " OK" };
const fmt = (n: number | null | undefined) => (n ?? 0).toLocaleString();

export function Terminal({ run, lines, live, onCancel, onClear }: Props) {
  const body = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    const el = body.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  function onScroll() {
    const el = body.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }

  const active = run && (run.status === "running" || run.status === "pending");

  return (
    <div className="terminal">
      <div className="term-head">
        <span className={`dot ${live ? "green" : "grey"}`} />
        <span className="mono">{run ? `run ${run.run_id}` : "no run selected"}</span>
        {run && <span className={`status ${run.status}`}>{run.status}</span>}
        {run?.dry_run && <span className="tag gap">DRY RUN</span>}
        {run?.current_task && <span className="muted mono" title="OpenSearch reindex task on target">task {run.current_task}</span>}
        <span className="spacer" />
        <span className="muted">{lines.length} lines</span>
        {active && <button className="btn btn-sm btn-danger" onClick={onCancel}>Cancel</button>}
        <button className="btn btn-sm" onClick={onClear}>clear</button>
      </div>
      <div className="term-body" ref={body} onScroll={onScroll}>
        {lines.length === 0 && <div className="muted">Start a run (or pick one from history) to see output here.</div>}
        {lines.map((l) => (
          <div key={l.seq} className={`line ${l.level}`}>
            <span className="ts">{l.ts.slice(11, 19)}</span>
            <span className="lvl">{LVL[l.level] ?? "INF"}</span>
            <span className="msg">{l.message}</span>
          </div>
        ))}
      </div>
      {run?.summary && (
        <div style={{ padding: "0 12px 12px" }}>
          <div className="summary">
            <div className="stat"><div className="k">Repaired</div><div className="v" style={{ color: "var(--green)" }}>{fmt(run.summary.repaired)}</div></div>
            <div className="stat"><div className="k">Partial</div><div className="v" style={{ color: run.summary.still_partial ? "var(--amber)" : undefined }}>{fmt(run.summary.still_partial)}</div></div>
            <div className="stat"><div className="k">Skipped</div><div className="v">{fmt(run.summary.skipped)}</div></div>
            <div className="stat"><div className="k">Mapping conflicts</div><div className="v" style={{ color: run.summary.mapping_conflicts ? "var(--red)" : undefined }}>{fmt(run.summary.mapping_conflicts)}</div></div>
            <div className="stat"><div className="k">Data-loss alerts</div><div className="v" style={{ color: run.summary.data_loss ? "var(--red)" : undefined }}>{fmt(run.summary.data_loss)}</div></div>
            <div className="stat"><div className="k">Total</div><div className="v">{fmt(run.summary.total)}</div></div>
          </div>
        </div>
      )}
      {run?.error && !run.summary && <div className="error" style={{ padding: "0 12px 12px" }}>{run.error}</div>}
    </div>
  );
}
