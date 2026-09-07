"""OpenSearch Migration Tool — standalone HTTP server.

    cd os-migration-tool/server
    python -m venv .venv && .venv\\Scripts\\pip install -r requirements.txt
    .venv\\Scripts\\python app.py            # http://127.0.0.1:8020

Serves the REST/SSE API under /api and, if ../ui/dist exists, the built UI.
Configuration via env: OSMT_HOST (127.0.0.1), OSMT_PORT (8020),
OSMT_DATA_DIR (./data), OSMT_ENGINE_PATH (./engine/opensearch_migrate.py).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import runner

app = FastAPI(title="OpenSearch Migration Tool", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ClusterRef(BaseModel):
    url: str = Field(..., examples=["http://qosdata01.p01.eng.sjc01.qualys.com:50140"])
    auth: str = Field(..., description="user:password")


class PlanRequest(BaseModel):
    source_url: str
    source_auth: str
    target_url: str
    target_auth: str


class RunRequest(PlanRequest):
    indices: list[str] | None = Field(None, description="Explicit index list; omit to auto-discover")
    dry_run: bool = False
    rps: int = 200
    pause: int = 30
    heap_threshold: int = 85
    unassigned_threshold: int = 600
    queue_threshold: int = 10
    start_from: int = 1
    sync_mapping: bool = Field(False, description="PUT source mapping onto existing target indices before reindex")
    copy_settings: bool = Field(False, description="For source-only indices, copy source settings (analyzers/shards) instead of 5/1 default")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "engine": str(runner.ENGINE), "engine_exists": runner.ENGINE.exists(),
            "data_dir": str(runner.DATA_DIR)}


@app.post("/api/clusters/info")
async def clusters_info(ref: ClusterRef) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(runner.cluster_info, ref.url, ref.auth)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/plan")
async def plan(req: PlanRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(runner.plan, req.source_url, req.source_auth,
                                       req.target_url, req.target_auth)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/runs", status_code=202)
def start_run(req: RunRequest) -> dict[str, Any]:
    if not runner.ENGINE.exists():
        raise HTTPException(status_code=500, detail=f"Engine not found at {runner.ENGINE}")
    if req.indices is not None and len(req.indices) == 0:
        raise HTTPException(status_code=422, detail="indices must be non-empty or omitted (auto-discover)")
    options = {k: getattr(req, k) for k in
               ("rps", "pause", "heap_threshold", "unassigned_threshold", "queue_threshold",
                "start_from", "sync_mapping", "copy_settings")}
    run = runner.registry.start(
        source_url=req.source_url, source_auth=req.source_auth,
        target_url=req.target_url, target_auth=req.target_auth,
        indices=req.indices, dry_run=req.dry_run, options=options,
    )
    return run.public()


@app.get("/api/runs")
def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    return [r.public() for r in runner.registry.list(limit)]


def _get_run(run_id: str) -> runner.Run:
    run = runner.registry.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return _get_run(run_id).public()


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, after: int = 0) -> dict[str, Any]:
    """Polling alternative to the SSE stream: events with seq > after."""
    run = _get_run(run_id)
    events = runner.registry.events_after(run, after)
    if not events and after == 0 and run.is_done():
        events = list(runner.registry.replay_from_log(run))
    return {"status": run.status, "events": [e.__dict__ for e in events]}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict[str, Any]:
    return runner.registry.cancel(_get_run(run_id))


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request) -> StreamingResponse:
    run = _get_run(run_id)

    async def gen():
        last = 0
        history = runner.registry.events_after(run, 0)
        if not history and run.is_done():
            history = list(runner.registry.replay_from_log(run))
        for ev in history:
            last = ev.seq
            yield ev.to_sse()
        while True:
            if await request.is_disconnected():
                return
            done = run.is_done()  # read status first, then drain — never drop the final line
            for ev in runner.registry.events_after(run, last):
                last = ev.seq
                yield ev.to_sse()
            if done:
                yield f"data: {json.dumps({'type': 'end', 'status': run.status})}\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Built UI (optional)
# ---------------------------------------------------------------------------
DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
if (DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(DIST / "index.html"))
else:
    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"message": "UI not built. Run `npm run build` in ../ui, or use the dev server on :5180.",
                "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=os.environ.get("OSMT_HOST", "127.0.0.1"),
                port=int(os.environ.get("OSMT_PORT", "8020")), log_level="info")
