#!/usr/bin/env bash
# Deploy infrastructure and the Function App code. Run from Git Bash or a Linux/macOS shell after `az login`.
#   cp cuscope.config.example.yml cuscope.config.yml    # then fill it in
#   ./infra/deploy.sh [resource-group] [region]         # arguments override the file
# Use the same region as your Fabric capacity.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${CUSCOPE_CONFIG:-$ROOT/cuscope.config.yml}"
[ -f "$CONFIG" ] || { echo "Missing $CONFIG. Copy cuscope.config.example.yml to cuscope.config.yml and fill it in." >&2; exit 1; }

WORK="$(mktemp -d)"
PY=""
for cand in python3 python py; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "Python 3 is required." >&2; exit 1; }

# Turn the flat YAML file into an ARM parameters file plus resource_group/location for the shell.
"$PY" - "$CONFIG" "$WORK/params.json" "$WORK/target.env" <<'EOF'
import json, re, sys
src, params_out, target_out = sys.argv[1:4]
cfg = {}
for n, line in enumerate(open(src, encoding="utf-8"), 1):
    line = re.sub(r"\s+#.*$", "", line.rstrip("\n")).strip()
    if not line or line.startswith("#"):
        continue
    key, sep, value = line.partition(":")
    if not sep:
        sys.exit(f"{src}:{n}: expected 'key: value'")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    cfg[key.strip()] = value

params = {
    "prefix": "prefix", "capacityIds": "capacity_ids", "metricsWorkspaceId": "metrics_workspace_id",
    "metricsDatasetId": "metrics_dataset_id", "metricsUtcOffset": "metrics_utc_offset",
    "metricsAppVersion": "metrics_app_version", "enrichWorkspaceIds": "enrich_workspace_ids",
    "loadgenWorkspaceId": "loadgen_workspace_id", "loadgenDatasetId": "loadgen_dataset_id",
    "retentionDays": "retention_days",
}
out = {}
for bicep, key in params.items():
    if key in cfg and cfg[key] != "":
        out[bicep] = {"value": int(cfg[key]) if bicep == "retentionDays" else cfg[key]}
for required in ("capacityIds", "metricsWorkspaceId", "metricsDatasetId"):
    if required not in out or set(out[required]["value"]) <= set("0-,"):
        sys.exit(f"Set {params[required]} in {src}")
json.dump({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
           "contentVersion": "1.0.0.0", "parameters": out}, open(params_out, "w"))
open(target_out, "w").write(f"CFG_RG='{cfg.get('resource_group', '')}'\nCFG_LOCATION='{cfg.get('location', '')}'\n")
EOF
. "$WORK/target.env"
PARAMS="$WORK/params.json"
# Git Bash does not translate paths that follow "@", so hand az a Windows path there.
command -v cygpath >/dev/null 2>&1 && PARAMS="$(cygpath -m "$PARAMS")"

RG="${1:-$CFG_RG}"
LOCATION="${2:-$CFG_LOCATION}"
[ -n "$RG" ] && [ -n "$LOCATION" ] || { echo "Set resource_group and location in $CONFIG (or pass them as arguments)." >&2; exit 1; }

az group create --name "$RG" --location "$LOCATION" --output none

echo "Deploying infrastructure..."
az deployment group create \
  --resource-group "$RG" \
  --name cuscope \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters "@$PARAMS" \
  --parameters location="$LOCATION" \
  --output none

out() { az deployment group show -g "$RG" -n cuscope --query "properties.outputs.$1.value" -o tsv; }
APP="$(out functionAppName)"
PRINCIPAL="$(out functionPrincipalId)"

echo "Packaging and deploying function code to $APP..."
PKG="$WORK/harvester.zip"
(cd "$ROOT/harvester" && "$PY" - "$PKG" <<'EOF'
import sys, zipfile, pathlib
skip = {"local_output", "__pycache__", ".venv"}
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for p in pathlib.Path(".").rglob("*"):
        if p.is_file() and not skip & set(p.parts) and p.name not in {"run_local.py", "local.settings.json"}:
            z.write(p, p.as_posix())
EOF
)
# Role assignments can take a few minutes to propagate; retry the code deploy until they have.
for attempt in 1 2 3 4; do
  if az functionapp deployment source config-zip -g "$RG" -n "$APP" --src "$PKG" --build-remote true --output none; then
    break
  fi
  [ "$attempt" = 4 ] && { echo "Code deploy failed after 4 attempts" >&2; exit 1; }
  echo "Deploy attempt $attempt failed (role propagation?); retrying in 60s..."
  sleep 60
done

cat <<EOF

Done.
  Function App:            $APP
  Managed identity (SP):   $PRINCIPAL
  Log Analytics workspace: $(out logAnalyticsWorkspaceName)

Next (README step 5): add the managed identity to sg-fabric-monitor and give it
Member on the metrics app workspace and Contributor on each enriched workspace.
EOF
