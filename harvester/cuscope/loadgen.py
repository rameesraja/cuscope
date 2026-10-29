"""Interactive load against the load-gen semantic model (loadgen/deploy_loadgen.py creates it).

executeQueries calls are interactive operations, so this produces the interactive rows the
harvester needs. 'erratic' runs on a timer and follows a business-day profile with random
dashboard storms; 'spike' is for deliberately pushing a small capacity into interactive
delay/rejection (spec 6.8 tests). Run spikes from a laptop with loadgen/interactive_load.py,
never from the timer.

The mood functions below are mirrored in loadgen/items (notebook in Python, warehouse pipeline
in T-SQL, copy pipeline in expressions); keep them in step so all items share the same days.
"""

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .clients import QueryError, SemanticModel

log = logging.getLogger(__name__)

# The pilot's users work in the capacity's local time (the metrics app runs at UTC-6).
LOCAL_OFFSET = timedelta(hours=-6)

STEADY = [
    "EVALUATE ROW(\"n\", COUNTROWS('Numbers'))",
    "EVALUATE TOPN(100, SUMMARIZECOLUMNS('Numbers'[Bucket], \"total\", [Total]), [total], DESC)",
    "EVALUATE ROW(\"x\", SUMX(FILTER('Numbers', MOD('Numbers'[Value], {m}) = 0), 'Numbers'[Value]))",
]

# Formula-engine heavy: a {n} x {n} cross join evaluated row by row.
SPIKE = ("EVALUATE ROW(\"x\", SUMX(CROSSJOIN("
         "SELECTCOLUMNS(GENERATESERIES(1, {n}), \"a\", [Value]), "
         "SELECTCOLUMNS(GENERATESERIES(1, {n}), \"b\", [Value])), MOD([a] * [b], 7)))")


MAX_MOOD = 8.0
BURST_WORKERS = 6


def day_factor(now):
    """One regime per date, shared by every item so busy days stack: quiet 20%, normal 40%,
    heavy 30%, overload 10% of weekdays. Weekends are quiet."""
    local = now + LOCAL_OFFSET
    if local.weekday() >= 5:
        return 0.3
    doy = local.timetuple().tm_yday
    bucket = (doy * doy * 31 + doy * 17) % 101 % 10
    return 0.4 if bucket < 2 else 1.0 if bucket < 6 else 1.8 if bucket < 9 else 2.6


def hour_factor(now):
    hour = (now + LOCAL_OFFSET).hour
    return 1.5 if 8 <= hour < 18 else 0.8 if 18 <= hour < 22 else 0.4


def storm_factor(now):
    """About one 2-hour block in ten is a storm (month-end close, a backfill, a launch) in which
    every item runs about 4x its mood at the same time, so the burst lands in the same timepoints."""
    local = now + LOCAL_OFFSET
    block = local.timetuple().tm_yday * 12 + local.hour // 2
    return 4.0 if (block * block * 31 + block * 17 + 5) % 101 % 10 == 9 else 1.0


def mood_of(now):
    return min(MAX_MOOD, day_factor(now) * hour_factor(now) * storm_factor(now))


def erratic(workspace_id, dataset_id, now=None, rng=random):
    """One 10-minute tick: a mood-scaled trickle of queries; in storms (and now and then
    outside them) a concurrent burst of heavy ones that pushes interactive CU past the F2."""
    now = now or datetime.now(timezone.utc)
    mood = mood_of(now)
    storm = storm_factor(now) > 1
    queries = [_steady_query(rng) for _ in range(rng.randint(0, round(8 * mood)))]
    if rng.random() < (0.5 if storm else 0.04 * mood):
        # Many people opening the same report at once: a burst of mixed and heavier queries.
        queries += [_steady_query(rng) if rng.random() < 0.4 else SPIKE.format(n=rng.randint(1200, 3000))
                    for _ in range(rng.randint(30, 90))]
        log.info("loadgen burst: %d queries (mood %.2f, storm %s)", len(queries), mood, storm)
    model = SemanticModel(workspace_id, dataset_id)
    deadline = time.monotonic() + 240  # well inside the 10-minute function timeout

    def run(text):
        if time.monotonic() > deadline:
            return
        try:
            model.query(text)
        except QueryError as e:
            log.warning("loadgen query failed: %s", e)

    # Concurrent, so the CU of a burst is spent in a few timepoints instead of being spread thin.
    with ThreadPoolExecutor(BURST_WORKERS) as pool:
        list(pool.map(run, queries))


def _steady_query(rng):
    return rng.choice(STEADY).replace("{m}", str(rng.randint(3, 97)))


def steady(workspace_id, dataset_id, queries=6):
    model = SemanticModel(workspace_id, dataset_id)
    for _ in range(queries):
        try:
            model.query(_steady_query(random))
        except QueryError as e:
            log.warning("loadgen query failed: %s", e)


def spike(workspace_id, dataset_id, minutes, size=3000, pause_s=0.0):
    model = SemanticModel(workspace_id, dataset_id)
    end = time.monotonic() + minutes * 60
    count = 0
    while time.monotonic() < end:
        t0 = time.monotonic()
        try:
            model.query(SPIKE.format(n=size))
            status = "ok"
        except QueryError as e:
            # Rejections during throttling are expected and are part of what the test measures.
            status = f"error: {str(e)[:120]}"
        count += 1
        print(f"{count:4d}  {time.monotonic() - t0:6.1f}s  {status}", flush=True)
        time.sleep(pause_s)
