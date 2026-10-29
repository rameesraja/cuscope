"""Runbook step 1.6: prove the service principal can read the Capacity Metrics model.

Standard library only. Reads research/.env, runs:
  1. token (client credentials)
  2. GET dataset metadata          -> SP has access to the app workspace
  3. hydration query                -> DirectQuery session works
  4. one timepoint, interactive and background detail -> per-operation rows come back
Raw responses are written to research/output/raw/... for later replay.

DAX lives in harvester/dax/*.dax (shared with the harvester); update it after schema discovery (runbook 1.5).

Usage:
  python sp_smoke_test.py                              # timepoint = now - 30 min
  python sp_smoke_test.py --timepoint "2026-10-01 13:30:00"
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
ENV_FILE = Path(os.environ.get("CUSCOPE_ENV_FILE") or RESEARCH / ".env")
DAX_DIR = RESEARCH.parent / "harvester" / "dax"  # shared with the harvester
RAW_DIR = RESEARCH / "output" / "raw"

PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
PBI_API = "https://api.powerbi.com/v1.0/myorg"
DETAIL_TABLES = {
    "interactive": "Timepoint Interactive Detail",
    "background": "Timepoint Background Detail",
}

REQUIRED = ["tenant_id", "client_id", "client_secret", "workspace_id", "dataset_id", "capacity_id"]


def load_env(path):
    """Tolerant KEY=VALUE parser: ignores case, spaces and quotes around keys and values."""
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().strip("'\"").lower()
        env[key] = value.strip().strip("'\"")
    return env


def http(method, url, token=None, body=None, form=None):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(text)
            except ValueError:
                return e.code, {"raw": text}
        except urllib.error.URLError as e:  # DNS / connection failures: retry, then give up
            if attempt == 5:
                raise
            print(f"     network error ({e.reason}), retry {attempt}")
            time.sleep(3 * attempt)


def get_token(env):
    status, payload = http(
        "POST",
        f"https://login.microsoftonline.com/{env['tenant_id']}/oauth2/v2.0/token",
        form={
            "grant_type": "client_credentials",
            "client_id": env["client_id"],
            "client_secret": env["client_secret"],
            "scope": PBI_SCOPE,
        },
    )
    if status != 200:
        fail("token", status, {k: payload.get(k) for k in ("error", "error_description")})
    return payload["access_token"]


def execute_query(env, token, dax):
    url = f"{PBI_API}/groups/{env['workspace_id']}/datasets/{env['dataset_id']}/executeQueries"
    return http("POST", url, token, {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}})


def rows_of(payload):
    """Return (rows, error) from an executeQueries response; errors can arrive with HTTP 200."""
    if "error" in payload:
        return None, payload["error"]
    result = payload["results"][0]
    if "error" in result:
        return None, result["error"]
    tables = result.get("tables") or [{}]
    return tables[0].get("rows", []), tables[0].get("error")


def dax_datetime(ts):
    return f"DATE({ts.year}, {ts.month}, {ts.day}) + TIME({ts.hour}, {ts.minute}, {ts.second})"


def save_raw(capacity_id, table, ts, dax, status, payload):
    path = (RAW_DIR / f"capacity={capacity_id}" / f"date={ts:%Y-%m-%d}" / f"table={table}"
            / f"timepoint={ts:%H%M%S}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"query": dax, "http_status": status, "response": payload}, indent=2))
    return path


def fail(step, status, detail):
    print(f"FAIL [{step}] HTTP {status}")
    print(json.dumps(detail, indent=2)[:2000])
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timepoint", help="Timepoint in the app's local time (UTC_offset), 'YYYY-MM-DD HH:MM:SS'")
    args = parser.parse_args()

    env = load_env(ENV_FILE)
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        print(f"Missing in {ENV_FILE}: {', '.join(missing)}")
        sys.exit(1)

    # The app shows timepoints in its UTC_offset; whether the TimePoint parameter expects
    # local or UTC is one of the things this test establishes.
    if args.timepoint:
        ts = datetime.strptime(args.timepoint, "%Y-%m-%d %H:%M:%S")
    else:
        offset = float(env.get("utc_offset", "0"))
        ts = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=offset) - timedelta(minutes=30)
    ts = ts.replace(second=0 if ts.second < 30 else 30, microsecond=0)

    token = get_token(env)
    print("OK   [token]")

    status, meta = http("GET", f"{PBI_API}/groups/{env['workspace_id']}/datasets/{env['dataset_id']}", token)
    if status != 200:
        fail("dataset access", status, meta)
    print(f"OK   [dataset access] {meta.get('name')}")

    dax = (DAX_DIR / "hydrate.dax").read_text()
    status, payload = execute_query(env, token, dax)
    rows, err = rows_of(payload) if status == 200 else (None, payload)
    save_raw(env["capacity_id"], "hydrate", ts, dax, status, payload)
    if rows is None:
        fail("hydrate", status, err)
    print(f"OK   [hydrate] {len(rows)} capacities visible: {[r.get('Capacities[Capacity name]') for r in rows]}")

    template = (DAX_DIR / "timepoint_detail.dax").read_text()
    for kind, table in DETAIL_TABLES.items():
        dax = template.format(timepoint=dax_datetime(ts), capacity_id=env["capacity_id"].upper(), table=table,
                              filter_vars="", filters="")
        status, payload = execute_query(env, token, dax)
        path = save_raw(env["capacity_id"], kind, ts, dax, status, payload)
        rows, err = rows_of(payload) if status == 200 else (None, payload)
        if rows is None:
            fail(kind, status, err)
        cols = list(rows[0].keys()) if rows else []
        print(f"OK   [{kind} @ {ts}] {len(rows)} rows -> {path.relative_to(RESEARCH)}")
        if cols:
            print(f"     columns: {cols}")
        if err:
            print(f"     warning: {err}")

    print("\nDone. 0 rows with no error usually means the timepoint is too recent, outside 14 days,"
          " or the TimePoint parameter expects a different time zone; retry with --timepoint.")


if __name__ == "__main__":
    main()
