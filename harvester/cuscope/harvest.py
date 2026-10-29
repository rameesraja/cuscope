"""Metrics-model harvester (spec 6.3): sample timepoint detail per capacity, normalise, store.

Every executeQueries response is written raw to the raw store before parsing, so a schema
change or parser bug can be replayed without re-querying a source that keeps 14 days.
"""

import hashlib
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from . import store, timeutil
from .clients import QueryError, SemanticModel

log = logging.getLogger(__name__)

DAX_DIR = Path(__file__).resolve().parent.parent / "dax"
DETAIL_TABLES = {
    "interactive": "Timepoint Interactive Detail",
    "background": "Timepoint Background Detail",
}
# Normalised model column -> FabricOps_CL column, where they differ.
RENAME = {
    "user": "user_name",
    "operation_start_time": "start_time",
    "operation_end_time": "end_time",
}
OPS_COLUMNS = {
    "operation_id", "operation", "user_name", "status", "billing_type", "start_time", "end_time",
    "smoothing_start", "smoothing_end", "workspace_id", "workspace_name", "item_id", "item_name",
    "item_kind", "duration_s", "total_cu_s", "timepoint_cu_s", "throttling_s",
}
DATETIME_COLUMNS = {"start_time", "end_time", "smoothing_start", "smoothing_end"}
# Columns the detail query must return; names come from the Items snapshot, not this table.
REQUIRED_COLUMNS = OPS_COLUMNS - {"item_name", "item_kind", "workspace_name"}


class SchemaDrift(Exception):
    pass


def dax(name, **params):
    params.setdefault("filter_vars", "")
    params.setdefault("filters", "")
    return (DAX_DIR / f"{name}.dax").read_text(encoding="utf-8").format(**params)


def norm(key):
    """'Timepoint Background Detail[Operation start time]' -> 'operation_start_time'."""
    match = re.search(r"\[([^\]]+)\]$", key)
    name = match.group(1) if match else key
    name = re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_")
    return RENAME.get(name, name)


class Harvester:
    """One instance per Function invocation: hydrates the DirectQuery session once."""

    def __init__(self, settings, job):
        self.s = settings
        self.job = job
        self.model = SemanticModel(settings.metrics_workspace_id, settings.metrics_dataset_id)
        self.model.query(dax("hydrate"))

    # -- logging ------------------------------------------------------------------------

    def _log(self, capacity_id, table, timepoint, status, rows=0, started=None, message=""):
        store.emit(self.s, "FabricHarvestLog_CL", [{
            "TimeGenerated": timeutil.iso(timeutil.utc_now()),
            "job": self.job,
            "capacity_id": capacity_id,
            "table_name": table,
            "sample_timepoint": timeutil.iso(timeutil.to_utc(timepoint, self.s.utc_offset_hours))
            if timepoint else None,
            "status": status,
            "rows": rows,
            "duration_ms": int((time.monotonic() - started) * 1000) if started else 0,
            "message": message[:4000],
        }])

    def _query_raw(self, capacity_id, table, timepoint, text):
        """Run a query, persist the raw response, return rows."""
        rows, payload = self.model.query(text)
        stamp = timepoint or timeutil.utc_now()
        path = (f"capacity={capacity_id}/date={stamp:%Y-%m-%d}/table={table}/"
                f"timepoint={stamp:%H%M%S}_{uuid.uuid4().hex[:8]}.json")
        store.raw_store(self.s).write(path, json.dumps({
            "query": text, "app_version": self.s.app_version, "rows": len(rows), "response": payload}))
        return rows

    # -- detail tables --------------------------------------------------------------------

    def _detail_rows(self, capacity_id, kind, timepoint):
        table = DETAIL_TABLES[kind]
        base = dict(timepoint=timeutil.dax_datetime(timepoint), capacity_id=capacity_id.upper(), table=table)
        rows = self._query_raw(capacity_id, kind, timepoint, dax("timepoint_detail", **base))
        if len(rows) < self.s.split_rows:
            return rows, ""
        # Close to the 1M-value cap: re-issue per operation name (spec 6.3). The detail table
        # has no item kind, so operation name is the split dimension.
        kinds = sorted({r.get("[operation]") for r in rows if r.get("[operation]")})
        split = []
        for k in kinds:
            literal = k.replace('"', '""')
            text = dax("timepoint_detail", **base, filters=f' && \'{table}\'[Operation] = "{literal}"')
            split.extend(self._query_raw(capacity_id, f"{kind}_split", timepoint, text))
        note = "split_by_operation"
        if any(len([r for r in split if r.get("[operation]") == k]) >= self.s.split_rows for k in kinds):
            note += "; possible_truncation"
        return split, note

    def _to_ops(self, capacity_id, kind, timepoint, rows):
        now = timeutil.iso(timeutil.utc_now())
        off = self.s.utc_offset_hours
        records = []
        seen = {}
        for row in rows:
            rec, extra = {}, {}
            for key, value in row.items():
                col = norm(key)
                if col in DATETIME_COLUMNS:
                    value = timeutil.iso(timeutil.to_utc(timeutil.parse_model_datetime(value), off)) \
                        if value else None
                (rec if col in OPS_COLUMNS else extra)[col] = value
            if not rec.get("operation_id"):
                # OneLake operations carry no Operation Id but are a real share of CU. Key them
                # on what identifies the event; the ordinal separates identical events.
                basis = "|".join(str(rec.get(c)) for c in
                                 ("item_id", "operation", "start_time", "end_time", "smoothing_start"))
                seen[basis] = seen.get(basis, 0) + 1
                digest = hashlib.sha1(f"{basis}#{seen[basis]}".encode()).hexdigest()[:24]
                rec["operation_id"] = f"syn-{digest}"
                extra["synthetic_id"] = True
            rec.update({
                "TimeGenerated": now,
                "capacity_id": capacity_id,
                "class": kind,
                "sample_timepoint": timeutil.iso(timeutil.to_utc(timepoint, off)),
                "extra": extra or None,
            })
            records.append(rec)
        return records

    def sample_detail(self, capacity_id, kind, timepoint):
        started = time.monotonic()
        try:
            rows, note = self._detail_rows(capacity_id, kind, timepoint)
            ops = self._to_ops(capacity_id, kind, timepoint, rows)
            if rows:
                missing = REQUIRED_COLUMNS - {norm(k) for k in rows[0]}
                if missing:
                    raise SchemaDrift(f"missing columns {sorted(missing)}; got {list(rows[0])}")
            store.emit(self.s, "FabricOps_CL", ops)
            self._log(capacity_id, kind, timepoint, "ok", len(ops), started, note)
            return len(ops)
        except (QueryError, SchemaDrift) as e:
            self._log(capacity_id, kind, timepoint, type(e).__name__, 0, started, str(e))
            raise

    def run_detail(self, kind):
        step = self.s.interactive_step_s if kind == "interactive" else self.s.background_step_s
        target = timeutil.to_local(timeutil.utc_now() - timedelta(minutes=self.s.lag_minutes),
                                   self.s.utc_offset_hours)

        def one_capacity(capacity_id):
            key = f"checkpoint/{capacity_id}/{kind}"
            last = (store.read_state(self.s, key) or {}).get("last_timepoint")
            last = timeutil.parse_model_datetime(last) if last else None
            for tp in timeutil.sample_grid(last, target, step, self.s.max_catchup_samples):
                self.sample_detail(capacity_id, kind, tp)
                # Advance only after a successful sample so failures are retried next run.
                store.write_state(self.s, key, {"last_timepoint": tp.isoformat()})

        self._fan_out(one_capacity)

    def run_reconcile(self):
        """Daily background pass at T-25h (spec 6.3), outside the checkpoint sequence."""
        tp = timeutil.floor_to(timeutil.to_local(timeutil.utc_now() - timedelta(hours=25),
                                                 self.s.utc_offset_hours), 30)
        self._fan_out(lambda cap: self.sample_detail(cap, "background", tp))

    # -- aggregates and dimensions ------------------------------------------------------

    def run_timepoints(self, hours=1):
        """Previous whole hour(s) of TimePoints per capacity: ground truth for validation."""
        end = timeutil.floor_to(timeutil.to_local(timeutil.utc_now() - timedelta(minutes=self.s.lag_minutes),
                                                  self.s.utc_offset_hours), 3600)
        start = end - timedelta(hours=hours)
        off = self.s.utc_offset_hours

        def one_capacity(capacity_id):
            started = time.monotonic()
            try:
                text = dax("timepoints_range", capacity_id=capacity_id.upper(),
                           start=timeutil.dax_datetime(start), end=timeutil.dax_datetime(end))
                rows = self._query_raw(capacity_id, "timepoints", start, text)
                now = timeutil.iso(timeutil.utc_now())
                records = []
                for row in rows:
                    vals = {norm(k): v for k, v in row.items()}
                    tp = vals.pop("timepoint", None)
                    records.append({
                        "TimeGenerated": now, "capacity_id": capacity_id,
                        "timepoint": timeutil.iso(timeutil.to_utc(timeutil.parse_model_datetime(tp), off)),
                        "metrics": vals,
                    })
                store.emit(self.s, "FabricTimepoints_CL", records)
                self._log(capacity_id, "timepoints", start, "ok", len(records), started)
            except QueryError as e:
                self._log(capacity_id, "timepoints", start, "QueryError", 0, started, str(e))
                raise

        self._fan_out(one_capacity)

    def run_dimensions(self):
        now = timeutil.iso(timeutil.utc_now())
        started = time.monotonic()
        rows = self._query_raw("all", "capacities", None, dax("capacities"))
        caps = []
        for row in rows:
            vals = {norm(k): v for k, v in row.items()}
            caps.append({
                "TimeGenerated": now,
                "capacity_id": (vals.pop("capacity_id", None) or "").lower(),
                "capacity_name": vals.pop("capacity_name", None),
                "sku": vals.pop("sku", None),
                "region": vals.pop("region", None),
                "attrs": vals or None,
            })
        store.emit(self.s, "FabricCapacities_CL", caps)
        self._log("all", "capacities", None, "ok", len(caps), started)

        def one_capacity(capacity_id):
            t0 = time.monotonic()
            rows = self._query_raw(capacity_id, "items", None, dax("items", capacity_id=capacity_id.upper()))
            items = []
            for row in rows:
                vals = {norm(k): v for k, v in row.items()}
                items.append({
                    "TimeGenerated": now, "capacity_id": capacity_id,
                    "item_id": vals.pop("item_id", None), "item_name": vals.pop("item_name", None),
                    "item_kind": vals.pop("item_kind", None), "workspace_id": vals.pop("workspace_id", None),
                    "workspace_name": vals.pop("workspace_name", None), "attrs": vals or None,
                })
            store.emit(self.s, "FabricItems_CL", items)
            self._log(capacity_id, "items", None, "ok", len(items), t0)

        self._fan_out(one_capacity)

    def run_smoke_test(self):
        """Daily: a background timepoint 1 h back should have rows on any capacity that ran
        anything in the last day. Zero rows on every capacity is logged as 'empty' so an
        alert can catch silent breakage (spec 6.1, decision 8)."""
        tp = timeutil.floor_to(timeutil.to_local(timeutil.utc_now() - timedelta(hours=1),
                                                 self.s.utc_offset_hours), 30)
        counts = {}
        for cap in self.s.capacity_ids:
            started = time.monotonic()
            rows, _ = self._detail_rows(cap, "background", tp)
            counts[cap] = len(rows)
            if rows:
                missing = REQUIRED_COLUMNS - {norm(k) for k in rows[0]}
                if missing:
                    self._log(cap, "smoke", tp, "SchemaDrift", len(rows), started, f"missing: {sorted(missing)}")
                    raise SchemaDrift(f"missing columns {sorted(missing)}")
            self._log(cap, "smoke", tp, "ok" if rows else "empty", len(rows), started)
        return counts

    # -- helpers --------------------------------------------------------------------------

    def _fan_out(self, fn):
        errors = []
        with ThreadPoolExecutor(max_workers=self.s.max_concurrency) as pool:
            for cap, fut in [(c, pool.submit(fn, c)) for c in self.s.capacity_ids]:
                try:
                    fut.result()
                except Exception as e:  # one capacity failing must not stop the others
                    log.exception("capacity %s failed in %s", cap, self.job)
                    errors.append(f"{cap}: {e}")
        if errors:
            raise RuntimeError("; ".join(errors))
