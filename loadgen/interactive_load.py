"""Interactive load from your laptop, signed in as you (spec 6.8 throttling tests).

  python interactive_load.py steady                 # a handful of light queries
  python interactive_load.py spike --minutes 20     # heavy queries back to back

On an F2, a spike of 15-30 minutes should push the capacity into interactive delay; watch the
Capacity Metrics app and FabricHarvestLog_CL while it runs. Ctrl+C stops it.
Uses loadgen/deployed.json for the workspace and semantic model IDs.
"""

import argparse
import json
import sys
from pathlib import Path

from azure.identity import InteractiveBrowserCredential

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harvester"))

from cuscope import clients, loadgen  # noqa: E402
from deploy_loadgen import tenant_id  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["steady", "spike"])
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument("--size", type=int, default=3000, help="cross-join side; cost grows with size^2")
    args = parser.parse_args()

    deployed = json.loads((HERE / "deployed.json").read_text())
    cred = InteractiveBrowserCredential(tenant_id=tenant_id())
    clients.credential = lambda: cred  # run as the signed-in user, not the service principal

    ws, model = deployed["workspace_id"], deployed["semantic_model_id"]
    if args.mode == "steady":
        loadgen.steady(ws, model)
        print("done")
    else:
        loadgen.spike(ws, model, args.minutes, args.size)


if __name__ == "__main__":
    main()
