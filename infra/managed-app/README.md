# Roadmap: one-click install

Status: planned. Nothing in this folder is built yet.

Today cuscope is installed by hand: fill in `cuscope.config.yml`, run `infra/deploy.sh`, then grant access in Fabric (see the main [README](../../README.md)). The goal of this work is to make installing a wizard rather than a runbook, without changing what cuscope does or where the data lives.

## Why

1. **Easier install.** A user fills in their capacity IDs and the Capacity Metrics app IDs in an Azure portal form, and Azure deploys everything.
2. **Easier upgrades.** When Microsoft changes the Capacity Metrics model, a new release should reach existing installs without a manual redeploy.
3. **Data stays with the user.** Everything runs in the user's own subscription, as it does today. The user pays only for their own Azure resources.

## Options

| Option | What it is | Trade-offs |
|---|---|---|
| **Deploy to Azure button** | A link in the README that opens the compiled template in the Azure portal, with a custom form | Simplest. No publisher account needed. Upgrades are a re-run of the button. |
| **Service catalog managed application** | A definition in the user's own tenant that installs cuscope as one resource | Good for organisations that want to install it in many subscriptions. Same template as the button. |
| **Azure Marketplace managed application (free)** | A public listing | Easiest to discover. Needs a Partner Center account, certification and a support commitment. |

The plan is to build the **Deploy to Azure button first**, because the other two reuse the same template and form.

## What we reuse

[infra/main.bicep](../main.bicep) already deploys everything: Log Analytics with the custom tables and DCR, the storage accounts, the Flex Consumption Function App with a system-assigned identity, the saved KQL functions and the Workbook. Most of the work is **packaging**, not new infrastructure.

## Work breakdown

### 1. Template and install form

- [ ] Compile: `az bicep build --file infra/main.bicep --outfile infra/managed-app/mainTemplate.json`, in CI on each release.
- [ ] Write `createUiDefinition.json` with fields for `capacityIds`, `metricsWorkspaceId`, `metricsDatasetId`, `metricsUtcOffset`, `metricsAppVersion` and `enrichWorkspaceIds`. Validate GUID format and comma-separated lists, and link to the prerequisites. Leave out the load-generator parameters; they are for testing only.
- [ ] Add the `outputs` a user needs after install: the managed identity's object ID (to grant in Fabric), the Workbook link and the Log Analytics workspace ID.
- [ ] Add the "Deploy to Azure" button to the main README.

### 2. Deliver the Function code (open design question)

Flex Consumption deploys from a blob package in the app's own `deploy` container (see `functionAppConfig.deployment` in [main.bicep](../main.bicep)). It does not support `WEBSITE_RUN_FROM_PACKAGE`, so the template needs a step that puts the release zip into that container at install time.

| Option | Notes |
|---|---|
| `Microsoft.Resources/deploymentScripts` copies the zip from the GitHub release into the `deploy` container | Needs a user-assigned identity and a script storage account. Simple, and the zip is public anyway. |
| Zip embedded in the template (base64) | The zip is small enough for the 4 MB template limit, but every release bloats the template. |
| Container image from a public registry | Changes the hosting plan and its cost. |

Pick one after a spike. The first option looks simplest for an open source release.

### 3. Fabric grants helper

Azure cannot grant access inside Fabric. Ship a script (`grant-access.ps1`, to be written) that uses the Fabric REST API to:

1. Add the managed identity to the `sg-fabric-monitor` security group.
2. Give it Member on the Capacity Metrics app workspace and Contributor on each enriched workspace.

The tenant settings (main README, step 2) still need a Fabric admin. The Workbook should get a "setup health" view driven by `FabricHarvestHealth()` that names the missing grant.

### 4. Result tables (performance)

Attribution logic (un-smoothing, warehouse idle allocation, Spark and warehouse attribution) currently runs in KQL at query time. Writing **result tables** from the harvester (for example `FabricAttributed_CL`) would make the Workbook and a future Power BI report faster and simpler, because they would read finished attribution instead of re-deriving it. Keep the KQL functions as the reference implementation and validation.

### 5. Marketplace (optional, later)

- [ ] Partner Center account enrolled in the commercial marketplace program.
- [ ] **Azure Application** offer, plan type **Managed application**, price **free**. Support contact, privacy policy and terms of use URLs are required.
- [ ] Submit for certification. Allow several weeks for review.
- [ ] Document how a user upgrades to a new version.

## Acceptance tests

Run these from a **second subscription**, signed in as a new user, not as the author.

1. Install from the form with only the documented inputs. The deployment succeeds without manual fixes.
2. After the Fabric grants, `FabricHarvestHealth()` shows no errors.
3. The user can open the Workbook, query the tables and see the outputs.
4. Install a newer version over an old one, and confirm the checkpoint and stored history survive.
5. Delete the install, and confirm the resources and the managed identity are removed.

## Risks

1. **Breakage from Microsoft changes.** The harvester reads the Capacity Metrics app's internal model through the DAX in `harvester/dax/`. If Microsoft changes it, every install stops until a fix ships. The setup-health view and GitHub issues are the signal.
2. **Microsoft may make this redundant** with native per-item attribution. Check the Fabric roadmap before investing in a Marketplace listing.
3. **Azure cost.** The user pays for the Function App, Log Analytics ingestion and storage, and the harvester's queries use some Fabric CU (about 1.9% of an F2's daily CU in the author's test). State both in the docs.
4. **Fabric grants are manual.** Install is not zero-touch until step 3 is done. Expect most support questions here.

## Out of scope

- Hosted multi-tenant SaaS. cuscope runs in the user's subscription by design.
- Fabric Workload Hub. It targets workloads that run inside Fabric, which this is not.
