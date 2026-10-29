"""Timepoint grid and local/UTC conversion."""

from datetime import datetime, timedelta, timezone


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def floor_to(ts, step_s):
    epoch = int(ts.replace(tzinfo=timezone.utc).timestamp())
    return datetime.fromtimestamp(epoch - epoch % step_s, timezone.utc).replace(tzinfo=None)


def to_local(utc_ts, offset_hours):
    return utc_ts + timedelta(hours=offset_hours)


def to_utc(local_ts, offset_hours):
    return local_ts - timedelta(hours=offset_hours)


def parse_model_datetime(value):
    """executeQueries returns naive ISO strings, e.g. '2026-10-01T13:30:00'."""
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "")[:26])


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def dax_datetime(ts):
    return f"DATE({ts.year}, {ts.month}, {ts.day}) + TIME({ts.hour}, {ts.minute}, {ts.second})"


def sample_grid(last, target, step_s, max_samples):
    """Timepoints after `last` up to `target` on a `step_s` grid, oldest first, capped."""
    target = floor_to(target, step_s)
    start = floor_to(last, step_s) + timedelta(seconds=step_s) if last else target
    points = []
    t = start
    while t <= target:
        points.append(t)
        t += timedelta(seconds=step_s)
    # When behind by more than the cap, keep the oldest so the gap closes over later runs
    # instead of being skipped. Data stays in the model for 14 days.
    return points[:max_samples]
