import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api";
import { ClusterCard } from "./components/ClusterCard";
import { PlanTable } from "./components/PlanTable";
import { RunHistory } from "./components/RunHistory";
import { Terminal } from "./components/Terminal";
import type { IndexMismatch, LogEvent, PlanResponse, Run, RunOptions } from "./types";

const DEFAULT_OPTS: RunOptions = { rps: 200, pause: 30, heap_threshold: 85, unassigned_threshold: 600, queue_threshold: 10, start_from: 1 };

const parseManual = (s: string) => s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

// localStorage helpers — persist cluster URLs + auth + run options across reloads
const LS_KEY = "osmt:clusters";
const LS_PRESETS = "osmt:presets";
type SavedState = { src: { url: string; auth: string }; tgt: { url: string; auth: string }; opts: RunOptions };
type Preset = { name: string; src: { url: string; auth: string }; tgt: { url: string; auth: string } };

function loadSaved(): Partial<SavedState> {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch { return {}; }
}
function saveSaved(s: Partial<SavedState>) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(s)); } catch { /* ignore quota */ }
}
function loadPresets(): Preset[] {
  try { return JSON.parse(localStorage.getItem(LS_PRESETS) || "[]"); } catch { return []; }
}
function savePresets(p: Preset[]) {
  try { localStorage.setItem(LS_PRESETS, JSON.stringify(p)); } catch { /* ignore */ }
}

export default function App() {
  const _saved = loadSaved();

  // step 1 — clusters (restored from localStorage on first render)
  const [src, setSrc] = useState(_saved.src ?? { url: "", auth: "" });
  const [tgt, setTgt] = useState(_saved.tgt ?? { url: "", auth: "" });

  // step 2 — plan
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planErr, setPlanErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<"plan" | "manual" | "discover">("plan");
  const [manual, setManual] = useState("");

  // step 3 — run (opts also restored from localStorage)
  const [opts, setOpts] = useState<RunOptions>(_saved.opts ?? DEFAULT_OPTS);
  const [dryRun, setDryRun] = useState(true);
  const [showAdv, setShowAdv] = useState(false);
  const [armed, setArmed] = useState(false);
  const [startErr, setStartErr] = useState<string | null>(null);

  // cluster presets
  const [presets, setPresets] = useState<Preset[]>(loadPresets);
  const [presetName, setPresetName] = useState("");

  // preflight
  const [preflight, setPreflight] = useState<{ ok: boolean; checks: { name: string; ok: boolean; detail: string }[] } | null>(null);
  const [preflighting, setPreflighting] = useState(false);

  // terminal / history
  const [run, setRun] = useState<Run | null>(null);
  const [lines, setLines] = useState<LogEvent[]>([]);
  const [live, setLive] = useState(false);
  const [runs, setRuns] = useState<Run[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const refreshRuns = useCallback(() => { api.listRuns().then(setRuns).catch(() => undefined); }, []);
  useEffect(refreshRuns, [refreshRuns]);

  // Persist cluster URLs + opts to localStorage on every change (auth is optional)
  useEffect(() => { saveSaved({ src, tgt, opts }); }, [src, tgt, opts]);

  // Preset handlers
  function savePreset() {
    const name = presetName.trim();
    if (!name || !src.url || !tgt.url) return;
    const updated = [...presets.filter((p) => p.name !== name), { name, src, tgt }];
    setPresets(updated); savePresets(updated); setPresetName("");
  }
  function loadPreset(name: string) {
    const p = presets.find((x) => x.name === name);
    if (!p) return;
    setSrc(p.src); setTgt(p.tgt);
  }
  function deletePreset(name: string) {
    const updated = presets.filter((p) => p.name !== name);
    setPresets(updated); savePresets(updated);
  }

  async function doPreflight() {
    setPreflighting(true); setPreflight(null);
    try {
      const r = await api.preflight({ source_url: src.url, source_auth: src.auth, target_url: tgt.url, target_auth: tgt.auth });
      setPreflight(r);
    } catch (e) {
      setPreflight({ ok: false, checks: [{ name: "error", ok: false, detail: e instanceof Error ? e.message : String(e) }] });
    } finally {
      setPreflighting(false);
    }
  }

  /** One-click: discover mismatches + start a live run for ALL of them. */
  async function doMigrateAll() {
    if (!dryRun && !armed) { setArmed(true); setTimeout(() => setArmed(false), 6000); return; }
    setArmed(false); setStartErr(null);
    try {
      // Start with auto-discover mode — engine will find and reindex all mismatches
      const r = await api.startRun({
        source_url: src.url, source_auth: src.auth, target_url: tgt.url, target_auth: tgt.auth,
        indices: undefined, dry_run: dryRun, ...opts,
      });
      attach(r);
      refreshRuns();
    } catch (e) {
      setStartErr(e instanceof Error ? e.message : String(e));
    }
  }

  const ready = !!(src.url && tgt.url);  // auth is optional
  const busy = run?.status === "running" || run?.status === "pending";

  async function doPlan() {
    setPlanning(true); setPlanErr(null); setPlan(null); setSelected(new Set());
    try {
      const p = await api.plan({ source_url: src.url, source_auth: src.auth, target_url: tgt.url, target_auth: tgt.auth });
      setPlan(p);
      setSelected(new Set(p.mismatches.map((m: IndexMismatch) => m.index)));
      setMode("plan");
    } catch (e) {
      setPlanErr(e instanceof Error ? e.message : String(e));
    } finally {
      setPlanning(false);
    }
  }

  function indicesForRun(): string[] | undefined {
    if (mode === "discover") return undefined;
    if (mode === "manual") return parseManual(manual);
    return Array.from(selected);
  }
  const indexCount = mode === "discover" ? null : (indicesForRun()?.length ?? 0);
  const canStart = ready && !busy && (mode === "discover" || (indexCount ?? 0) > 0);

  async function doStart() {
    if (!dryRun && !armed) { setArmed(true); setTimeout(() => setArmed(false), 6000); return; }
    setArmed(false); setStartErr(null);
    try {
      const r = await api.startRun({
        source_url: src.url, source_auth: src.auth, target_url: tgt.url, target_auth: tgt.auth,
        indices: indicesForRun(), dry_run: dryRun, ...opts,
      });
      attach(r);
      refreshRuns();
    } catch (e) {
      setStartErr(e instanceof Error ? e.message : String(e));
    }
  }

  /** Attach the terminal to a run: replay history, then tail live until it ends. */
  function attach(r: Run) {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setRun(r); setLines([]);
    (async () => {
      setLive(true);
      try {
        for await (const ev of api.stream(r.run_id, ctrl.signal)) {
          if ("type" in ev) {
            const final = await api.getRun(r.run_id).catch(() => null);
            if (final) setRun(final);
            break;
          }
          setLines((prev) => [...prev, ev]);
          if (ev.message.includes("Task ID:")) api.getRun(r.run_id).then(setRun).catch(() => undefined);
        }
      } catch {
        /* aborted (user picked another run) or network drop */
      } finally {
        if (!ctrl.signal.aborted) { setLive(false); refreshRuns(); }
      }
    })();
  }
  useEffect(() => () => abortRef.current?.abort(), []);

  async function doCancel() {
    if (!run) return;
    try { await api.cancelRun(run.run_id); } catch (e) { setStartErr(e instanceof Error ? e.message : String(e)); }
  }

  return (
    <div className="shell">
      <header className="topbar">
        <h1>OpenSearch Migration Tool</h1>
        <span className="sub">cross-cluster reindex · op_type=index · source is never written · resumable</span>
        <span className="spacer" />
        <a href="/docs" target="_blank" rel="noreferrer" className="muted" style={{ fontSize: 12 }}>API docs</a>
      </header>

      <div className="main">
        {/* LEFT: steps */}
        <div className="col">
          <section className="card">
            <h2><span className="step">1</span> Clusters</h2>
            {/* Preset dropdown */}
            {presets.length > 0 && (
              <div className="row" style={{ marginBottom: 10 }}>
                <select className="fixed" style={{ width: 200 }} value="" onChange={(e) => { if (e.target.value) loadPreset(e.target.value); }}>
                  <option value="">Load preset…</option>
                  {presets.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
                </select>
                {presets.map((p) => (
                  <button key={p.name} className="btn btn-sm fixed" title={`Delete preset "${p.name}"`}
                    onClick={() => deletePreset(p.name)} style={{ padding: "4px 8px" }}>x</button>
                )).slice(-1)}
              </div>
            )}
            <ClusterCard label="Source" url={src.url} auth={src.auth} disabled={busy} onChange={(url, auth) => setSrc({ url, auth })} />
            <div style={{ height: 10 }} />
            <ClusterCard label="Target" url={tgt.url} auth={tgt.auth} disabled={busy} onChange={(url, auth) => setTgt({ url, auth })} />
            {/* Save preset */}
            <div className="row mt">
              <input placeholder="preset name (e.g. p01 qels→qos)" value={presetName} disabled={busy || !ready}
                onChange={(e) => setPresetName(e.target.value)} />
              <button className="btn btn-sm fixed" onClick={savePreset} disabled={!presetName.trim() || !ready}>Save preset</button>
            </div>
          </section>

          <section className="card">
            <h2><span className="step">2</span> Choose indices</h2>
            <div className="row">
              <button className="btn btn-primary" onClick={doPlan} disabled={!ready || planning || busy}>
                {planning ? "Comparing clusters…" : "Discover mismatches"}
              </button>
              <select className="fixed" value={mode} onChange={(e) => setMode(e.target.value as typeof mode)} style={{ width: 190 }}>
                <option value="plan">Use discovered list</option>
                <option value="manual">Type index names</option>
                <option value="discover">Let engine discover at run</option>
              </select>
            </div>
            {planErr && <div className="error">{planErr}</div>}
            {plan && mode === "plan" && (
              <div className="mt">
                <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
                  {plan.mismatches.length} mismatched · total gap {plan.total_gap.toLocaleString()} docs · {plan.source_only_count} source-only · {plan.duration_s}s
                </div>
                <PlanTable rows={plan.mismatches} selected={selected}
                  onToggle={(n) => setSelected((p) => { const s = new Set(p); if (s.has(n)) s.delete(n); else s.add(n); return s; })}
                  onSetAll={(names) => setSelected(new Set(names))} />
              </div>
            )}
            {mode === "manual" && (
              <div className="mt field">
                <label>Index names (comma / newline separated)</label>
                <textarea value={manual} onChange={(e) => setManual(e.target.value)} placeholder={"asset-idx149-v3\nuai-storage-idx1-v2"} />
                <div className="muted" style={{ fontSize: 12 }}>{parseManual(manual).length} indices</div>
              </div>
            )}
            {mode === "discover" && (
              <div className="banner info mt">The engine will compare doc counts itself when the run starts and reindex <b>every</b> mismatched index (smallest gap first).</div>
            )}
          </section>

          <section className="card">
            <h2><span className="step">3</span> Run</h2>
            {/* Pre-flight checks */}
            <div className="row" style={{ marginBottom: 10 }}>
              <button className="btn btn-sm fixed" onClick={doPreflight} disabled={!ready || preflighting || busy}>
                {preflighting ? "Checking…" : "Pre-flight checks"}
              </button>
              {preflight && (
                <span className="muted" style={{ fontSize: 12 }}>
                  {preflight.ok ? "All checks passed" : `${preflight.checks.filter((c) => !c.ok).length} warning(s)`}
                </span>
              )}
            </div>
            {preflight && (
              <div className="mt" style={{ marginBottom: 10 }}>
                {preflight.checks.map((c) => (
                  <div key={c.name} style={{ display: "flex", gap: 8, fontSize: 12, padding: "3px 0" }}>
                    <span className={`dot ${c.ok ? "green" : "red"}`} style={{ marginTop: 5 }} />
                    <span className="mono" style={{ flex: "0 0 130px" }}>{c.name}</span>
                    <span className={c.ok ? "muted" : ""} style={{ color: c.ok ? undefined : "var(--red)" }}>{c.detail}</span>
                  </div>
                ))}
              </div>
            )}
            {/* Quick migrate all */}
            <div className="row" style={{ marginBottom: 10 }}>
              <button className="btn btn-primary" onClick={doMigrateAll} disabled={!ready || busy}
                title="Auto-discover all mismatched indices and reindex them all in one run">
                {busy ? "Run in progress…"
                  : dryRun ? "Dry run ALL (auto-discover)"
                  : armed ? "Click again to confirm LIVE migrate all"
                  : "Migrate ALL (one click)"}
              </button>
              <span className="muted" style={{ fontSize: 11 }}>discovers + reindexes every mismatch</span>
            </div>
            <div className="row">
              <label className="row fixed" style={{ gap: 6, cursor: "pointer" }}>
                <input type="checkbox" checked={dryRun} onChange={(e) => { setDryRun(e.target.checked); setArmed(false); }} /> Dry run (no writes)
              </label>
              <div className="field fixed" style={{ width: 110 }}>
                <label>RPS limit</label>
                <input type="number" value={opts.rps} min={1} onChange={(e) => setOpts({ ...opts, rps: Number(e.target.value) })} />
              </div>
              <div className="field fixed" style={{ width: 110 }}>
                <label>Pause (s)</label>
                <input type="number" value={opts.pause} min={0} onChange={(e) => setOpts({ ...opts, pause: Number(e.target.value) })} />
              </div>
              <span className="spacer" />
              <button className="btn btn-sm fixed" onClick={() => setShowAdv((s) => !s)}>{showAdv ? "hide" : "advanced"}</button>
            </div>
            <div className="row mt" style={{ gap: 16 }}>
              <label className="row fixed" style={{ gap: 6, cursor: "pointer" }} title="For existing target indices: PUT the source mapping before reindexing. Adds new fields; field-type conflicts are detected and logged.">
                <input type="checkbox" checked={!!opts.sync_mapping} onChange={(e) => setOpts({ ...opts, sync_mapping: e.target.checked })} /> Sync mapping
              </label>
              <label className="row fixed" style={{ gap: 6, cursor: "pointer" }} title="For source-only indices: copy source index settings (analyzers, shard/replica count) instead of defaulting to 5 shards / 1 replica.">
                <input type="checkbox" checked={!!opts.copy_settings} onChange={(e) => setOpts({ ...opts, copy_settings: e.target.checked })} /> Copy settings
              </label>
            </div>
            <div className="row mt" style={{ gap: 16 }}>
              <label className="row fixed" style={{ gap: 6, cursor: "pointer" }} title="Copy index templates (legacy + composable) from source to target before reindexing. Skips built-in templates.">
                <input type="checkbox" checked={!!opts.sync_templates} onChange={(e) => setOpts({ ...opts, sync_templates: e.target.checked })} /> Sync templates
              </label>
              <label className="row fixed" style={{ gap: 6, cursor: "pointer" }} title="After reindexing, recreate source aliases on target. Only creates aliases that don't already exist on target.">
                <input type="checkbox" checked={!!opts.sync_aliases} onChange={(e) => setOpts({ ...opts, sync_aliases: e.target.checked })} /> Sync aliases
              </label>
            </div>
            {showAdv && (
              <div className="grid2 mt">
                <div className="field"><label>Source heap max %</label>
                  <input type="number" value={opts.heap_threshold} onChange={(e) => setOpts({ ...opts, heap_threshold: Number(e.target.value) })} /></div>
                <div className="field"><label>Source search-queue max</label>
                  <input type="number" value={opts.queue_threshold} onChange={(e) => setOpts({ ...opts, queue_threshold: Number(e.target.value) })} /></div>
                <div className="field"><label>Source unassigned-shards max</label>
                  <input type="number" value={opts.unassigned_threshold} onChange={(e) => setOpts({ ...opts, unassigned_threshold: Number(e.target.value) })} /></div>
                <div className="field"><label>Start from (1-based, resume)</label>
                  <input type="number" value={opts.start_from} min={1} onChange={(e) => setOpts({ ...opts, start_from: Number(e.target.value) })} /></div>
              </div>
            )}
            {!dryRun && (
              <div className="banner warn mt">
                LIVE run: writes to <b>{tgt.url || "target"}</b> with op_type=index (create/update only — never deletes). Source is read-only.
                The engine pauses automatically if the source cluster is under pressure.
              </div>
            )}
            <div className="row mt">
              <button className={`btn ${dryRun ? "btn-warn" : armed ? "btn-danger" : "btn-go"}`} onClick={doStart} disabled={!canStart}>
                {busy ? "Run in progress…"
                  : dryRun ? `Dry run${indexCount !== null ? ` ${indexCount} indices` : ""}`
                  : armed ? `Click again to confirm live reindex${indexCount !== null ? ` of ${indexCount}` : ""}`
                  : `Reindex${indexCount !== null ? ` ${indexCount} indices` : " (auto-discover)"}`}
              </button>
            </div>
            {startErr && <div className="error">{startErr}</div>}
          </section>
        </div>

        {/* RIGHT: terminal + history */}
        <div className="col">
          <Terminal run={run} lines={lines} live={live} onCancel={doCancel} onClear={() => setLines([])} />
          <RunHistory runs={runs} activeId={run?.run_id ?? null} onRefresh={refreshRuns}
            onSelect={(r) => { if (r.run_id !== run?.run_id) attach(r); }} />
        </div>
      </div>
    </div>
  );
}
