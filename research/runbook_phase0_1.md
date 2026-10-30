# Runbook: Phase 0 and Phase 1 (before Azure spend)

Companion to `cu_attribution_research.md`. Environment: new Microsoft 365 tenant, same work account used for Fabric and Azure.
Do the steps in order and tick them off. Record every value marked **Record** in the table at the bottom.

---

## Phase 0: Identity and tenant (no trial clocks running)

### 0.1 Confirm Fabric and Azure use the same tenant
- [ ] Azure portal → Microsoft Entra ID → Overview → copy **Tenant ID**.
- [ ] Fabric (app.fabric.microsoft.com) → `?` (Help) → About → check that the tenant ID / `ctid` in the Tenant URL is the same.
- [ ] Same page: note **"Your data is stored in"**. This is the Fabric home region. **Record**.

### 0.2 Confirm admin rights
- [ ] Entra admin center → Roles & admins → check your account has **Global Administrator**. That also makes you Fabric Administrator.

### 0.3 Security group for the harvester identity
- [ ] Entra admin center → Groups → New group
  - Type: **Security**, Name: `sg-fabric-monitor`, Membership: Assigned.

### 0.4 App registration (service principal)
- [ ] Entra admin center → App registrations → New registration
  - Name: `cuscope-harvester`, Supported account types: **Single tenant**, no redirect URI.
- [ ] **Record** Application (client) ID.
- [ ] Certificates & secrets → New client secret, 90-day expiry. Copy the value now; it's shown only once. Keep it in a password manager for now (it moves to Key Vault in Phase 2).
- [ ] **Don't** add Power BI or Fabric API permissions under "API permissions". Tenant settings and workspace roles grant access, not Entra API permissions.
- [ ] Add the app (Enterprise application `cuscope-harvester`) as a member of `sg-fabric-monitor`.

---

## Phase 1: Fabric trial (the 60-day clock starts here)

### 1.1 Start the trial
- [ ] Fabric → account picture (top right) → **Start trial**.
- [ ] Region: pick the home region from 0.1. Your Azure resources go in the same region later. You can't change this afterwards without deleting items.
- [ ] Govern → Capacities → **Trial** tab: note the size (F4 or F64) and **Record** the trial end date.
- [ ] Open the capacity and **Record** the Capacity ID (the GUID after `/capacities/` in the URL).
- [ ] If the trial is F64, consider switching it to **F4** (Change size). On F4 you can cause throttling deliberately, which you need for the throttling and outage tests.

#### If the message says "A Fabric trial isn't available for your account"
1. Admin portal → Tenant settings: turn **Users can create Fabric items** On, and under Help and support settings turn **Users can try Microsoft Fabric paid features** On. Wait 15 minutes.
2. If you already have a Power BI individual trial, the Account manager won't offer a Fabric trial. Instead, create a workspace (or use My workspace) → New item → **Lakehouse**, and accept the trial prompt if it appears.
3. If it's still blocked, the tenant isn't eligible right now. Microsoft doesn't publish the rules. Use a **pay-as-you-go F2** from Azure instead (Azure portal → Create a resource → Microsoft Fabric). Set yourself as capacity admin and use the region from 0.1. **Pause it** when you're not working to keep the cost down. Then continue at 1.2 with the F2's capacity ID.

### 1.2 Tenant settings (Admin portal → Tenant settings)
Settings can take up to 15 minutes to apply. Scope SP settings to `sg-fabric-monitor` rather than the whole organisation.

| Section | Setting | Value |
|---|---|---|
| Microsoft Fabric | Users can create Fabric items | On |
| Developer settings | Service principals can call Fabric public APIs (older name: "Service principals can use Fabric APIs") | On, specific group `sg-fabric-monitor` |
| Integration settings | Semantic Model Execute Queries REST API | On |
| Integration settings | Allow XMLA endpoints and Analyze in Excel with on-premises semantic models | On (needed for Desktop live connect / schema discovery) |
| Audit and usage settings | Azure Log Analytics connections for workspace administrators | On (needed in Phase 2 for PowerBIDatasetsWorkspace) |
| Admin API settings | Service principals can access read-only admin APIs | On, `sg-fabric-monitor` (Activity Events, inventory; optional now) |

### 1.3 Install the Capacity Metrics app
- [ ] AppSource → **Microsoft Fabric Capacity Metrics** → Get it now → Install. Install it **as yourself**, the capacity admin. The app's OAuth2 connection runs under the installer, so the installer's access decides what data the service principal can see.
- [ ] Apps → open the app → **Connect**:
  - `UTC_offset`: `0`. Keep the app on UTC so timepoints match the harvested data for every capacity; the workbook shows each viewer their own local time. (Any other value works only if `METRICS_UTC_OFFSET` is set to the same number.)
  - `RegionName` (if shown): `Default`
  - Auth: OAuth2, Privacy: Organizational → Sign in and continue.
- [ ] Open the app workspace (named like *Microsoft Fabric Capacity Metrics*) → Workspace settings → License info → make sure the type is **Pro**. Change it from Trial or PPU if needed. This is design decision 1.
- [ ] **Record** the app version (from the app's info/about page or the AppSource listing) and the workspace ID and semantic model ID (from the URL when you open the semantic model: `/groups/<ws>/datasets/<ds>`).
- [ ] Workspace → Manage access → add `cuscope-harvester` as **Member**.
- [ ] Turn off automatic app updates if the option is shown (decision 8).

### 1.4 Load generator (start this early: the app keeps only 14 days)
Create workspace `ws-loadgen` with type **Fabric Trial**. Keep each job small. The goal is a variety of operations, not volume.

- [ ] **Lakehouse** `lh_loadgen`.
- [ ] **Notebook** `nb_spark_hourly`: generates and writes ~1M synthetic rows to a Delta table. Schedule it hourly. (Spark background ops; used for the Livy-ID check.)
- [ ] **Warehouse** `wh_loadgen`: load a sample dataset. Add a pipeline with a **Script** activity that runs 3–4 aggregate queries, scheduled every 30 min. (Warehouse billing intervals; used for the queryinsights overlap.)
- [ ] **Pipeline** `pl_copy_foreach`: a Copy activity (sample data → lakehouse) plus a ForEach of ~50 Wait(1s) activities. Schedule it every 2 h. (DataMovement and ActivityRun meters.)
- [ ] **Semantic model + report** on `lh_loadgen` tables. Add a notebook `nb_dax_interactive` that runs a few DAX queries with `sempy.fabric.evaluate_dax` every 15 min. (XMLA reads are interactive ops; used for the XmlaRequestId join.) Also open the report by hand now and then.
- [ ] Optional: **Dataflow Gen2** refresh once a day (to see the staging warehouse meter).
- [ ] Keep a short log of anything you run by hand (time, item, what). It's your ground truth for validation.

### 1.5 Schema discovery
- [ ] Install Power BI Desktop (Microsoft Store).
- [ ] Get data → Power BI semantic models → the metrics model (live connection). List tables and columns and compare them with the names used in the DAX templates in `harvester/dax/` (`Timepoint Background Detail`, `Capacities[Capacity Id]`, `CapacitiesList`, `Operation Id`, ...). Save the list as `research/metrics_model_schema_<appversion>.md`.
- [ ] Capture the report's own DAX. In the app workspace, download the report (.pbix, live connected) and open it in Desktop → Optimize → **Performance Analyzer** → Start recording → go to the Timepoint detail page → **Copy query** for the detail visuals. If download is blocked, open the report in the browser, press F12 → Network, filter on `query`, and copy the request payload.
- [ ] Note the exact MPARAMETER names and the hydration query the report sends.

### 1.6 Local test of the service principal (no Azure needed)
- [ ] From your laptop, get a token for the SP (client credentials, scope `https://analysis.windows.net/powerbi/api/.default`).
- [ ] Call `POST https://api.powerbi.com/v1.0/myorg/groups/<ws>/datasets/<ds>/executeQueries` with (a) the hydration query, (b) `EVALUATE TOPN(1, 'Timepoint Background Detail')` with parameters, (c) one timepoint query for your capacity.
- [ ] Success criterion: rows come back for a timepoint at least 20 minutes old. If you get 401/403, check the 1.2 settings (wait 15 min) and the workspace role from 1.3.

**Phase 1 is done when:** the trial is running, the app is in a Pro workspace, the load generator has run for at least 24 h, the schema is documented, and the SP can pull timepoint detail rows from your laptop. Start the Azure free account (Phase 2) at that point.

---

## Recorded values

| Item | Value |
|---|---|
| Tenant ID | |
| Fabric home region | |
| SP client ID | |
| SP secret expiry | |
| Trial capacity ID | |
| Trial size / end date | |
| Metrics app version | |
| App workspace ID | |
| Metrics semantic model ID | |
| Load-gen workspace ID | |
