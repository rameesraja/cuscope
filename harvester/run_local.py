"""Run one harvester job from a laptop, without Functions Core Tools or any Azure resources.

Reads research/.env (tenant_id, client_id, client_secret plus harvester settings) and maps the
service principal onto the AZURE_* variables DefaultAzureCredential expects. With no
RAW_STORAGE_URL / DCR_ENDPOINT set, output goes to harvester/local_output/.

  python run_local.py interactive|background|timepoints|reconcile|dimensions|smoke|enrich|loadgen
"""

import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("CUSCOPE_ENV_FILE") or HERE.parent / "research" / ".env")


def load_env():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().strip("'\"")
        os.environ.setdefault(key.upper(), value.strip().strip("'\""))
    for short, azure in (("TENANT_ID", "AZURE_TENANT_ID"), ("CLIENT_ID", "AZURE_CLIENT_ID"),
                         ("CLIENT_SECRET", "AZURE_CLIENT_SECRET")):
        if short in os.environ:
            os.environ.setdefault(azure, os.environ[short])
    # research/.env uses the smoke-test names; map them onto the harvester settings.
    for short, setting in (("WORKSPACE_ID", "METRICS_WORKSPACE_ID"), ("DATASET_ID", "METRICS_DATASET_ID"),
                           ("CAPACITY_ID", "CAPACITY_IDS"), ("UTC_OFFSET", "METRICS_UTC_OFFSET")):
        if short in os.environ:
            os.environ.setdefault(setting, os.environ[short])
    os.environ.setdefault("LOCAL_OUTPUT_DIR", str(HERE / "local_output"))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    load_env()
    sys.path.insert(0, str(HERE))
    from cuscope import config, enrich, loadgen
    from cuscope.harvest import Harvester

    job = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    s = config.load()
    if job in ("interactive", "background"):
        Harvester(s, job).run_detail(job)
    elif job == "timepoints":
        Harvester(s, job).run_timepoints()
    elif job == "reconcile":
        Harvester(s, job).run_reconcile()
    elif job == "dimensions":
        Harvester(s, job).run_dimensions()
    elif job == "smoke":
        print(Harvester(s, job).run_smoke_test())
    elif job == "enrich":
        enrich.spark_sessions(s)
        enrich.job_instances(s)
        enrich.warehouse_requests(s)
    elif job == "loadgen":
        loadgen.steady(s.loadgen_workspace_id, s.loadgen_dataset_id)
    else:
        sys.exit(__doc__)
    print(f"done: {job}; output in {s.local_output_dir if not s.dcr_endpoint else 'Log Analytics'}")


if __name__ == "__main__":
    main()
