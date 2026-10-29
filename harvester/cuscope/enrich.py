"""Enrichment sources (spec 5): Spark sessions, job instances, warehouse query history.

Power BI enrichment needs no code: PowerBIDatasetsWorkspace lands in the same Log Analytics
workspace and is joined in KQL (kql/FabricPbiQueries.kql).

Spark and job lists are re-read every run; only rows whose state changed since the last run
are emitted, tracked in a small state blob, so the tables hold one row per state transition.
"""

import hashlib
import logging
import struct
import time
from datetime import timedelta
from decimal import Decimal

from . import store, timeutil
from .clients import SQL_SCOPE, fabric_get, fabric_get_all, token

log = logging.getLogger(__name__)

JOB_ITEM_TYPES = {"Notebook", "DataPipeline", "SparkJobDefinition", "Dataflow", "CopyJob"}
STATE_RETENTION = timedelta(days=3)


def _seconds(duration):
    if not duration:
        return None
    factor = {"Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86400}.get(duration.get("timeUnit"), 1)
    return (duration.get("value") or 0) * factor


def _changed_only(settings, key, records, id_field, sig_fields):
    """Filter to records whose signature changed since the last run; persist new signatures."""
    seen = store.read_state(settings, key, {})
    cutoff = timeutil.iso(timeutil.utc_now() - STATE_RETENTION)
    seen = {k: v for k, v in seen.items() if v["at"] >= cutoff}
    out = []
    for r in records:
        sig = hashlib.sha1("|".join(str(r.get(f)) for f in sig_fields).encode()).hexdigest()
        if seen.get(r[id_field], {}).get("sig") != sig:
            out.append(r)
            seen[r[id_field]] = {"sig": sig, "at": timeutil.iso(timeutil.utc_now())}
    store.write_state(settings, key, seen)
    return out


def spark_sessions(settings):
    now = timeutil.iso(timeutil.utc_now())
    for ws in settings.enrich_workspace_ids:
        records = []
        for s in fabric_get_all(f"workspaces/{ws}/spark/livySessions"):
            item = s.get("item") or {}
            submitter = s.get("submitter") or {}
            records.append({
                "TimeGenerated": now,
                "livy_id": s.get("livyId"),
                "workspace_id": item.get("workspaceId", ws),
                "item_id": item.get("itemId"),
                "item_name": s.get("itemName"),
                "item_type": s.get("itemType"),
                "capacity_id": s.get("capacityId"),
                "operation_name": s.get("operationName"),
                "job_type": s.get("jobType"),
                "state": s.get("state"),
                "submitter_id": submitter.get("id"),
                "submitter_type": submitter.get("type"),
                "job_instance_id": s.get("jobInstanceId"),
                "spark_application_id": s.get("sparkApplicationId"),
                "submitted_time": s.get("submittedDateTime"),
                "start_time": s.get("startDateTime"),
                "end_time": s.get("endDateTime"),
                "queued_s": _seconds(s.get("queuedDuration")),
                "running_s": _seconds(s.get("runningDuration")),
                "total_s": _seconds(s.get("totalDuration")),
                "driver_cores": s.get("driverCores"),
                "executor_cores": s.get("executorCores"),
                "num_executors": s.get("numExecutors"),
                "dynamic_allocation": s.get("isDynamicAllocationEnabled"),
                "dyn_max_executors": s.get("dynamicAllocationMaxExecutors"),
                "is_high_concurrency": s.get("isHighConcurrency"),
            })
        records = [r for r in records if r["livy_id"]]
        changed = _changed_only(settings, f"enrich/spark/{ws}", records, "livy_id",
                                ["state", "end_time", "running_s"])
        store.emit(settings, "FabricSparkSessions_CL", changed)
        log.info("spark %s: %d sessions, %d changed", ws, len(records), len(changed))


def job_instances(settings):
    now = timeutil.iso(timeutil.utc_now())
    for ws in settings.enrich_workspace_ids:
        records = []
        for item in fabric_get_all(f"workspaces/{ws}/items"):
            if item.get("type") not in JOB_ITEM_TYPES:
                continue
            try:
                jobs = fabric_get_all(f"workspaces/{ws}/items/{item['id']}/jobs/instances")
            except Exception as e:
                log.warning("job instances for %s failed: %s", item.get("displayName"), e)
                continue
            for j in jobs:
                records.append({
                    "TimeGenerated": now,
                    "job_instance_id": j.get("id"),
                    "workspace_id": ws,
                    "item_id": item["id"],
                    "item_name": item.get("displayName"),
                    "item_type": item.get("type"),
                    "job_type": j.get("jobType"),
                    "invoke_type": j.get("invokeType"),
                    "status": j.get("status"),
                    "root_activity_id": j.get("rootActivityId"),
                    "start_time": j.get("startTimeUtc"),
                    "end_time": j.get("endTimeUtc"),
                    "failure_reason": j.get("failureReason"),
                })
        changed = _changed_only(settings, f"enrich/jobs/{ws}", records, "job_instance_id",
                                ["status", "end_time"])
        store.emit(settings, "FabricJobInstances_CL", changed)
        log.info("jobs %s: %d instances, %d changed", ws, len(records), len(changed))


# -- warehouse / SQL endpoint query history ----------------------------------------------

WH_QUERY = """
SELECT TOP (5000)
    distributed_statement_id, session_id, login_name, start_time, end_time, status,
    allocated_cpu_time_ms, total_elapsed_time_ms, row_count, data_scanned_remote_storage_mb,
    label, command
FROM queryinsights.exec_requests_history
WHERE end_time > ?
ORDER BY end_time
"""


def _sql_value(v):
    """Make pyodbc values JSON-serialisable for the Logs Ingestion API."""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%dT%H:%M:%S.%f")
    if isinstance(v, Decimal):
        return float(v)
    return v


def _sql_targets(ws):
    """(item_id, database, server) for every warehouse and lakehouse SQL endpoint."""
    targets = []
    for wh in fabric_get_all(f"workspaces/{ws}/warehouses"):
        server = (wh.get("properties") or {}).get("connectionString")
        if server:
            targets.append((wh["id"], wh["displayName"], server))
    for lh in fabric_get_all(f"workspaces/{ws}/lakehouses"):
        sql = (fabric_get(f"workspaces/{ws}/lakehouses/{lh['id']}").get("properties") or {}) \
            .get("sqlEndpointProperties") or {}
        if sql.get("connectionString") and sql.get("id"):
            targets.append((sql["id"], lh["displayName"], sql["connectionString"]))
    return targets


def _connect(server, database):
    import pyodbc
    raw = token(SQL_SCOPE).encode("utf-16-le")
    attrs = {1256: struct.pack(f"<I{len(raw)}s", len(raw), raw)}  # SQL_COPT_SS_ACCESS_TOKEN
    conn_str = (f"Driver={{ODBC Driver 18 for SQL Server}};Server={server},1433;Database={database};"
                "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60")
    return pyodbc.connect(conn_str, attrs_before=attrs, timeout=60)


def warehouse_requests(settings):
    """Copy queryinsights out of Fabric every run (spec 5.3): it is the one enrichment source
    served by the production capacity, so history must already be outside when it locks up."""
    now = timeutil.iso(timeutil.utc_now())
    for ws in settings.enrich_workspace_ids:
        for item_id, database, server in _sql_targets(ws):
            key = f"enrich/wh/{item_id}"
            mark = (store.read_state(settings, key) or {}).get("end_time", "2000-01-01T00:00:00")
            started = time.monotonic()
            try:
                with _connect(server, database) as conn:
                    cur = conn.cursor()
                    cur.execute(WH_QUERY, mark)
                    cols = [c[0] for c in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as e:
                log.warning("queryinsights %s/%s failed: %s", ws, database, e)
                continue
            records = [{
                "TimeGenerated": now, "workspace_id": ws, "item_id": item_id, "item_name": database,
                **{k: _sql_value(v) for k, v in r.items()},
            } for r in rows]
            store.emit(settings, "FabricWhRequests_CL", records)
            if rows:
                store.write_state(settings, key, {"end_time": records[-1]["end_time"]})
            log.info("queryinsights %s: %d rows in %.1fs", database, len(rows), time.monotonic() - started)
