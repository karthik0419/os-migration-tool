# OpenSearch Migration Tool

Standalone web UI + API for reindexing indices between two OpenSearch /
Elasticsearch clusters (e.g. `qelsdata01.p01` → `qosdata01.p01`). It wraps the
battle-tested CLI engine `server/engine/opensearch_migrate.py` and adds:

- cluster connectivity test, mismatch discovery (doc-count diff, alias-aware)
- pick exactly which indices to reindex, dry-run first
- live terminal (SSE) with colour-coded log, cancel button, per-run summary
- run history that survives server restarts (JSON + log files, no database)

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

Dry run is **on by default** in the UI. A live run requires a second click to confirm.

## Quick start (Windows)

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

1. **Clusters** — enter source and target URL + `user:password`, click **Test**
   on each. You should see cluster name, version, health colour, node and index
   counts. Fix connectivity here before going further.
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
   Two optional toggles below the RPS/Pause row:
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
| `POST` | `/api/runs` | start a run (202). Body = plan fields + `indices?`, `dry_run`, `rps`, `pause`, `heap_threshold`, `unassigned_threshold`, `queue_threshold`, `start_from`, `sync_mapping`, `copy_settings` |
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

## CLI only (no UI)

The engine still works on its own:

```bash
python server/engine/opensearch_migrate.py \
  --source http://SRC:50140 --target http://TGT:50140 \
  --discover --dry-run                       # what would be done
python server/engine/opensearch_migrate.py \
  --source ... --target ... --indices idx-a,idx-b --rps 200 --pause 30
# With mapping sync + settings copy:
python server/engine/opensearch_migrate.py \
  --source ... --target ... --indices idx-a,idx-b --sync-mapping --copy-settings
```

Credentials: `--source-auth user:pass` / `--target-auth user:pass`, or the env
vars `OS_MIGRATE_SOURCE_AUTH` / `OS_MIGRATE_TARGET_AUTH` (the server uses the
env vars so passwords never appear in the process list).

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

## Layout

```
os-migration-tool/
├── README.md
├── server/
│   ├── app.py              FastAPI app: REST + SSE + serves ui/dist
│   ├── runner.py           run registry, thread-based subprocess driver, persistence
│   ├── requirements.txt    fastapi, uvicorn, pydantic
│   ├── engine/opensearch_migrate.py   the reindex engine (CLI)
│   └── data/               runs.json + logs/ (git-ignored)
└── ui/                     React + Vite + TypeScript (port 5180 in dev)
    └── src/
        ├── App.tsx                 3-step flow + terminal + history
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
