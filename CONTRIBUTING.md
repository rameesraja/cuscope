# Contributing to cuscope

Thanks for helping. This page covers how to set up, where code lives and what a pull request needs.

## Keep tenant data out

cuscope works with real capacity data, so this rule comes first. Do not put any of the following in issues, pull requests, commits, logs or sample data:

- tenant, capacity, workspace or dataset (semantic model) IDs
- secrets, keys, connection strings or tokens
- user names, email addresses or UPNs
- names of items, workspaces or capacities specific to your organisation

Replace them with placeholders such as `<capacity-id>` or `user@contoso.com` before you post. `research/.env`, `cuscope.config.yml`, `harvester/local_output/` and `loadgen/deployed.json` are git-ignored for this reason. Keep it that way.

## Dev setup

You need Python 3.11 or later.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r harvester/requirements.txt ruff
```

`pyodbc` needs an ODBC driver manager (`unixodbc-dev` on Debian/Ubuntu). For deployment work you also need the Azure CLI with Bicep (`az bicep install`).

## Running locally

[harvester/run_local.py](harvester/run_local.py) runs one harvester job from your laptop without Functions Core Tools or any Azure resources. It reads the service principal and settings from `research/.env` (copy [research/.env.example](research/.env.example)). With no `RAW_STORAGE_URL` / `DCR_ENDPOINT` set, output goes to `harvester/local_output/`.

```bash
cd harvester
python run_local.py background     # or: interactive, timepoints, reconcile, dimensions, smoke, enrich, loadgen
```

## Where things live

| What | Where |
|---|---|
| DAX queries against the Capacity Metrics model. Table and column names from the app's model belong here only. | [harvester/dax/](harvester/dax/) |
| Harvest, enrichment and storage logic | [harvester/cuscope/](harvester/cuscope/) |
| Saved KQL functions in Log Analytics | [kql/](kql/) |
| Workbook generator (all workbook queries) | [infra/workbook/build_workbook.py](infra/workbook/build_workbook.py) |
| Azure infrastructure (Function App, Log Analytics tables, DCR, workbook) | [infra/main.bicep](infra/main.bicep) |

If the Capacity Metrics app renames a table or column, the fix should normally stay inside `harvester/dax/`.

## Changing the workbook

Edit the queries in `build_workbook.py`, not the generated JSON. Then regenerate and test:

```bash
python infra/workbook/build_workbook.py                                                # writes cuscope.workbook.json
python infra/workbook/build_workbook.py --test <workspace-customer-id> <capacity-id>   # runs every query via `az rest`
```

`--test` needs `az login` and a Log Analytics workspace with harvested data. Commit the regenerated `cuscope.workbook.json` only when the generator changed. The file contains random IDs, so regenerating it on its own only creates noise.

## Local checks

Run these before you open a pull request. CI runs the same checks.

```bash
ruff check .
az bicep build --file infra/main.bicep --stdout > /dev/null
```

## Pull requests

- One topic per pull request. Split unrelated fixes.
- Say how you tested the change (local run, deployed environment, `--test`).
- When you change DAX or KQL, state the Capacity Metrics app version you tested against.
- Update the README and CHANGELOG when behaviour or setup changes.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
