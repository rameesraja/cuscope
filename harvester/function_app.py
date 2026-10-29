"""Capacity attribution service: timer-triggered harvest and enrichment (spec 6).

Plain timers instead of Durable Functions: at pilot scale one invocation per job, fanning out
across capacities with a thread pool, stays well inside the 10-minute timeout, and a
checkpoint per capacity/table resumes after missed runs.

Schedules are UTC (NCRONTAB with seconds).
"""

import logging

import azure.functions as func

from cuscope import config, enrich, loadgen
from cuscope.harvest import Harvester

app = func.FunctionApp()


def _harvester(job):
    return Harvester(config.load(), job)


# Every 5 minutes; the checkpoint makes each run cover two 2.5-minute timepoints.
@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", use_monitor=True)
def harvest_interactive(timer: func.TimerRequest) -> None:
    _harvester("interactive").run_detail("interactive")


@app.timer_trigger(schedule="0 2,32 * * * *", arg_name="timer", use_monitor=True)
def harvest_background(timer: func.TimerRequest) -> None:
    _harvester("background").run_detail("background")


@app.timer_trigger(schedule="0 25 * * * *", arg_name="timer", use_monitor=True)
def harvest_timepoints(timer: func.TimerRequest) -> None:
    _harvester("timepoints").run_timepoints()


@app.timer_trigger(schedule="0 40 3 * * *", arg_name="timer", use_monitor=True)
def harvest_reconcile(timer: func.TimerRequest) -> None:
    _harvester("reconcile").run_reconcile()


# Every 6 hours: cheap (two queries), and a new deployment gets item names and SKU within
# hours instead of waiting for the next day. One run falls after the app's midnight refresh.
@app.timer_trigger(schedule="0 10 */6 * * *", arg_name="timer", use_monitor=True)
def harvest_dimensions(timer: func.TimerRequest) -> None:
    h = _harvester("dimensions")
    h.run_dimensions()
    h.run_smoke_test()


@app.timer_trigger(schedule="0 7,22,37,52 * * * *", arg_name="timer", use_monitor=True)
def enrich_fabric(timer: func.TimerRequest) -> None:
    s = config.load()
    for step in (enrich.spark_sessions, enrich.job_instances, enrich.warehouse_requests):
        try:
            step(s)
        except Exception:
            logging.exception("%s failed", step.__name__)


# Erratic interactive load for the pilot; does nothing until LOADGEN_DATASET_ID is set.
@app.timer_trigger(schedule="0 */10 * * * *", arg_name="timer", use_monitor=False)
def loadgen_interactive(timer: func.TimerRequest) -> None:
    s = config.load()
    if s.loadgen_workspace_id and s.loadgen_dataset_id:
        loadgen.erratic(s.loadgen_workspace_id, s.loadgen_dataset_id)
