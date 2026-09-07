import { useState } from "react";
import { api } from "../api";
import type { ClusterInfo } from "../types";

interface Props {
  label: "Source" | "Target";
  url: string;
  auth: string;
  onChange: (url: string, auth: string) => void;
  disabled?: boolean;
}

export function ClusterCard({ label, url, auth, onChange, disabled }: Props) {
  const [info, setInfo] = useState<ClusterInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [show, setShow] = useState(false);

  async function test() {
    setBusy(true);
    setErr(null);
    setInfo(null);
    try {
      setInfo(await api.clusterInfo(url, auth));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="cluster">
      <div className="title">{label} {label === "Source" ? "(read-only)" : "(receives writes)"}</div>
      <div className="field">
        <label>URL</label>
        <input value={url} disabled={disabled} placeholder="http://host:9200" spellCheck={false}
          onChange={(e) => { onChange(e.target.value, auth); setInfo(null); }} />
      </div>
      <div className="row mt">
        <div className="field">
          <label>Basic auth (user:password) — optional</label>
          <input value={auth} disabled={disabled} type={show ? "text" : "password"} spellCheck={false}
            placeholder="leave empty for no-auth clusters"
            onChange={(e) => { onChange(url, e.target.value); setInfo(null); }} />
        </div>
        <button className="btn btn-sm fixed" style={{ marginTop: 16 }} onClick={() => setShow((s) => !s)}>
          {show ? "hide" : "show"}
        </button>
        <button className="btn btn-sm fixed" style={{ marginTop: 16 }} onClick={test} disabled={busy || disabled || !url}>
          {busy ? "Testing…" : "Test"}
        </button>
      </div>
      {info && (
        <div>
          <span className="chip"><span className={`dot ${info.status ?? "grey"}`} />{info.cluster_name ?? "?"}</span>
          <span className="chip">{info.distribution} {info.version}</span>
          <span className="chip">{info.nodes} nodes</span>
          <span className="chip">{info.indices} indices</span>
          {!!info.unassigned_shards && <span className="chip" style={{ color: "var(--amber)" }}>{info.unassigned_shards} unassigned</span>}
        </div>
      )}
      {err && <div className="error">{err}</div>}
    </div>
  );
}
