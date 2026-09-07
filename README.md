# OpenSearch Migration Tool

Standalone web UI + API for reindexing indices between two OpenSearch /
Elasticsearch clusters (e.g. `qelsdata01.p01` → `qosdata01.p01`). It wraps the
battle-tested CLI engine `server/engine/opensearch_migrate.py` and adds:

- cluster connectivity test, mismatch discovery (doc-count diff, alias-aware)
- pick exactly which indices to reindex, dry-run first
- **one-click "Migrate ALL"** — auto-discovers + reindexes every mismatch in one run
- live terminal (SSE) with colour-coded log, cancel button, per-run summary
- run history that survives server restarts (JSON + log files, no database)
- **cluster presets** — save/load named cluster pairs (localStorage)
- **pre-flight checks** — source/target reachability, cluster health, disk space
- **mapping sync** — PUT source mapping onto existing target indices before reindex
- **settings copy** — copy source index settings (analyzers, shards) for new indices
- **template sync** — copy index templates (legacy + composable) from source to target
- **alias sync** — recreate source aliases on target after reindex
- **optional auth** — works with no-auth clusters too
- **light + purple theme** — clean, modern UI

It is **independent of the Platform Health Portal** — its own server, port,
UI and dependencies. Nothing here imports from `backend/` or `frontend/`.

## Safety model (inherited from the engine)

| Guarantee | How |
|---|---|
| Source is never written | Only `GET`/`_search`/`_count` against source; reindex runs *on the target* pulling from source via `remote` |
| Never deletes on target | `op_type=index` (create/update only), `conflicts=proceed` |
| Aborts on shrinkage | Target doc count checked before/after; any decrease is flagged `DATA LOSS` and the index is not retried |
| Source protection | Pauses when source heap > 85 %, search queue > 10, unassigned shards > 600, or cluster RED (all tunable) |
| Mapping conflicts | Detected and skipped, not retried (needs a v4 index + alias swap — separate process) |
| Resumable | `start_from` skips already-processed indices; circuit-breaker hits retry with smaller batch/rps |
| **Mapping sync** (`--sync-mapping`) | PUTs source mapping onto existing target indices before reindex. Adds new fields; field-type changes are detected and logged as conflicts (ES/OS won't allow changing an existing field type in-place) |
| **Settings copy** (`--copy-settings`) | For source-only indices, copies source index settings (analyzers, shard/replica count) instead of defaulting to 5 shards / 1 replica. Read-only keys (uuid, creation_date, version…) are stripped automatically |
| **Template sync** (`--sync-templates`) | Copies legacy (`_template`) + composable (`_index_template`) templates from source to target before reindex. Skips built-in (dot-prefixed) templates. Strips read-only settings from composable template settings |
| **Alias sync** (`--sync-aliases`) | After reindexing, recreates source aliases on target via `_aliases` API. Only creates aliases that don't already exist on target (won't overwrite) |

Dry run is **on by default** in the UI. A live run requires a second click to confirm.

## Quick start

### One-click setup (Windows)

```powershell
cd os-migration-tool
.\setup.ps1
# Creates venv, installs deps, builds UI, starts server, opens browser
# -> http://127.0.0.1:8020
```

Options: `.\setup.ps1 -Port 9090` or `.\setup.ps1 -NoStart` (setup only).

### Docker

```bash
docker compose up
# Builds UI (Node stage) + server (Python stage), starts on :8020
# Run history persists in a named volume (osmt-data)
```

### Manual setup

```powershell
# 1. server (Python 3.11+)
cd os-migration-tool\server
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py          # -> http://127.0.0.1:8020

# 2. UI — either use the pre-built bundle (served by the server at :8020) ...
cd ..\ui
npm install
npm run build                            # writes ui/dist; restart app.py to pick it up

# ... or run the dev server with hot reload (proxies /api to :8020)
npm run dev                              # -> http://127.0.0.1:5180
```

Linux/macOS: same, with `.venv/bin/python`.

Environment overrides for the server:

| Var | Default | Purpose |
|---|---|---|
| `OSMT_HOST` / `OSMT_PORT` | `127.0.0.1` / `8020` | bind address (use `0.0.0.0` to share on the LAN — see Security) |
| `OSMT_DATA_DIR` | `server/data` | run history (`runs.json`) + per-run logs/results |
| `OSMT_ENGINE_PATH` | `server/engine/opensearch_migrate.py` | use a different engine copy |
| `OSMT_PLAN_TIMEOUT` | `600` | seconds allowed for discovery |
| `OSMT_API_URL` (UI dev only) | `http://127.0.0.1:8020` | where `npm run dev` proxies `/api` |

## Using the UI

1. **Clusters** — enter source and target URL + `user:password` (auth is
   **optional** — leave empty for no-auth clusters), click **Test** on each.
   You should see cluster name, version, health colour, node and index counts.
   - **Presets**: save named cluster pairs (e.g. "p01 qels→qos") using the
     input at the bottom of the Clusters card. Load them from the dropdown
     with one click. Presets persist in the browser's localStorage.
   - Cluster URLs, auth, and run options are automatically saved and restored
     on page reload — no re-entering after refresh.
2. **Choose indices** — three modes (dropdown):
   - *Use discovered list* (default): click **Discover mismatches**. The engine
     compares `_cat/indices` doc counts on both sides, resolves source names
     that exist only as **aliases** on the target (so `-migrated` + alias
     patterns are not reported as missing), and lists every index where
     target < source. Filter, sort by gap, tick the ones you want.
     Tags: `GAP` (exists both sides, target behind), `ALIAS` (target has it via
     alias), `SOURCE-ONLY` (does not exist on target — will be created with the
     source mapping, 5 shards / 1 replica).
   - *Type index names*: paste a comma/newline separated list.
   - *Let engine discover at run*: the run itself discovers and processes
     **every** mismatched index, smallest gap first.
3. **Run** — keep **Dry run** ticked for the first pass; it prints the plan and
   exits. Untick for a live run (button turns green, needs a confirming second
   click). `RPS limit` and `Pause (s)` between indices throttle load on the
   source; **advanced** exposes the heap / queue / unassigned thresholds and
   `Start from` for resuming an interrupted run at index *N*.
   - **Pre-flight checks** — click to test source/target reachability, cluster
     health (RED warning), and target disk space (>85% warning) before starting.
   - **Migrate ALL** — one-click button that auto-discovers all mismatched
     indices and reindexes them all in a single run. No need to manually
     discover + select + start.
   - **Sync mapping** — for indices that already exist on the target, PUT the
     source mapping before reindexing. This adds any new fields the source has
     that the target doesn't. If a field *type* differs (e.g. source `keyword`,
     target `text`), ES/OpenSearch rejects the change — the conflict is logged
     and the index proceeds to reindex (which may also fail with a mapping
     conflict, the authoritative signal).
   - **Copy settings** — for source-only indices (not yet on target), copy the
     source's index settings (custom analyzers, shard count, replica count,
     refresh interval, etc.) instead of defaulting to 5 shards / 1 replica.
     Read-only keys like `uuid`, `creation_date`, `version` are stripped.
   - **Sync templates** — copy index templates (legacy `_template` + composable
     `_index_template`) from source to target before reindexing. Skips
     built-in (dot-prefixed) templates. New indices will inherit these
     templates on creation.
   - **Sync aliases** — after reindexing, recreate source aliases on target.
     Only creates aliases that don't already exist on target (won't overwrite
     existing aliases).
4. **Terminal** (right) tails the run live. `Cancel` terminates the engine
   *and* sends `_tasks/<id>/_cancel` to the target for the in-flight reindex
   task. When a run ends the summary tiles show repaired / partial / skipped /
   mapping conflicts / data-loss alerts.
5. **History** lists previous runs; click one to replay its log.

Log and results files live under `server/data/logs/` (`run_<id>.log`,
`run_<id>_results.json`, `plan_<ts>.log`).

## API

Interactive docs at `http://127.0.0.1:8020/docs`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | server + engine sanity |
| `POST` | `/api/clusters/info` | `{url, auth}` → name/version/status/nodes/indices |
| `POST` | `/api/plan` | `{source_url, source_auth, target_url, target_auth}` → mismatches (alias-aware) |
| `POST` | `/api/preflight` | `{source_url, source_auth, target_url, target_auth}` → pre-flight checks (reachability, health, disk) |
| `POST` | `/api/runs` | start a run (202). Body = plan fields + `indices?`, `dry_run`, `rps`, `pause`, `heap_threshold`, `unassigned_threshold`, `queue_threshold`, `start_from`, `sync_mapping`, `copy_settings`, `sync_templates`, `sync_aliases` |
| `GET` | `/api/runs` | history, newest first |
| `GET` | `/api/runs/{id}` | status, exit code, summary |
| `GET` | `/api/runs/{id}/events?after=N` | poll log lines with `seq > N` |
| `GET` | `/api/runs/{id}/stream` | SSE tail (replays history, then live, ends with `{type:"end"}`) |
| `POST` | `/api/runs/{id}/cancel` | terminate engine + cancel target reindex task |

Example — dry-run two indices:

```powershell
$b = @{ source_url="http://qelsdata01.p01.eng.sjc01.qualys.com:50140"; source_auth="admin:***";
        target_url="http://qosdata01.p01.eng.sjc01.qualys.com:50140"; target_auth="admin:***";
        indices=@("exception-idx1-v1","etm-trending-idx1-v1"); dry_run=$true } | ConvertTo-Json
Invoke-RestMethod -Method POST http://127.0.0.1:8020/api/runs -ContentType application/json -Body $b
```

Example — live run with all sync options:

```powershell
$b = @{ source_url="http://SRC:50140"; source_auth="admin:***";
        target_url="http://TGT:50140"; target_auth="admin:***";
        dry_run=$false; sync_mapping=$true; copy_settings=$true;
        sync_templates=$true; sync_aliases=$true; rps=200 } | ConvertTo-Json
Invoke-RestMethod -Method POST http://127.0.0.1:8020/api/runs -ContentType application/json -Body $b
```

## CLI only (no UI)

The engine still works on its own:

```bash
python server/engine/opensearch_migrate.py \
  --source http://SRC:50140 --target http://TGT:50140 \
  --discover --dry-run                       # what would be done
python server/engine/opensearch_migrate.py \
  --source ... --target ... --indices idx-a,idx-b --rps 200 --pause 30
# With all sync options:
python server/engine/opensearch_migrate.py \
  --source ... --target ... --indices idx-a,idx-b \
  --sync-mapping --copy-settings --sync-templates --sync-aliases
```

Credentials: `--source-auth user:pass` / `--target-auth user:pass`, or the env
vars `OS_MIGRATE_SOURCE_AUTH` / `OS_MIGRATE_TARGET_AUTH` (the server uses the
env vars so passwords never appear in the process list). Auth is optional —
omit for no-auth clusters.

## Security notes

- Credentials are held in memory for the duration of a request/run only. They
  are **not** written to `runs.json`, logs, or results files (the engine logs
  the username only).
- Traffic between browser and server is plain HTTP. Default bind is loopback;
  if you expose it on the LAN (`OSMT_HOST=0.0.0.0`) put it behind TLS/auth.
- TLS certificate verification to the clusters is disabled (matches the
  engine; eng clusters use self-signed certs).

## Known limitations

- Doc-count comparison sees through Document-Level-Security filters
  differently on each side — an index can show a "gap" that is a DLS artefact
  (see `STATE.md` for the p01 examples). Verify with `_count` as the same user
  on both sides before assuming data is missing.
- Mapping conflicts are reported, not fixed. Fix = create a `v4` index with
  the source mapping, reindex into it, swap the alias.
- One engine process per run; runs execute concurrently if you start several.
  Be mindful of source load — each run monitors the source independently.
- Run history is a JSON file; fine for a team tool, not for many thousands of runs.
- Template sync copies templates as-is; if the target already has a template
  with the same name, it will be overwritten. Review template conflicts before
  enabling this option.

## Layout

```
os-migration-tool/
├── README.md
├── setup.ps1               one-click Windows setup
├── Dockerfile              multi-stage build (Node UI + Python server)
├── docker-compose.yml      containerized deployment with persistent volume
├── .dockerignore
├── server/
│   ├── app.py              FastAPI app: REST + SSE + serves ui/dist
│   ├── runner.py           run registry, thread-based subprocess driver, persistence
│   ├── requirements.txt    fastapi, uvicorn, pydantic
│   ├── engine/opensearch_migrate.py   the reindex engine (CLI)
│   └── data/               runs.json + logs/ (git-ignored)
└── ui/                     React + Vite + TypeScript (port 5180 in dev)
    └── src/
        ├── App.tsx                 3-step flow + terminal + history + presets
        ├── api.ts / types.ts       REST + SSE client
        └── components/             ClusterCard, PlanTable, Terminal, RunHistory
```

## Why not inside the portal?

The first version lived inside the Platform Health Portal (`/migration`
page). It failed on Windows with `502 {"detail": ""}` because uvicorn
`--reload` forces `WindowsSelectorEventLoopPolicy`, under which
`asyncio.create_subprocess_exec` raises an empty `NotImplementedError`. This
tool uses a plain `subprocess.Popen` + reader thread, which works on every
platform and event loop, and keeps the migration workflow (long-running,
credential-bearing, write-capable) separate from the read-only health portal.
