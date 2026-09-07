"""Run registry + subprocess runner for the OpenSearch migration engine.

Runs `engine/opensearch_migrate.py` in a plain `subprocess.Popen` driven by a
reader thread (NOT asyncio subprocesses — those raise NotImplementedError on
Windows under the selector event loop that uvicorn --reload forces).

Each run gets:
  - a ring buffer of log events (for live SSE tailing)
  - a log file + results file under DATA_DIR/logs (survives restarts)
  - a persisted record in DATA_DIR/runs.json (no database needed)

Credentials are passed to the engine via environment variables and are never
persisted or logged.
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent
ENGINE = Path(os.environ.get("OSMT_ENGINE_PATH", BASE_DIR / "engine" / "opensearch_migrate.py"))
DATA_DIR = Path(os.environ.get("OSMT_DATA_DIR", BASE_DIR / "data"))
LOG_DIR = DATA_DIR / "logs"
RUNS_FILE = DATA_DIR / "runs.json"

MAX_EVENTS = 5000
PLAN_TIMEOUT_S = int(os.environ.get("OSMT_PLAN_TIMEOUT", "600"))

# Order matters in classify(): success → error → warn. Patterns are anchored on the
# engine's actual alert phrasing so banner/summary lines ("NO data loss",
# "Mapping conflicts: 0") don't light up red.
_RE_ERROR = re.compile(
    r"\bERROR\b|DATA LOSS:|MAPPING CONFLICT detected|MAPPING CONFLICTS DETECTED|FAILURE:|Failed to|"
    r"Could not|TIMEOUT after|STILL PARTIAL|Data loss alerts:\s*[1-9]|Mapping conflicts:\s*[1-9]")
_RE_WARN = re.compile(r"\bWARN\b|Skipping|Still partial|pressure|NOT safe|Retry \d|CIRCUIT BREAKER|"
                      r"Will retry|Target already ahead|cancel", re.IGNORECASE)
_RE_SUCCESS = re.compile(r"COMPLETED:|REPAIRED:|MIGRATION COMPLETE|Created index|Run completed")
_RE_TASK = re.compile(r"Task ID:\s*(\S+)")
_RE_DISCOVERED = re.compile(r"Discovered indices saved:\s*(.+)$")
_RE_DRYRUN_LINE = re.compile(r"\s*\[\d+/\d+\]\s+(\S+)\s*(\(SOURCE-ONLY\))?\s*src=(\d+)\s+gap=(-?\d+)")

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify(line: str) -> str:
    if _RE_SUCCESS.search(line):
        return "success"
    if _RE_ERROR.search(line):
        return "error"
    if _RE_WARN.search(line):
        return "warn"
    return "info"


def _engine_env(source_auth: str, target_auth: str) -> dict[str, str]:
    env = dict(os.environ)
    env["OS_MIGRATE_SOURCE_AUTH"] = source_auth
    env["OS_MIGRATE_TARGET_AUTH"] = target_auth
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ---------------------------------------------------------------------------
# Cluster info (used by the UI "Test connection" button)
# ---------------------------------------------------------------------------
def cluster_info(url: str, auth: str, timeout: int = 15) -> dict[str, Any]:
    url = url.rstrip("/")
    headers = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode()}

    def get(path: str) -> Any:
        req = urllib.request.Request(url + path, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as r:
            return json.loads(r.read().decode())

    try:
        root = get("/")
        health = get("/_cluster/health")
        indices = get("/_cat/indices?h=index&format=json")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url}{e.url[len(url):] if e.url else ''}: {e.reason}")
    except Exception as e:  # noqa: BLE001 — surface the raw network error to the UI
        raise RuntimeError(f"Could not reach {url}: {e}")

    version = root.get("version", {})
    return {
        "cluster_name": root.get("cluster_name") or health.get("cluster_name"),
        "distribution": version.get("distribution", "elasticsearch"),
        "version": version.get("number"),
        "status": health.get("status"),
        "nodes": health.get("number_of_nodes"),
        "data_nodes": health.get("number_of_data_nodes"),
        "unassigned_shards": health.get("unassigned_shards"),
        "indices": len([i for i in indices if not i["index"].startswith(".")]),
    }


# ---------------------------------------------------------------------------
# Plan (discover) — synchronous, returns parsed mismatches
# ---------------------------------------------------------------------------
def plan(source_url: str, source_auth: str, target_url: str, target_auth: str) -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    results_file = LOG_DIR / f"plan_{stamp}_results.json"
    log_file = LOG_DIR / f"plan_{stamp}.log"
    cmd = [
        sys.executable, str(ENGINE),
        "--source", source_url, "--target", target_url,
        "--discover", "--dry-run",
        "--log-file", str(log_file), "--results-file", str(results_file),
    ]
    started = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_engine_env(source_auth, target_auth), timeout=PLAN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Discovery timed out after {PLAN_TIMEOUT_S}s")
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        tail = [l for l in output.splitlines() if l.strip()][-8:]
        raise RuntimeError(f"Engine exited {proc.returncode}: " + " | ".join(tail))

    mismatches: list[dict[str, Any]] = []
    # Preferred: the JSON file the engine writes next to the results file.
    m = _RE_DISCOVERED.search(output)
    disc_path = Path(m.group(1).strip()) if m else results_file.with_name(results_file.name.replace("_results.json", "_discovered.json"))
    if disc_path.exists():
        mismatches = json.loads(disc_path.read_text(encoding="utf-8"))
    else:
        # Fallback: parse the dry-run lines.
        for line in output.splitlines():
            dm = _RE_DRYRUN_LINE.search(line)
            if dm:
                src, gap = int(dm.group(3)), int(dm.group(4))
                mismatches.append({"index": dm.group(1), "src_count": src, "tgt_count": src - gap,
                                   "gap": gap, "source_only": bool(dm.group(2))})

    errors = [l for l in output.splitlines() if "ERROR" in l]
    return {
        "mismatches": mismatches,
        "total_gap": sum(x["gap"] for x in mismatches),
        "source_only_count": sum(1 for x in mismatches if x.get("source_only")),
        "duration_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "log_file": str(log_file),
        "warnings": errors,
    }


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
@dataclass
class Event:
    seq: int
    ts: str
    level: str
    message: str

    def to_sse(self) -> str:
        return f"data: {json.dumps(asdict(self))}\n\n"


@dataclass
class Run:
    run_id: str
    status: str  # pending | running | completed | failed | cancelled
    source_url: str
    target_url: str
    indices: list[str] | None
    dry_run: bool
    options: dict[str, Any]
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    summary: dict[str, Any] | None = None
    log_file: str = ""
    results_file: str = ""
    current_task: str | None = None
    # runtime-only (not persisted)
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS), repr=False)
    seq: int = field(default=0, repr=False)
    proc: subprocess.Popen | None = field(default=None, repr=False)
    target_auth: str | None = field(default=None, repr=False)
    cancel_requested: bool = field(default=False, repr=False)

    PUBLIC_FIELDS = ("run_id", "status", "source_url", "target_url", "indices", "dry_run", "options",
                     "started_at", "finished_at", "exit_code", "error", "summary", "log_file",
                     "results_file", "current_task")

    def public(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.PUBLIC_FIELDS}
        d["log_lines"] = len(self.events)
        return d

    def is_done(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._lock = threading.RLock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not RUNS_FILE.exists():
            return
        try:
            for rec in json.loads(RUNS_FILE.read_text(encoding="utf-8")):
                rec.pop("log_lines", None)
                run = Run(**rec)
                if not run.is_done():  # server died mid-run
                    run.status, run.error = "failed", "Server restarted while run was in progress"
                    run.finished_at = run.finished_at or _now()
                self._runs[run.run_id] = run
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[runner] could not load {RUNS_FILE}: {e}", file=sys.stderr)

    def _save(self) -> None:
        with self._lock:
            recs = [r.public() for r in self._runs.values()]
        tmp = RUNS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(recs, indent=2), encoding="utf-8")
        tmp.replace(RUNS_FILE)

    # -- queries -----------------------------------------------------------
    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list(self, limit: int = 50) -> list[Run]:
        runs = sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)
        return runs[:limit]

    def events_after(self, run: Run, after_seq: int) -> list[Event]:
        with self._lock:
            return [e for e in run.events if e.seq > after_seq]

    def replay_from_log(self, run: Run) -> Iterable[Event]:
        """For runs from a previous server process: rebuild events from the log file."""
        p = Path(run.log_file) if run.log_file else None
        if not p or not p.exists():
            return []
        out = []
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            ts = line[1:21] if line.startswith("[") else run.started_at
            out.append(Event(seq=i, ts=ts, level=classify(line), message=line))
        return out

    # -- lifecycle ---------------------------------------------------------
    def _emit(self, run: Run, level: str, message: str) -> None:
        with self._lock:
            run.seq += 1
            run.events.append(Event(seq=run.seq, ts=_now(), level=level, message=message))

    def start(self, *, source_url: str, source_auth: str, target_url: str, target_auth: str,
              indices: list[str] | None, dry_run: bool, options: dict[str, Any]) -> Run:
        run_id = uuid.uuid4().hex[:12]
        run = Run(
            run_id=run_id, status="pending", source_url=source_url, target_url=target_url,
            indices=indices or None, dry_run=dry_run, options=options, started_at=_now(),
            log_file=str(LOG_DIR / f"run_{run_id}.log"),
            results_file=str(LOG_DIR / f"run_{run_id}_results.json"),
            target_auth=target_auth,
        )
        with self._lock:
            self._runs[run_id] = run
        self._save()

        cmd = [
            sys.executable, str(ENGINE),
            "--source", source_url, "--target", target_url,
            "--heap-threshold", str(options.get("heap_threshold", 85)),
            "--unassigned-threshold", str(options.get("unassigned_threshold", 600)),
            "--queue-threshold", str(options.get("queue_threshold", 10)),
            "--pause", str(options.get("pause", 30)),
            "--rps", str(options.get("rps", 200)),
            "--start-from", str(options.get("start_from", 1)),
            "--log-file", run.log_file, "--results-file", run.results_file,
        ]
        if options.get("sync_mapping"):
            cmd.append("--sync-mapping")
        if options.get("copy_settings"):
            cmd.append("--copy-settings")
        cmd += ["--indices", ",".join(indices)] if indices else ["--discover"]
        if dry_run:
            cmd.append("--dry-run")

        t = threading.Thread(target=self._drive, args=(run, cmd, source_auth, target_auth),
                             name=f"osmt-run-{run_id}", daemon=True)
        t.start()
        return run

    def _drive(self, run: Run, cmd: list[str], source_auth: str, target_auth: str) -> None:
        self._emit(run, "info", f"Run {run.run_id} starting ({'DRY RUN' if run.dry_run else 'LIVE'})")
        self._emit(run, "info", f"Source: {run.source_url}")
        self._emit(run, "info", f"Target: {run.target_url}")
        self._emit(run, "info", "Indices: " + (", ".join(run.indices) if run.indices else "auto-discover"))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
                env=_engine_env(source_auth, target_auth),
            )
        except OSError as e:
            run.status, run.error, run.finished_at = "failed", f"Could not start engine: {e}", _now()
            self._emit(run, "error", run.error)
            self._save()
            return

        run.proc, run.status = proc, "running"
        self._save()

        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            tm = _RE_TASK.search(line)
            if tm:
                run.current_task = tm.group(1)
            elif "COMPLETED:" in line or "TIMEOUT after" in line:
                run.current_task = None
            self._emit(run, classify(line), line)
        proc.wait()

        run.exit_code = proc.returncode
        run.finished_at = _now()
        run.proc = None
        rf = Path(run.results_file)
        if rf.exists():
            try:
                run.summary = json.loads(rf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                run.summary = None

        if run.cancel_requested:
            status, level, msg = "cancelled", "warn", "Run cancelled by user"
        elif proc.returncode == 0:
            status, level, msg = "completed", "success", "Run completed (exit 0)"
        else:
            status, level, msg = "failed", "error", f"Engine exited with code {proc.returncode}"
            run.error = msg
        # Emit BEFORE flipping status so SSE tailers never miss the final line.
        self._emit(run, level, msg)
        run.status = status
        run.target_auth = None
        self._save()

    def cancel(self, run: Run) -> dict[str, Any]:
        """Terminate the engine process and try to cancel the in-flight reindex task on target."""
        if run.is_done() or run.proc is None:
            return {"cancelled": False, "reason": "run is not active"}
        run.cancel_requested = True
        task_cancel: str | None = None
        if run.current_task and run.target_auth:
            url = f"{run.target_url.rstrip('/')}/_tasks/{run.current_task}/_cancel"
            req = urllib.request.Request(url, method="POST", headers={
                "Authorization": "Basic " + base64.b64encode(run.target_auth.encode()).decode()})
            try:
                with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx) as r:
                    task_cancel = f"HTTP {r.getcode()}"
            except Exception as e:  # noqa: BLE001
                task_cancel = f"failed: {e}"
            self._emit(run, "warn", f"Cancel target task {run.current_task}: {task_cancel}")
        run.proc.terminate()
        return {"cancelled": True, "target_task": run.current_task, "target_task_cancel": task_cancel}


registry = RunRegistry()
