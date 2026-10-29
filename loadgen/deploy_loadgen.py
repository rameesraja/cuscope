"""Create the load-generator workspace and items on your Fabric capacity (runbook 1.4).

Runs as you (browser sign-in), not as the service principal, so the items and schedules are
owned by a real user. Safe to re-run: existing items are kept and only missing ones created.

  python deploy_loadgen.py [--workspace-name dev] [--capacity-id <guid>]
  python deploy_loadgen.py --update     # push loadgen/items to the existing items in deployed.json

Uses the existing workspace if found (it must already be on a Fabric capacity); otherwise
creates it on --capacity-id. Creates: small Spark pool (set as the workspace default), lh_loadgen, wh_loadgen, nb_spark_events (every 3 h),
pl_copy_foreach (every 2 h), pl_wh_queries (every 30 min), sm_loadgen (calculated-table
semantic model, refreshed daily). Writes the IDs to loadgen/deployed.json.

--update signs in as the service principal from research/.env, because the schedules run as it
and a pipeline runs as the identity that last saved it. Schedules are left as they are; the
items themselves vary their work per run (see harvester/cuscope/loadgen.py).
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from azure.identity import ClientSecretCredential, InteractiveBrowserCredential

HERE = Path(__file__).resolve().parent
ITEMS = HERE / "items"
ENV_FILE = Path(os.environ.get("CUSCOPE_ENV_FILE") or HERE.parent / "research" / ".env")
FABRIC = "https://api.fabric.microsoft.com/v1"
PBI = "https://api.powerbi.com/v1.0/myorg"
SCHEDULE_DAYS = 60


def env(key):
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and line.split("=", 1)[0].strip().strip("'\"").lower() == key:
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit(f"{key} not found in {ENV_FILE}")


def tenant_id():
    return env("tenant_id")


class Api:
    def __init__(self, tenant, cred=None):
        self.cred = cred or InteractiveBrowserCredential(tenant_id=tenant)

    def _headers(self, scope):
        return {"Authorization": f"Bearer {self.cred.get_token(scope).token}"}

    def call(self, method, url, body=None, scope="https://api.fabric.microsoft.com/.default", ok_404=False):
        for attempt in range(8):
            resp = requests.request(method, url, json=body, headers=self._headers(scope), timeout=120)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                continue
            break
        if resp.status_code == 202 and "Location" in resp.headers and method == "POST" and "/jobs/instances" not in url:
            return self._wait(resp)
        if ok_404 and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:800]}")
        return resp.json() if resp.content else {}

    def _wait(self, resp):
        """Poll a Fabric long-running operation and return its result."""
        op_url = resp.headers["Location"]
        while True:
            time.sleep(float(resp.headers.get("Retry-After", 5)))
            resp = requests.get(op_url, headers=self._headers("https://api.fabric.microsoft.com/.default"), timeout=60)
            status = resp.json().get("status")
            if status == "Succeeded":
                result = requests.get(f"{op_url}/result",
                                      headers=self._headers("https://api.fabric.microsoft.com/.default"), timeout=60)
                return result.json() if result.ok and result.content else {}
            if status in ("Failed", "Undefined"):
                raise RuntimeError(f"operation failed: {resp.text[:800]}")

    def fabric(self, method, path, body=None, **kw):
        return self.call(method, f"{FABRIC}/{path}", body, **kw)

    def pbi(self, method, path, body=None):
        return self.call(method, f"{PBI}/{path}", body, scope="https://analysis.windows.net/powerbi/api/.default")


def b64(text):
    return base64.b64encode(text.encode()).decode()


def render(name, ids):
    text = (ITEMS / name).read_text(encoding="utf-8")
    for key, value in ids.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def ensure_workspace(api, name, capacity_id):
    """Reuse an existing workspace as-is (never reassigned), or create one on `capacity_id`."""
    for ws in api.fabric("GET", "workspaces").get("value", []):
        if ws["displayName"] == name:
            current = ws.get("capacityId", "")
            if not current:
                sys.exit(f"Workspace {name} is not on a Fabric capacity. Assign it to your F2 in "
                         "Workspace settings > License info, then re-run.")
            if capacity_id and current.lower() != capacity_id.lower():
                sys.exit(f"Workspace {name} is on capacity {current}, not {capacity_id}. "
                         "Fix the assignment or the --capacity-id argument, then re-run.")
            print(f"  workspace {name} exists on capacity {current}; using it")
            return ws["id"]
    if not capacity_id:
        sys.exit(f"Workspace {name} not found; pass --capacity-id to create it.")
    ws = api.fabric("POST", "workspaces", {"displayName": name, "capacityId": capacity_id})
    print(f"  workspace {name} created")
    return ws["id"]


def ensure_item(api, ws, collection, item_type, name, definition=None):
    for item in api.fabric("GET", f"workspaces/{ws}/items?type={item_type}").get("value", []):
        if item["displayName"] == name:
            print(f"  {name} exists")
            return item["id"]
    body = {"displayName": name}
    if definition:
        body["definition"] = definition
    item = api.fabric("POST", f"workspaces/{ws}/{collection}", body)
    if not item.get("id"):  # some LROs return no body; look it up
        item = next(i for i in api.fabric("GET", f"workspaces/{ws}/items?type={item_type}")["value"]
                    if i["displayName"] == name)
    print(f"  {name} created")
    return item["id"]


def ensure_spark_pool(api, ws):
    """Small pool so one session fits an F2's Spark allowance; failure is not fatal."""
    try:
        pools = api.fabric("GET", f"workspaces/{ws}/spark/pools").get("value", [])
        if not any(p["name"] == "pool_small" for p in pools):
            api.fabric("POST", f"workspaces/{ws}/spark/pools", {
                "name": "pool_small", "nodeFamily": "MemoryOptimized", "nodeSize": "Small",
                "autoScale": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 2},
                "dynamicExecutorAllocation": {"enabled": True, "minExecutors": 1, "maxExecutors": 1},
            })
        api.fabric("PATCH", f"workspaces/{ws}/spark/settings",
                   {"pool": {"defaultPool": {"name": "pool_small", "type": "Workspace"}}})
        print("  spark pool_small set as workspace default")
    except RuntimeError as e:
        print(f"  WARNING spark pool not configured ({e}); set a small pool by hand in workspace settings")


def ensure_schedule(api, ws, item_id, fallback_job_type, interval_min, start_in_min):
    start = datetime.now(timezone.utc) + timedelta(minutes=start_in_min)
    body = {
        "enabled": True,
        "configuration": {
            "type": "Cron", "interval": interval_min, "localTimeZoneId": "UTC",
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": (start + timedelta(days=SCHEDULE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    last = None
    for job_type in ("DefaultJob", fallback_job_type):
        try:
            existing = api.fabric("GET", f"workspaces/{ws}/items/{item_id}/jobs/{job_type}/schedules", ok_404=True)
            if existing and existing.get("value"):
                print(f"    schedule exists ({job_type})")
                return
            api.fabric("POST", f"workspaces/{ws}/items/{item_id}/jobs/{job_type}/schedules", body)
            print(f"    scheduled every {interval_min} min ({job_type})")
            return
        except RuntimeError as e:
            last = e
    print(f"    WARNING schedule not created: {last}")


def run_now(api, ws, item_id, job_type):
    api.fabric("POST", f"workspaces/{ws}/items/{item_id}/jobs/instances?jobType={job_type}")


def definitions(ids):
    """Definitions of the notebook and pipelines, keyed by their deployed.json ID field."""
    return {
        "notebook_id": {"parts": [{"path": "notebook-content.py", "payload": b64(render("nb_spark_events.py", ids)),
                                   "payloadType": "InlineBase64"}]},
        "pl_copy_foreach_id": {"parts": [{"path": "pipeline-content.json",
                                          "payload": b64(render("pl_copy_foreach.json", ids)),
                                          "payloadType": "InlineBase64"}]},
        "pl_wh_queries_id": {"parts": [{"path": "pipeline-content.json",
                                        "payload": b64(render("pl_wh_queries.json", ids)),
                                        "payloadType": "InlineBase64"}]},
    }


def update():
    deployed = json.loads((HERE / "deployed.json").read_text())
    api = Api(None, ClientSecretCredential(env("tenant_id"), env("client_id"), env("client_secret")))
    ws = deployed["workspace_id"]
    ids = {"WORKSPACE_ID": ws, "LAKEHOUSE_ID": deployed["lakehouse_id"], "WAREHOUSE_ID": deployed["warehouse_id"],
           "WAREHOUSE_ENDPOINT": deployed["warehouse_endpoint"]}
    for key, definition in definitions(ids).items():
        api.fabric("POST", f"workspaces/{ws}/items/{deployed[key]}/updateDefinition", {"definition": definition})
        print(f"  {key.removesuffix('_id')} updated")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-name", default="dev")
    parser.add_argument("--capacity-id", help="only needed if the workspace doesn't exist yet")
    parser.add_argument("--update", action="store_true", help="push item definitions to the deployed items")
    args = parser.parse_args()
    if args.update:
        update()
        return

    api = Api(tenant_id())
    print("Workspace")
    ws = ensure_workspace(api, args.workspace_name, args.capacity_id)
    ensure_spark_pool(api, ws)

    print("Storage items")
    lakehouse = ensure_item(api, ws, "lakehouses", "Lakehouse", "lh_loadgen")
    warehouse = ensure_item(api, ws, "warehouses", "Warehouse", "wh_loadgen")
    endpoint = None
    for _ in range(30):  # connection string appears once the warehouse is provisioned
        endpoint = (api.fabric("GET", f"workspaces/{ws}/warehouses/{warehouse}").get("properties") or {}) \
            .get("connectionString")
        if endpoint:
            break
        time.sleep(10)
    ids = {"WORKSPACE_ID": ws, "LAKEHOUSE_ID": lakehouse, "WAREHOUSE_ID": warehouse,
           "WAREHOUSE_ENDPOINT": endpoint or ""}
    defs = definitions(ids)

    print("Notebook")
    notebook = ensure_item(api, ws, "notebooks", "Notebook", "nb_spark_events", defs["notebook_id"])
    # First run now so synthetic_events exists before the pipelines and warehouse need it.
    run_now(api, ws, notebook, "RunNotebook")
    print("    first run started")
    ensure_schedule(api, ws, notebook, "RunNotebook", interval_min=180, start_in_min=180)

    print("Pipelines")
    copy_pl = ensure_item(api, ws, "dataPipelines", "DataPipeline", "pl_copy_foreach", defs["pl_copy_foreach_id"])
    ensure_schedule(api, ws, copy_pl, "Pipeline", interval_min=120, start_in_min=30)
    wh_pl = ensure_item(api, ws, "dataPipelines", "DataPipeline", "pl_wh_queries", defs["pl_wh_queries_id"])
    ensure_schedule(api, ws, wh_pl, "Pipeline", interval_min=30, start_in_min=30)

    print("Semantic model")
    model = ensure_item(api, ws, "semanticModels", "SemanticModel", "sm_loadgen", {
        "format": "TMSL",
        "parts": [
            {"path": "model.bim", "payload": b64(render("sm_loadgen.bim", ids)), "payloadType": "InlineBase64"},
            {"path": "definition.pbism", "payload": b64(json.dumps({"version": "4.0", "settings": {}})),
             "payloadType": "InlineBase64"},
        ]})
    try:
        api.pbi("POST", f"groups/{ws}/datasets/{model}/refreshes", {"notifyOption": "NoNotification"})
        api.pbi("PATCH", f"groups/{ws}/datasets/{model}/refreshSchedule", {"value": {
            "enabled": True, "localTimeZoneId": "UTC", "times": ["06:00"],
            "days": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]}})
        print("    refreshed now and scheduled daily 06:00 UTC")
    except RuntimeError as e:
        print(f"    WARNING refresh/schedule: {e}")

    out = {"workspace_id": ws, "lakehouse_id": lakehouse, "warehouse_id": warehouse,
           "warehouse_endpoint": endpoint, "notebook_id": notebook, "pl_copy_foreach_id": copy_pl,
           "pl_wh_queries_id": wh_pl, "semantic_model_id": model}
    (HERE / "deployed.json").write_text(json.dumps(out, indent=2))
    print(f"\nWrote {HERE / 'deployed.json'}")
    print("Check the notebook's first run in the Monitoring hub. Pipelines start in 30 minutes.")


if __name__ == "__main__":
    main()
