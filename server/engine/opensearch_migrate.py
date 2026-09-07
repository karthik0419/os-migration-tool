#!/usr/bin/env python3
"""OpenSearch Cross-Cluster Reindex Migration Tool

Reindexes indices from a source OpenSearch/ES cluster to a target OpenSearch
cluster with full source protection, resumability, and error handling.

Designed for the qelsdata -> qosdata migration pattern but parameterized for
any cluster pair.

Usage:
  # Full migration with auto-discovery of mismatched indices
  python3 opensearch_migrate.py \
    --source http://qelsdata01.p01.eng.sjc01.qualys.com:50140 \
    --source-auth admin:admin123 \
    --target http://qosdata01.p01.eng.sjc01.qualys.com:50140 \
    --target-auth admin:admin \
    --discover

  # Resume from a specific index (skip already-processed)
  python3 opensearch_migrate.py \
    --source http://qelsdata01.p01.eng.sjc01.qualys.com:50140 \
    --source-auth admin:admin123 \
    --target http://qosdata01.p01.eng.sjc01.qualys.com:50140 \
    --target-auth admin:admin \
    --indices-file /tmp/migration_indices.json \
    --start-from 42

  # Reindex specific indices only
  python3 opensearch_migrate.py \
    --source http://qelsdata01.p01.eng.sjc01.qualys.com:50140 \
    --source-auth admin:admin123 \
    --target http://qosdata01.p01.eng.sjc01.qualys.com:50140 \
    --target-auth admin:admin \
    --indices asset-idx149-v3,uai-storage-idx1-v2

Safety:
  - op_type=index ONLY - creates new docs, updates existing, NEVER deletes
  - Source cluster is READ-ONLY - never written to
  - No index is deleted on target - only created (for source-only) or updated
  - Target count checked BEFORE and AFTER - aborts if count DECREASES
  - Conflicts tolerated (conflicts=proceed)
  - SOURCE PROTECTION:
    * Heap monitoring - skips if any node > threshold (default 85%)
    * Cluster health check - skips if RED or too many unassigned shards
    * Search queue monitoring - pauses if queue building up
    * Configurable rps (default 200)
    * Pause between indices for source recovery
  - MAPPING CONFLICTS: Detected and logged - index skipped, not retried
    (unlike circuit breakers which ARE retried with smaller batches)
  - RESUMABLE: --start-from skips already-processed indices
  - Results saved to JSON for audit trail

Lessons learned (incorporated from p01 migration runs):
  - 429 responses may have malformed JSON bodies - handle gracefully
  - Circuit breaker exceptions need retry with smaller batch sizes
  - Mapping conflicts (field type mismatch) CANNOT be fixed by retry -
    need v4 index with corrected mapping + alias swap (separate process)
  - Source cache clear can 429 - treat as non-fatal
  - Unassigned shard threshold should be high enough to not block on
    replica recovery (source may have hundreds of unassigned replicas)
  - Clear source cache in wait loop can CAUSE 429s - removed from wait loop
"""
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
import urllib.error
import base64
import ssl
import datetime
import subprocess

# ---------------------------------------------------------------------------
# Globals (set from args)
# ---------------------------------------------------------------------------
SOURCE = None
TARGET = None
SOURCE_AUTH = None
TARGET_AUTH = None
LOG_FILE = None
RESULTS_FILE = None
HEAP_THRESHOLD = 85
UNASSIGNED_THRESHOLD = 600
SEARCH_QUEUE_THRESHOLD = 10
PAUSE_BETWEEN = 30
SYNC_MAPPING = False     # --sync-mapping: PUT source mapping onto existing target indices
COPY_SETTINGS = False    # --copy-settings: copy source settings (analyzers/shards) for source-only

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def log(msg):
    ts = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = "[%s] %s" % (ts, msg)
    print(line, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _auth_header(auth_str):
    if not auth_str:
        return None
    return "Basic " + base64.b64encode(auth_str.encode()).decode()


def _src_auth():
    return _auth_header(SOURCE_AUTH)


def _tgt_auth():
    return _auth_header(TARGET_AUTH)


def _auth_headers(is_target=False) -> dict:
    h = _tgt_auth() if is_target else _src_auth()
    return {"Authorization": h} if h else {}


def jget(url, timeout=30, is_target=False):
    req = urllib.request.Request(url, headers=_auth_headers(is_target))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return None
    except Exception:
        return None


def jpost(url, body, timeout=120, is_target=False):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        **_auth_headers(is_target), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            return r.getcode(), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:2000]
        try:
            parsed = json.loads(raw) if raw.startswith("{") else None
            return e.code, parsed if parsed is not None else {"error": raw}
        except (json.JSONDecodeError, ValueError):
            return e.code, {"error": raw[:500]}
    except Exception as e:
        return 0, {"error": str(e)[:200]}


def jput(url, body, timeout=60, is_target=True):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="PUT", headers={
        **_auth_headers(is_target), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            return r.getcode(), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:2000]
        try:
            parsed = json.loads(raw) if raw.startswith("{") else None
            return e.code, parsed if parsed is not None else {"error": raw}
        except (json.JSONDecodeError, ValueError):
            return e.code, {"error": raw[:500]}
    except Exception as e:
        return 0, {"error": str(e)[:200]}


# ---------------------------------------------------------------------------
# Count & mapping helpers
# ---------------------------------------------------------------------------
def get_count(cluster, index, is_target=False):
    code, resp = jpost("%s/%s/_search?size=0&track_total_hits=true" % (cluster, index),
                       {"query": {"match_all": {}}}, timeout=120, is_target=is_target)
    if code == 200 and "hits" in resp and "total" in resp["hits"]:
        tot = resp["hits"]["total"]
        return tot.get("value", 0) if isinstance(tot, dict) else tot
    data = jget("%s/%s/_count" % (cluster, index), timeout=30, is_target=is_target)
    if data and "count" in data:
        return data["count"]
    return None


def get_mapping(cluster, index, is_target=False):
    data = jget("%s/%s/_mapping" % (cluster, index), timeout=30, is_target=is_target)
    if data and index in data:
        return data[index].get("mappings", {})
    return None


def get_settings(cluster, index, is_target=False):
    """Return the index settings dict (the 'settings' object from _settings API)."""
    data = jget("%s/%s/_settings" % (cluster, index), timeout=30, is_target=is_target)
    if data and index in data:
        return data[index].get("settings", {})
    return None


def _filter_live_only(settings):
    """Strip read-only / runtime settings that can't be PUT on index creation.

    ES/OpenSearch rejects PUT /<index> with body containing auto-managed keys like
    'index.creation_date', 'index.uuid', 'index.version.created', 'index.provided_name'.
    Only keep keys that are user-settable at creation time.
    """
    if not settings:
        return {}
    idx = settings.get("index", settings)  # settings may be flat or nested under "index"
    if not isinstance(idx, dict):
        return {}
    # Keys that are managed by the cluster and cannot be specified at create time.
    _READ_ONLY = {
        "creation_date", "uuid", "version", "provided_name", "routing",
        "history_uuid", "resize_source_name", "resize_source_uuid",
        "verified_before_close", "frozen",
    }
    clean = {}
    for k, v in idx.items():
        if k in _READ_ONLY:
            continue
        # number_of_shards can't be changed after creation, but IS valid at create time
        clean[k] = v
    return {"index": clean}


def index_exists(cluster, index, is_target=False):
    data = jget("%s/%s/_count" % (cluster, index), timeout=15, is_target=is_target)
    return data is not None


def refresh_index(cluster, index, is_target=True):
    jpost("%s/%s/_refresh" % (cluster, index), {}, timeout=30, is_target=is_target)


def clear_source_cache():
    code, resp = jpost("%s/_cache/clear" % SOURCE, {}, timeout=60, is_target=False)
    if code == 200:
        log("  Source cache cleared.")
    else:
        log("  Source cache clear returned HTTP %d (non-fatal)." % code)


# ---------------------------------------------------------------------------
# Source protection
# ---------------------------------------------------------------------------
def check_source_heap():
    data = jget("%s/_nodes/stats/jvm" % SOURCE, timeout=30, is_target=False)
    if not data:
        return 0
    max_pct = 0
    for nid, info in data.get("nodes", {}).items():
        mem = info.get("jvm", {}).get("mem", {})
        heap_used = mem.get("heap_used_in_bytes", 0)
        heap_max = mem.get("heap_max_in_bytes", 1)
        pct = heap_used / heap_max * 100
        if pct > max_pct:
            max_pct = pct
    return max_pct


def check_source_cluster_health():
    data = jget("%s/_cluster/health" % SOURCE, timeout=30, is_target=False)
    if not data:
        return "unknown", 999, 999, 999
    return (data.get("status", "unknown"),
            data.get("initializing_shards", 0),
            data.get("unassigned_shards", 0),
            data.get("number_of_pending_tasks", 0))


def check_source_search_queue():
    data = jget("%s/_cat/thread_pool/search?h=node_name,queue&format=json" % SOURCE,
                timeout=15, is_target=False)
    if not data:
        return 0
    max_queue = 0
    for entry in data:
        q = int(entry.get("queue", 0))
        if q > max_queue:
            max_queue = q
    return max_queue


def source_is_safe():
    status, init, unassigned, pending = check_source_cluster_health()
    if status == "red":
        return False, "source cluster RED"
    if unassigned > UNASSIGNED_THRESHOLD:
        return False, "source has %d unassigned shards" % unassigned
    if pending > 50:
        return False, "source has %d pending tasks" % pending
    heap = check_source_heap()
    if heap > HEAP_THRESHOLD:
        return False, "source heap %.1f%% > %d%%" % (heap, HEAP_THRESHOLD)
    queue = check_source_search_queue()
    if queue > SEARCH_QUEUE_THRESHOLD:
        return False, "source search queue %d > %d" % (queue, SEARCH_QUEUE_THRESHOLD)
    return True, "heap=%.1f%% queue=%d status=%s init=%d" % (heap, queue, status, init)


def wait_for_source_safe(max_wait=3600):
    waited = 0
    while waited < max_wait:
        safe, reason = source_is_safe()
        if safe:
            if waited > 0:
                log("  Source recovered after %ds: %s" % (waited, reason))
            return True
        if waited == 0:
            log("  Source NOT safe: %s. Waiting..." % reason)
        time.sleep(30)
        waited += 30
        if waited % 300 == 0:
            log("  Still waiting for source (%ds): %s" % (waited, reason))
    log("  Source did not recover after %ds. Skipping." % max_wait)
    return False


# ---------------------------------------------------------------------------
# Reindex logic
# ---------------------------------------------------------------------------
def start_reindex(index, batch_size, rps):
    # Build remote block — include username/password only if auth is provided
    remote_block = {"host": SOURCE}
    if SOURCE_AUTH and ":" in SOURCE_AUTH:
        remote_block["username"] = SOURCE_AUTH.split(":")[0]
        remote_block["password"] = SOURCE_AUTH.split(":", 1)[1]
    body = {
        "source": {
            "remote": remote_block,
            "index": index,
            "size": batch_size,
        },
        "dest": {"index": index, "op_type": "index"},
        "conflicts": "proceed",
    }
    # Rate limiting is a query parameter, NOT a body field.
    # ES/OpenSearch both use ?requests_per_second=N (body "rate" field causes HTTP 400).
    url = "%s/_reindex?wait_for_completion=false" % TARGET
    if rps > 0:
        url += "&requests_per_second=%d" % rps

    code, resp = jpost(url, body, timeout=60, is_target=True)
    if code == 200 and "task" in resp:
        return resp["task"]
    log("  ERROR starting reindex: HTTP %d %s" % (code, json.dumps(resp)[:300]))
    return None


def poll_task(task_id, max_wait=72000):
    waited = 0
    while waited < max_wait:
        time.sleep(10)
        waited += 10
        info = jget("%s/_tasks/%s" % (TARGET, task_id), timeout=30, is_target=True)
        if info is None:
            continue
        completed = info.get("completed", False)
        task = info.get("task", info)
        if completed:
            resp = info.get("response", task.get("response", {}))
            status = task.get("status", {})
            created = resp.get("created", status.get("created", 0))
            updated = resp.get("updated", status.get("updated", 0))
            noops = resp.get("noops", status.get("noops", 0))
            failures = resp.get("failures", [])
            conflicts = resp.get("version_conflicts", status.get("version_conflicts", 0))
            batches = resp.get("batches", status.get("batches", 0))
            total = resp.get("total", status.get("total", 0))
            log("  [%ds] COMPLETED: created=%s updated=%s noops=%s conflicts=%s batches=%s total=%s failures=%d" % (
                waited, format(created, ","), format(updated, ","),
                format(noops, ","), format(conflicts, ","),
                format(batches, ","), format(total, ","), len(failures)))
            if failures:
                for f in failures[:5]:
                    log("  FAILURE: %s" % json.dumps(f)[:300])
            has_cb = any("circuit_breaking_exception" in json.dumps(f) for f in failures)
            has_mapping = any("mapper" in json.dumps(f) and "cannot be changed" in json.dumps(f)
                              for f in failures)
            return True, resp, has_cb, has_mapping
        else:
            status = task.get("status", {})
            created = status.get("created", 0)
            updated = status.get("updated", 0)
            noops = status.get("noops", 0)
            batches = status.get("batches", 0)
            total = status.get("total", 0)
            processed = created + updated + noops
            pct = (processed / total * 100) if total else 0
            if waited % 300 == 0:
                log("  [%ds] Progress: %s/%s (%.1f%%) [created=%s updated=%s noops=%s batches=%d]" % (
                    waited, format(processed, ","), format(total, ","), pct,
                    format(created, ","), format(updated, ","),
                    format(noops, ","), batches))
    log("  TIMEOUT after %ds." % max_wait)
    return False, {}, False, False


def create_index_on_target(index):
    src_mapping = get_mapping(SOURCE, index, is_target=False)
    if not src_mapping:
        log("  Could not fetch source mapping for %s." % index)
        return False
    if COPY_SETTINGS:
        src_settings = get_settings(SOURCE, index, is_target=False)
        settings = {"settings": _filter_live_only(src_settings)}
        log("  Copying source settings for %s (shards=%s replicas=%s)." % (
            index,
            settings["settings"].get("index", {}).get("number_of_shards", "?"),
            settings["settings"].get("index", {}).get("number_of_replicas", "?")))
    else:
        settings = {"settings": {"number_of_shards": 5, "number_of_replicas": 1}}
    body = {"mappings": src_mapping, **settings}
    code, resp = jput("%s/%s" % (TARGET, index), body, timeout=60, is_target=True)
    if code in (200, 201):
        log("  Created index %s on target with source mapping." % index)
        return True
    log("  Failed to create index: HTTP %d %s" % (code, json.dumps(resp)[:300]))
    return False


def sync_mapping_on_target(index):
    """PUT the source mapping onto an existing target index.

    OpenSearch/ES allows ADDING new fields to an existing mapping (PUT /<index>/_mapping)
    but will reject changing an existing field's type with a 400 'cannot be changed'.
    Returns True if the PUT succeeded (new fields added or no diff), False on conflict.
    """
    src_mapping = get_mapping(SOURCE, index, is_target=False)
    if not src_mapping:
        log("  Could not fetch source mapping for %s." % index)
        return False
    code, resp = jput("%s/%s/_mapping" % (TARGET, index), src_mapping, timeout=60, is_target=True)
    if code in (200, 201):
        log("  Synced source mapping onto target index %s." % index)
        return True
    # 400 with 'cannot be changed' = field type conflict (existing field type differs)
    err_str = json.dumps(resp) if resp else ""
    if "cannot be changed" in err_str or "mapper_parsing_exception" in err_str:
        log("  MAPPING SYNC CONFLICT on %s: existing field type differs from source." % index)
        log("  Detail: %s" % err_str[:300])
        return False
    log("  Mapping sync failed: HTTP %d %s" % (code, err_str[:300]))
    return False


def reindex_index(index, src_count, is_source_only=False, batch_size=100, rps=200):
    """Reindex a single index. Returns:
      True             - successfully reindexed (gap closed or < 1%)
      False            - still partial after all retries
      'data_loss'      - target count decreased (ABORT)
      'heap_high'      - source heap too high
      'source_pressure'- source not safe
      'mapping_conflict'- field type mismatch (can't fix by retry)
      'circuit_breaker'- CB hit, should retry with smaller batch
    """
    if not wait_for_source_safe():
        return "source_pressure"

    tgt_before = get_count(TARGET, index, is_target=True)
    if tgt_before is not None and src_count is not None and tgt_before > src_count:
        log("  Target already ahead: tgt=%s src=%s. Skipping." % (
            format(tgt_before, ","), format(src_count, ",")))
        return True

    if is_source_only and (tgt_before is None or tgt_before == 0):
        log("  Source-only index not on target. Creating with source mapping...")
        if not create_index_on_target(index):
            return False
    elif SYNC_MAPPING and tgt_before is not None and tgt_before > 0:
        # Existing target index — try to add any new source fields to its mapping.
        # If a field type differs, this returns False (conflict) and we log it but
        # still proceed with reindex — the reindex itself may also fail with a
        # mapping_conflict, which is the authoritative signal.
        log("  Syncing source mapping onto existing target index...")
        sync_mapping_on_target(index)

    log("  Target BEFORE: %s  Source: %s  Gap: %s" % (
        format(tgt_before or 0, ","), format(src_count or 0, ","),
        format((src_count or 0) - (tgt_before or 0), ",")))

    task_id = start_reindex(index, batch_size, rps)
    if not task_id:
        return False
    log("  Task ID: %s" % task_id)

    ok, resp, has_cb, has_mapping = poll_task(task_id)
    if not ok:
        return False

    if has_mapping:
        log("  MAPPING CONFLICT detected. Cannot fix by retry. Needs v4 index.")
        return "mapping_conflict"

    if has_cb:
        log("  CIRCUIT BREAKER detected.")
        return "circuit_breaker"

    refresh_index(TARGET, index, is_target=True)
    tgt_after = get_count(TARGET, index, is_target=True)

    if tgt_after is not None and tgt_before is not None and tgt_after < tgt_before:
        log("  DATA LOSS: target count decreased %s -> %s!" % (
            format(tgt_before, ","), format(tgt_after, ",")))
        return "data_loss"

    if tgt_after is not None and src_count is not None:
        gap = src_count - tgt_after
        gap_pct = (gap / src_count * 100) if src_count else 0
        delta = tgt_after - (tgt_before or 0)
        log("  Target AFTER: %s  Gap: %s (%.2f%%)  [before=%s, delta=+%s]" % (
            format(tgt_after, ","), format(gap, ","), gap_pct,
            format(tgt_before or 0, ","), format(delta, ",")))
        if gap <= 0:
            log("  REPAIRED: %s - gap=0 (0.00%%) - ACCEPTED" % index)
            return True
        if gap_pct < 1.0:
            log("  REPAIRED: %s - gap=%s (%.2f%%) - ACCEPTED (<1%%)" % (index, format(gap, ","), gap_pct))
            return True
        log("  STILL PARTIAL: %s - gap=%s (%.2f%%)" % (index, format(gap, ","), gap_pct))
        return False

    log("  Could not verify counts. Treating as partial.")
    return False


# ---------------------------------------------------------------------------
# Index discovery
# ---------------------------------------------------------------------------
def discover_mismatched_indices():
    """Compare source vs target indices and find ones with doc count gaps."""
    log("Discovering mismatched indices...")

    # Get all source indices
    src_data = jget("%s/_cat/indices?h=index,docs.count&format=json" % SOURCE,
                    timeout=60, is_target=False)
    if not src_data:
        log("ERROR: Could not fetch source index list.")
        return []

    # Get all target indices
    tgt_data = jget("%s/_cat/indices?h=index,docs.count&format=json" % TARGET,
                    timeout=60, is_target=True)
    if not tgt_data:
        log("ERROR: Could not fetch target index list.")
        return []

    tgt_counts = {d["index"]: int(d["docs.count"]) for d in tgt_data}

    # _cat/indices does NOT list aliases. A source index that was migrated to
    # "<name>-migrated" + alias "<name>" on target would otherwise look
    # SOURCE-ONLY. Resolve those through the alias with a real doc count.
    tgt_alias_data = jget("%s/_cat/aliases?h=alias,index&format=json" % TARGET,
                          timeout=60, is_target=True) or []
    tgt_aliases = {}
    for a in tgt_alias_data:
        tgt_aliases.setdefault(a["alias"], []).append(a["index"])
    if tgt_aliases:
        log("  Target has %d aliases; resolving source-only candidates through them." % len(tgt_aliases))

    mismatches = []
    via_alias = 0
    for entry in src_data:
        name = entry["index"]
        # Skip system indices
        if name.startswith(".") or name.startswith("kibana"):
            continue
        src_docs = int(entry["docs.count"])
        tgt_docs = tgt_counts.get(name, -1)
        alias_target = None

        if tgt_docs == -1 and name in tgt_aliases:
            # Compare like-for-like (_search totals on both sides) since docs.count
            # from _cat includes nested docs while _search does not.
            tgt_via = get_count(TARGET, name, is_target=True)
            src_via = get_count(SOURCE, name, is_target=False)
            if tgt_via is not None:
                tgt_docs = tgt_via
                alias_target = ",".join(tgt_aliases[name])
                via_alias += 1
                if src_via is not None:
                    src_docs = src_via

        if tgt_docs == -1:
            # Source-only - doesn't exist on target
            mismatches.append({"index": name, "src_count": src_docs, "tgt_count": 0,
                               "gap": src_docs, "source_only": True})
        elif tgt_docs < src_docs:
            gap = src_docs - tgt_docs
            if gap > 0:
                m = {"index": name, "src_count": src_docs, "tgt_count": tgt_docs,
                     "gap": gap, "source_only": False}
                if alias_target:
                    m["target_alias"] = alias_target
                mismatches.append(m)

    mismatches.sort(key=lambda x: x["gap"])
    if via_alias:
        log("  %d source indices resolved via target alias." % via_alias)
    log("Found %d mismatched indices (total gap: %s docs)." % (
        len(mismatches), format(sum(m["gap"] for m in mismatches), ",")))
    return mismatches


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global SOURCE, TARGET, SOURCE_AUTH, TARGET_AUTH, LOG_FILE, RESULTS_FILE
    global HEAP_THRESHOLD, UNASSIGNED_THRESHOLD, SEARCH_QUEUE_THRESHOLD, PAUSE_BETWEEN

    parser = argparse.ArgumentParser(
        description="OpenSearch cross-cluster reindex migration tool")
    parser.add_argument("--source", required=True, help="Source cluster URL")
    parser.add_argument("--source-auth",
                        help="Source auth (user:pass); or set env OS_MIGRATE_SOURCE_AUTH")
    parser.add_argument("--target", required=True, help="Target cluster URL")
    parser.add_argument("--target-auth",
                        help="Target auth (user:pass); or set env OS_MIGRATE_TARGET_AUTH")
    parser.add_argument("--discover", action="store_true",
                        help="Auto-discover mismatched indices by comparing doc counts")
    parser.add_argument("--indices-file", help="JSON file with index list to process")
    parser.add_argument("--indices", help="Comma-separated index names to process")
    parser.add_argument("--start-from", type=int, default=1,
                        help="Skip indices 1 to N-1 (1-indexed, for resume)")
    parser.add_argument("--heap-threshold", type=int, default=85,
                        help="Skip if source heap > this %% (default 85)")
    parser.add_argument("--unassigned-threshold", type=int, default=600,
                        help="Skip if source unassigned shards > this (default 600)")
    parser.add_argument("--queue-threshold", type=int, default=10,
                        help="Skip if source search queue > this (default 10)")
    parser.add_argument("--pause", type=int, default=30,
                        help="Pause seconds between indices (default 30)")
    parser.add_argument("--rps", type=int, default=200,
                        help="Requests per second limit (default 200)")
    parser.add_argument("--log-file", help="Log file path (default: /tmp/os_migrate_<timestamp>.log)")
    parser.add_argument("--results-file", help="Results JSON path (default: /tmp/os_migrate_<timestamp>_results.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without reindexing")
    parser.add_argument("--sync-mapping", action="store_true",
                        help="For existing target indices, PUT source mapping before reindex "
                             "(adds new fields; field-type changes are detected and logged)")
    parser.add_argument("--copy-settings", action="store_true",
                        help="For source-only indices, copy source index settings (analyzers, "
                             "shard/replica count) instead of defaulting to 5 shards / 1 replica")
    args = parser.parse_args()

    SOURCE = args.source
    TARGET = args.target
    SOURCE_AUTH = args.source_auth or os.environ.get("OS_MIGRATE_SOURCE_AUTH", "")
    TARGET_AUTH = args.target_auth or os.environ.get("OS_MIGRATE_TARGET_AUTH", "")
    # Auth is optional — clusters may have no auth enabled
    HEAP_THRESHOLD = args.heap_threshold
    UNASSIGNED_THRESHOLD = args.unassigned_threshold
    SEARCH_QUEUE_THRESHOLD = args.queue_threshold
    PAUSE_BETWEEN = args.pause
    SYNC_MAPPING = args.sync_mapping
    COPY_SETTINGS = args.copy_settings

    ts = _utcnow().strftime("%Y%m%d_%H%M%S")
    LOG_FILE = args.log_file or os.path.join(tempfile.gettempdir(), "os_migrate_%s.log" % ts)
    RESULTS_FILE = args.results_file or os.path.join(tempfile.gettempdir(), "os_migrate_%s_results.json" % ts)

    # Build index list
    if args.discover:
        all_indices = discover_mismatched_indices()
        if not all_indices:
            log("No mismatched indices found. Nothing to do.")
            return
        # Save discovered list for reference (next to the results file so callers can find it)
        disc_file = RESULTS_FILE.replace("_results.json", "_discovered.json") \
            if RESULTS_FILE.endswith("_results.json") \
            else os.path.join(tempfile.gettempdir(), "os_migrate_%s_discovered.json" % ts)
        with open(disc_file, "w") as f:
            json.dump(all_indices, f, indent=2)
        log("Discovered indices saved: %s" % disc_file)
    elif args.indices_file:
        with open(args.indices_file) as f:
            all_indices = json.load(f)
        # Normalize format
        for entry in all_indices:
            if "source_only" not in entry:
                entry["source_only"] = False
    elif args.indices:
        names = [n.strip() for n in args.indices.split(",")]
        all_indices = []
        for name in names:
            src_count = get_count(SOURCE, name, is_target=False)
            tgt_count = get_count(TARGET, name, is_target=True) or 0
            all_indices.append({
                "index": name,
                "src_count": src_count or 0,
                "tgt_count": tgt_count,
                "gap": (src_count or 0) - tgt_count,
                "source_only": tgt_count == 0,
            })
    else:
        log("ERROR: Must specify --discover, --indices-file, or --indices")
        sys.exit(1)

    total = len(all_indices)
    log("=" * 80)
    log("OPENSEARCH CROSS-CLUSTER REINDEX MIGRATION")
    log("  Source: %s (%s)" % (SOURCE, SOURCE_AUTH.split(":")[0] if SOURCE_AUTH else "no-auth"))
    log("  Target: %s (%s)" % (TARGET, TARGET_AUTH.split(":")[0] if TARGET_AUTH else "no-auth"))
    log("  op_type=index ONLY - NO deletion, NO data loss")
    log("  Total indices to process: %d" % total)
    log("  SOURCE PROTECTION: heap<%d%%, queue<%d, unassigned<%d, pause=%ds" % (
        HEAP_THRESHOLD, SEARCH_QUEUE_THRESHOLD, UNASSIGNED_THRESHOLD, PAUSE_BETWEEN))
    if SYNC_MAPPING:
        log("  SYNC MAPPING: enabled (PUT source mapping onto existing target indices)")
    if COPY_SETTINGS:
        log("  COPY SETTINGS: enabled (source-only indices get source settings, not 5/1 default)")
    if args.start_from > 1:
        log("  START_FROM=%d: skipping indices 1-%d (already processed)" % (
            args.start_from, args.start_from - 1))
    log("  Log: %s" % LOG_FILE)
    log("  Results: %s" % RESULTS_FILE)
    log("=" * 80)
    log("")

    if args.dry_run:
        log("DRY RUN - no reindexing will occur.")
        for i, entry in enumerate(all_indices):
            if i + 1 < args.start_from:
                continue
            name = entry["index"]
            src = entry.get("src_count", "?")
            gap = entry.get("gap", "?")
            so = " (SOURCE-ONLY)" if entry.get("source_only") else ""
            log("  [%d/%d] %s%s  src=%s gap=%s" % (i + 1, total, name, so, src, gap))
        return

    repaired = 0
    still_partial = 0
    data_loss_detected = 0
    skipped = 0
    mapping_conflicts = 0
    results = []

    batch_sizes = [100, 50, 25, 10, 5, 1]
    rps_values = [args.rps, args.rps // 2, args.rps // 4, args.rps // 8, args.rps // 20, 5]

    for i, entry in enumerate(all_indices):
        if i + 1 < args.start_from:
            continue

        name = entry["index"]
        is_src_only = entry.get("source_only", False)
        log("[%d/%d] %s%s" % (i + 1, total, name, " (SOURCE-ONLY)" if is_src_only else ""))

        # Get fresh source count
        fresh_src = get_count(SOURCE, name, is_target=False)
        if fresh_src is not None:
            src_live = fresh_src
            log("  Fresh source count: %s" % format(src_live, ","))
        else:
            src_live = entry.get("src_count", entry.get("gap", 0))
            log("  Using cached source count: %s" % format(src_live, ","))

        if src_live == 0 and is_src_only:
            if index_exists(TARGET, name, is_target=True):
                log("  Source has 0 docs and index exists on target. Skipping.")
                skipped += 1
                results.append({"index": name, "result": "skipped_0_docs",
                                "src": 0, "tgt_after": 0})
                log("")
                continue
            if create_index_on_target(name):
                repaired += 1
                results.append({"index": name, "result": "created_empty",
                                "src": 0, "tgt_after": 0})
            else:
                still_partial += 1
                results.append({"index": name, "result": "create_failed",
                                "src": 0, "tgt_after": None})
            log("")
            continue

        result = False
        for attempt, (bs, rps) in enumerate(zip(batch_sizes, rps_values)):
            if attempt > 0:
                if result == "mapping_conflict":
                    # Don't retry mapping conflicts - they can't be fixed by smaller batches
                    break
                log("  Retry %d with batch=%d rps=%d (waiting 60s for GC)..." % (
                    attempt, bs, rps))
                clear_source_cache()
                time.sleep(60)

            result = reindex_index(name, src_live, is_source_only=is_src_only,
                                   batch_size=bs, rps=rps)

            if result is True:
                repaired += 1
                break
            elif result == "data_loss":
                data_loss_detected += 1
                break
            elif result in ("heap_high", "source_pressure"):
                log("  Source under pressure. Waiting 120s before retry...")
                clear_source_cache()
                time.sleep(120)
                continue
            elif result == "mapping_conflict":
                log("  Mapping conflict. Will NOT retry (needs v4 index).")
                mapping_conflicts += 1
                break
            elif result == "circuit_breaker":
                log("  Will retry with smaller batch.")
                continue
            elif result is False:
                tgt = get_count(TARGET, name, is_target=True)
                if tgt is not None:
                    gap = src_live - tgt
                    gap_pct = (gap / src_live * 100) if src_live else 0
                    if gap_pct < 1.0:
                        log("  Gap < 1%%. Accepting.")
                        repaired += 1
                        result = True
                        break
                log("  Still partial. Will retry with smaller batch.")
                continue

        if result is not True and result not in ("heap_high", "source_pressure", "mapping_conflict"):
            still_partial += 1
        elif result in ("heap_high", "source_pressure"):
            skipped += 1

        tgt_final = get_count(TARGET, name, is_target=True)
        results.append({"index": name, "result": str(result),
                        "src": src_live, "tgt_after": tgt_final})
        log("")

        # Pause between indices
        if i < total - 1:
            log("  Pausing %ds between indices for source recovery..." % PAUSE_BETWEEN)
            time.sleep(PAUSE_BETWEEN)

    # Summary
    log("=" * 80)
    log("MIGRATION COMPLETE")
    log("  Repaired:           %d" % repaired)
    log("  Still PARTIAL:      %d" % still_partial)
    log("  Skipped:            %d" % skipped)
    log("  Mapping conflicts:  %d" % mapping_conflicts)
    log("  Data loss alerts:   %d" % data_loss_detected)
    log("  Total attempted:    %d" % total)
    log("=" * 80)
    log("")
    log("Per-index results:")
    for r in results:
        log("  %-45s %-20s src=%s tgt=%s" % (
            r["index"], r["result"],
            format(r.get("src", 0), ","),
            format(r.get("tgt_after", 0) or 0, ",")))

    # Save results
    summary = {
        "timestamp": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCE,
        "target": TARGET,
        "repaired": repaired,
        "still_partial": still_partial,
        "skipped": skipped,
        "mapping_conflicts": mapping_conflicts,
        "data_loss": data_loss_detected,
        "total": total,
        "results": results,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(summary, f, indent=2)
    log("Results saved: %s" % RESULTS_FILE)
    log("Log file: %s" % LOG_FILE)

    # Print mapping conflicts prominently for follow-up
    if mapping_conflicts > 0:
        log("")
        log("!" * 80)
        log("MAPPING CONFLICTS DETECTED - these indices need v4 index with corrected mapping:")
        for r in results:
            if r["result"] == "mapping_conflict":
                log("  - %s (src=%s, tgt=%s)" % (
                    r["index"], format(r.get("src", 0), ","),
                    format(r.get("tgt_after", 0) or 0, ",")))
        log("  Fix: create new index with source mapping, reindex, swap alias")
        log("!" * 80)


if __name__ == "__main__":
    main()
