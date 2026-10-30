Attributing Fabric CU consumption to operations, from outside Fabric

Research findings and reference architecture for an Azure Functions based capacity attribution service

Author: Ramees Raja | Date: 5 September 2026 | Scope: multiple F-SKU capacities, single tenant, all workloads

## Summary

The question is "which operation caused this CU spike". Smoothing makes the answer non-obvious because a background job that finished at 02:00 is still adding load at 20:00, and an interactive query that took 3 seconds is spread across at least 5 minutes of timepoints. The Capacity Metrics app has the data to answer it but exposes one 30-second timepoint at a time, keeps 14 days, and is itself a Power BI report that becomes hard to use when the capacity that hosts it is throttled.

Findings that drive the design:

* Per-operation CU exists in exactly one place: the semantic model behind the Capacity Metrics app (Timepoint Interactive Detail, Timepoint Background Detail). Every row carries Operation Id, Total CU (s), Timepoint CU (s), start/end and the smoothing window. No REST API, no Azure Monitor metric and no Cost Management export carries this grain.
* That model is reachable from outside Fabric through the Power BI executeQueries REST API with a service principal. If the app is installed in a Pro workspace (which is what Microsoft recommends) it has no dependency on any of your F capacities, so it keeps working while a production capacity is throttled or paused. XMLA is the higher-throughput alternative but requires the app workspace to sit on a capacity.
* Fabric capacity overview events (GA, Real-Time hub) give a 30-second capacity-level feed with throttling thresholds and a per-workload breakdown, sub-minute latency, but no per-item or per-operation data. They are the alerting signal; the metrics model is the attribution source.
* Smoothing is deterministic and can be reversed. Background: Total CU / 2880 per timepoint for 24 hours from completion. Interactive: Total CU / N across a 5 to 64 minute window that the model reports as Smoothing start and Smoothing end. Once operations are harvested with those fields, both the "smoothed" view (what the capacity felt) and the "raw" view (what actually ran when) can be computed for any time range, not just one timepoint.
* Linking an operation to the actual query text depends on the workload. Power BI: Operation Id = XmlaRequestId in Azure Log Analytics (or OperationId in Workspace Monitoring). Warehouse: the link to distributed\_statement\_id was removed in 2026 when warehouse billing moved to per-minute vNode allocation; attribution is now by time overlap against queryinsights.exec\_requests\_history. Spark: the operation id has matched the Livy id; treat this as something to validate in your tenant since a community source reported the technique broke in August 2026.
* Everything Microsoft publishes for long retention (FUAM, the Chargeback app, Workspace Monitoring) runs inside Fabric and consumes CU from a Fabric capacity. None of them meets the requirement that the tool keeps working during a capacity outage, which is why the recommendation is an Azure Functions harvester writing to Azure storage plus Log Analytics or Azure Data Explorer.

The recommended build is a timer-triggered Durable Functions app that samples the metrics model per capacity every 2.5 minutes for interactive operations and every 30 minutes for background operations, de-duplicates on Operation Id, reconstructs per-timepoint contributions, enriches from Log Analytics, Spark monitoring APIs and queryinsights, and raises alerts from the capacity events feed. Estimated Azure run cost is in the tens of dollars per month for a handful of capacities. Section 6 has the design; section 7 has the numbered decisions.

## Contents

# 1. Problem statement

Several F-SKU capacities in one tenant run a mix of Spark notebooks, Data Factory pipelines, warehouses, SQL analytics endpoints and Power BI semantic models. Utilisation charts show peaks and occasional throttling, and the team cannot say with confidence which job, report or user is responsible. The tool that answers this must run outside Fabric, so that it still works when a capacity is throttled, paused, or in a background-rejection state.

Two separate questions hide inside "what caused the spike":

* What is contributing to the smoothed utilisation at timepoint T? This is what throttling reacts to. The answer includes operations that finished up to 24 hours earlier.
* What was actually running around time T, and how much did each thing cost in total? This is what an engineer needs in order to fix something.

The Capacity Metrics app answers the first question for one timepoint at a time and answers the second only indirectly. The design below stores the underlying operation records so both questions become queries over a table.

# 2. How Fabric accounts for CU (the mechanics to model)

## 2.1 Timepoints, smoothing and throttling

Capacity usage is evaluated in 30-second timepoints. A day is 2,880 timepoints. Each timepoint has a budget of SKU CU × 30 CU-seconds (an F64 has 1,920 CU-seconds per timepoint; an F2 has 60). Every operation is classified as interactive or background, and its total CU-seconds are spread over a smoothing window rather than charged at the moment they were consumed.

| **Class** | **Smoothing window** | **Typical operations** |
| --- | --- | --- |
| Interactive | Minimum 5 minutes, up to 64 minutes; the window grows with the CU of the operation. The model reports the exact window per operation as Smoothing start / Smoothing end. | Report queries, DAX/XMLA reads, SQL database in Fabric, GraphQL, User Data Functions, paginated report render in the service. |
| Background | Exactly 24 hours (2,880 timepoints) starting when the operation completes or its billing interval closes. | Spark (all operations), pipelines, Dataflow Gen2, semantic model refresh, Warehouse and SQL endpoint queries (deliberately classified background), OneLake, Eventhouse uptime, Copilot. |

Worked example from the Microsoft throttling documentation: a background job that consumed 1 CU-hour (3,600 CU-seconds) on an F2 adds 3,600 / 2,880 = 1.25 CU-seconds to each of the next 2,880 timepoints, which is 1.25 / 60 = 2.1% of the F2 budget for a full day. A query-level example published in July 2026 measured a warehouse query at 1,230.176 CU-seconds over 258 seconds of execution; the model reported 0.427 CU-seconds per timepoint, which is 1,230.176 / 2,880.

Throttling is decided on the smoothed series, using three forward-looking windows:

| **Future usage committed** | **Policy** | **Effect** |
| --- | --- | --- |
| Up to 10 minutes | Overage protection | No throttling. Capacity borrows up to 10 minutes of future budget. |
| 10 to 60 minutes | Interactive delay | New interactive operations are delayed 20 seconds at submission. |
| 60 minutes to 24 hours | Interactive rejection | New interactive operations are rejected. Background operations still start. |
| More than 24 hours | Background rejection | All new operations rejected until burndown clears the carry-forward. |

Overage in a timepoint becomes carry-forward CU. Idle budget in later timepoints burns it down. Throttling affects new operations only; in-flight operations finish and keep reporting usage. Rejected operations appear in the model with limited detail (product, user, operation id, request time) because they never ran. Surge protection, if enabled, applies an earlier cut-off on the 24-hour background percentage per capacity and optionally per workspace; it checks every 5 minutes.

## 2.2 What this does to attribution

Three consequences follow. First, a spike in the utilisation chart at 14:00 is the sum of thousands of slivers; the largest slivers are usually background jobs that finished within the last 24 hours, and the jobs that completed most recently are no more responsible than the ones that completed 20 hours ago. Second, a short interactive burst (a dashboard opened by 200 users at 09:00) does show up as a spike, but spread over at least 5 minutes, so it appears at 09:00 to 09:05 and the 30-second timepoint with the maximum value is not when the queries ran. Third, throttling state depends on projected usage over 10 minutes, 60 minutes and 24 hours; the 24-hour projection is what pushes a capacity into background rejection, and that projection is simply the sum of Total CU of every background operation completed in the trailing 24 hours divided by the daily budget.

So the useful attribution report is rarely "top operations at timepoint T". It is "total CU by item and by hour of actual execution for the trailing 24 hours, compared with the same window yesterday". The metrics model has the fields needed for both; it does not present the second.

## 2.3 Where the per-operation numbers come from, by workload

CU is metered by each workload engine with its own conversion rate. The rates matter when you want to estimate CU from telemetry you already have (Spark cores, warehouse CPU time) before the metrics model catches up, and when you sanity-check the model.

| **Workload** | **Metering basis (Fabric operations reference)** | **Classification** | **Operation identity in the metrics model** |
| --- | --- | --- | --- |
| Spark (notebooks, Spark job definitions, Lakehouse table load) | 2 Spark vCores = 1 CU. Billed on allocated cores for the session duration, one operation per session (Notebook run, Notebook HC run, Notebook pipeline run, Spark job run, ...). Autoscale Billing for Spark, if opted in, moves Spark off the capacity to a separate 0.5 CU-hour serverless meter. | Background | Operation Id has matched the Livy session id shown in the Monitoring hub and returned by the Livy sessions API. Validate; see 4.7. |
| Warehouse / SQL analytics endpoint | vNode (4 vCores) allocation measured per second inside one-minute allocation windows; 1 warehouse core = 0.538 CU. Since 2026 an "operation" is a billing interval, not a query. | Background (deliberate, for 24-hour smoothing) | Operation Id no longer maps to distributed\_statement\_id. Attribute by time overlap with queryinsights. |
| Power BI semantic models | 8 CU = 1 Power BI v-core; reported as CU processing seconds per query or refresh. | Interactive query, XMLA read: interactive. Refresh, background query, XMLA write, export, subscriptions: background. | Operation Id = XmlaRequestId (Log Analytics) = OperationId (Workspace Monitoring) = RequestId (Profiler). |
| Data Factory pipelines | DataMovement: 1.5 CU-hours per intelligent optimisation throughput unit-hour of Copy activity. ActivityRun: 0.0056 CU-hours per non-copy activity run. Reported per pipeline run, aggregated by operation type. | Background | One operation per pipeline run per meter; correlate with Job Scheduler job instances by item and time. |
| Dataflow Gen2 | "Dataflow Gen2 Run Queries" for the mashup engine, plus "High Scale Dataflow Compute - SQL Endpoint Query" when staging goes through the warehouse engine. | Background | Per refresh; correlate by item and time. |
| OneLake | Read/write/iterative/other operation meters, separate rates for redirect vs proxy and for BCDR. | Background | Aggregated per item; not attributable to a caller from the model alone. |
| Eventhouse / Eventstream | Eventhouse UpTime; Eventstream per hour, per GB, processor hours, connector vCore-hours. | Background (Eventstreams reduce allocation instead of throttling) | Per item, continuous. |

# 3. The Capacity Metrics semantic model: the only per-operation source

## 3.1 What it contains

The Capacity Metrics app is a Power BI template app. Its semantic model is DirectQuery against a Microsoft-hosted telemetry store; the app workspace only hosts the model definition and the report. Compute data is retained 14 days, storage data 30 days. New usage appears 10 to 15 minutes after the fact (community measurements land between 6 and 15 minutes). Dimension tables (capacities, workspaces, items) refresh at local midnight through the scheduled refresh; a new item that consumed CU is not visible until that refresh or a manual refresh.

The tables that matter for attribution, using the naming of the current app generation:

| **Table** | **Grain** | **Columns used** |
| --- | --- | --- |
| Capacities | One row per capacity | Capacity Id, name, SKU, region, state |
| TimePoints | One row per 30-second timepoint per capacity, 14 days | TimePoint, CU %, interactive %, background %, throttling and overage measures |
| Timepoint Interactive Detail | One row per interactive operation visible in the selected timepoint (top 100k by CU) | Operation Id, Operation, User, Status, Billing type, Operation start time, Operation end time, Duration (s), Total CU (s), Timepoint CU (s), Throttling (s), % of Base Capacity, Smoothing start, Smoothing end, virtualised item/workspace flags |
| Timepoint Background Detail | One row per background operation whose 24-hour window covers the selected timepoint (top 100k by CU) | Same columns; for in-progress operations Duration (s) and Total CU (s) reflect what has been consumed up to the timepoint |
| Items | One row per item | Item Id, Item name, Item kind, Workspace Id, Workspace name |
| Burndown / overage tables | Per timepoint per experience | Add %, Burndown %, Cumulative %, minutes to burndown |

The detail tables are parameterised. The report passes M parameters (CapacitiesList and TimePoint in the current version; CapacityID and TimePoint in 2024 builds) through the DAX MPARAMETER syntax. A query without the parameters returns nothing. The newer item-detail page additionally filters by a bucket start and end time, which means the backend supports range queries per item; the exact parameter names for that path should be captured from the app with Performance Analyzer in your tenant because Microsoft does not document them.

Two behaviours were observed by people running this at scale. The DirectQuery connection needs a first "hydration" query per session before the parameterised queries return rows (a query against Capacities with the region parameter is enough). And the backend rate-limits: one implementation hit HTTP 429 when querying 40 capacities in parallel.

## 3.2 Reaching it from outside Fabric

| **Path** | **Requirements** | **Limits** | **Works during a production capacity outage?** |
| --- | --- | --- | --- |
| Power BI REST POST /groups/{ws}/datasets/{ds}/executeQueries | Tenant setting "Semantic Model Execute Queries REST API" on; service principal allowed to use Fabric APIs; SP has Build on the model (Member/Contributor on the app workspace). App workspace can be Pro or on a capacity. | 100,000 rows or 1,000,000 values or 15 MB per query, whichever first; 120 requests per minute per identity; one DAX query per call; no DMV or INFO functions. | Yes, if the app workspace is Pro. The model is DirectQuery to Microsoft telemetry, not to your capacity. |
| XMLA endpoint (ADOMD.NET, pyadomd, Tabular Editor, DAX Studio) | App workspace on a Fabric capacity with XMLA read enabled; SP with Build. Semantic Link in a Fabric notebook uses the same route. | No row cap; DMV and INFO functions available for schema discovery; throughput limited by the backend. | Only if the app workspace sits on a capacity that is not the one under stress. A small dedicated F2 for monitoring works but is itself a Fabric dependency. |
| Power BI Desktop live connection / Analyze in Excel | Read and Build on the model. | Interactive only. | Same as the model hosting. Useful for schema discovery, not automation. |

Microsoft's install guidance is to put the app in a Pro workspace "to avoid throttling due to capacity overutilization". That advice is the same as the outage requirement in this project, so the default recommendation is Pro workspace plus executeQueries.

*Support position: the metrics app documentation states that "any consumption from, usage of, or modification of the semantic model isn't supported". Microsoft's own FUAM accelerator, the Chargeback app and several commercial tools read it anyway, and the community reports it as stable apart from schema changes between app versions. Treat it as an unsupported but widely used dependency, pin the app version, and keep the DAX in configuration.*

## 3.3 Schema drift is real

Between the 2024 and 2025 builds the table TimePointBackgroundDetail became Timepoint Background Detail, Capacities[capacityId] became Capacities[Capacity Id], the parameter CapacityID became CapacitiesList, and OperationId became Operation Id. The v47 release of September 2025 added the Operation Id column to the report tables. Any extractor needs a schema smoke test that runs before the first harvest of the day and fails loudly.

# 4. Catalogue of data sources reachable from outside Fabric

Every source that carries CU, cost or operation identity was reviewed against four criteria: grain, whether it can be read by an Azure Function, whether it keeps working when a production capacity is throttled or paused, and what it costs.

| **Source** | **Grain / what it gives** | **Read from Azure Functions** | **Independent of production capacity** | **Retention** | **Role in the design** |
| --- | --- | --- | --- | --- | --- |
| Capacity Metrics semantic model | Per operation: Total CU, Timepoint CU, smoothing window, user, item, status, billing type. Per timepoint aggregates. | Yes: executeQueries REST (or XMLA) | Yes, when app is in a Pro workspace | 14 days | Primary attribution source |
| Capacity overview events (Real-Time hub, GA) | Per capacity per 30 s: CU-ms used, base CU, three throttling threshold percentages, overage add/burndown/total, interactive vs background split, per-workload breakdown (SparkCore, AS, DMS, DI, lake, ...). State events on transitions with reason. | Indirectly: Eventstream custom endpoint (Event Hubs compatible) → Event Hub trigger | Only if the Eventstream and Eventhouse live on a separate monitoring capacity | No backfill; store it yourself | Near-real-time alerting and throttling forecast; validation of reconstructed utilisation |
| Azure Log Analytics for Power BI (PowerBIDatasetsWorkspace) | Per semantic model event: QueryEnd, ProgressReportEnd, Command, ExecutionMetrics with XmlaRequestId, CpuTimeMs, DurationMs, query text, user, application context, capacityThrottlingMs. | Yes: Azure Monitor Query SDK | Yes; data already in Azure | Configurable, up to 2 years interactive | Query text and user for Power BI operations; join key = Operation Id |
| Workspace Monitoring (Eventhouse per workspace, preview) | Semantic model logs, pipeline activity runs, copy jobs, Eventhouse and Eventstream logs, mirroring. Queries against it are not throttled. | Yes: Kusto query URI with the Kusto SDK | No: ingestion and storage bill CU to the workspace capacity; 1 to 3% of an F64 per day in one published measurement | 30 days | Alternative to Log Analytics where already enabled; cannot coexist with Log Analytics on the same workspace |
| Warehouse queryinsights.exec\_requests\_history | Per T-SQL request: distributed\_statement\_id, login, start/end, allocated\_cpu\_time\_ms, command text. | Yes: TDS with Entra token (pyodbc / Microsoft.Data.SqlClient) | No: served by the warehouse engine on the production capacity; degraded during background rejection | 30 days | Warehouse attribution by time-overlap and CPU share |
| Spark monitoring APIs (Livy sessions per workspace / notebook / SJD / lakehouse) | Per session: livyId, jobInstanceId, sparkApplicationId, submitter, item, capacityId, operationName, submitted/start/end, queued/running duration, driver and executor cores, dynamic allocation limits, state. | Yes: Fabric REST with service principal | Yes for the API; the sessions themselves ran on the capacity | Recently completed sessions (100 per item for job instances; validate for Livy list) | Spark enrichment; estimated CU from cores × duration; owner and pipeline linkage |
| Spark diagnostic emitters | Driver/executor logs, Spark event log and metrics pushed to Event Hubs, Log Analytics or Storage with artifactId, workspaceId, applicationId, userId. | Yes | Yes once emitted | Your retention | Deep Spark diagnostics; optional |
| Job Scheduler API (/items/{id}/jobs/instances) and pipeline activity-run query | Per job instance: type, invoker (Scheduled/Manual), status, rootActivityId, start/end. Pipeline activity runs per run. | Yes: Fabric REST, SP and managed identity supported | Yes for reading history | Last 100 completed per item | Pipeline and notebook run inventory; who scheduled what |
| Power BI refresh history and admin Activity Events | Refresh: requestId, type, start/end, status. Activity events: every user action with item, user, timestamp, request ids. | Yes: Power BI REST | Yes | 60 refresh entries per model; 30 days activity | Report-level usage context for interactive spikes |
| Chargeback app | Workspace, item, domain, user by day. | No; its model is declared unsupported for consumption and refreshes daily | Runs as a Power BI app | Not documented | Reference only |
| Azure Cost Management | Meter-level cost per capacity per day (Capacity Usage CU, autoscale, pay-as-you-go, Spark autoscale billing). | Yes: Cost Management exports / API | Yes | Years | Dollar reconciliation; no item grain |
| Azure Monitor platform metrics for Microsoft.Fabric/capacities | None published. Only legacy Microsoft.PowerBIDedicated Gen2 has cpu\_metric and overload\_metric. | n/a | n/a | n/a | Not usable |
| FUAM (microsoft/fabric-toolbox) | Extracts the metrics model at three grains (timepoints, operations, item-kind by day) plus activity events and inventory into a lakehouse. | No; Fabric notebooks and pipelines | No; runs on a Fabric capacity and needs the app workspace on a capacity for XMLA | Your lakehouse | Reference implementation for the DAX and the medallion layout; not the runtime |
| Commercial: SpendWeave, Measure Killer | Continuous extraction of the metrics model with schema updates maintained by the vendor; SpendWeave stores in an Eventhouse in your tenant. | Vendor-specific | SpendWeave: no (Eventhouse). Measure Killer: desktop tool. | Vendor | Buy option if the team does not want to own schema drift |

# 5. Attribution recipe per workload

The metrics model gives item, operation type, user, status and CU for everything. The recipes below add the second hop, from an operation row to the artefact an engineer can change.

## 5.1 Power BI semantic models

Interactive queries are the only workload in this estate that lands as interactive CU, so they are what produces interactive delay and rejection. Each query is one row with Operation Id. Enable Azure Log Analytics on the workspaces that matter (the workspace must sit on a Premium or Fabric capacity, and cannot have Workspace Monitoring enabled at the same time). In PowerBIDatasetsWorkspace, XmlaRequestId equals the metrics Operation Id; the ExecutionMetrics event carries totalCpuTimeMs, durationMs, approximatePeakMemConsumptionKB, capacityThrottlingMs and executionDelayMs in its EventText JSON; QueryEnd carries the DAX text and the effective user. Refreshes appear as background operations and the same join works for Command/ProgressReportEnd events. Expect about 35 GB of logs per month for an average Premium capacity per Microsoft's FAQ; filter to the workspaces that host shared models.

When Log Analytics is not enabled, the fallback is the admin Activity Events API (which report was opened by whom, when) joined by user, item and a 5-minute time bucket. It gives the report and page, not the query.

## 5.2 Spark notebooks and Spark job definitions

One Spark session is one background operation. The model reports the session's total CU, which is allocated cores × wall time / 2, including idle time on an open interactive session and including high-concurrency sessions shared by several notebooks. The Livy sessions API returns, per session, the submitter, the item, the driver and executor core configuration, the dynamic allocation ceiling, and queued versus running duration; livyId has matched the metrics Operation Id, and jobInstanceId links to the pipeline run that started it. Use the Livy list to explain a session's CU (cores, duration, idle) and to route it to an owner. Estimate CU before the model catches up as (driverCores + executorCores × numExecutors) × runningSeconds / 2; dynamic allocation makes this an upper bound.

If Spark is the dominant load and the team wants it out of the throttling equation entirely, Autoscale Billing for Spark moves it to a separate serverless meter (0.5 CU-hour rate, opt-in per capacity, maximum CU cap set by the capacity admin). Spark then no longer contributes to the 24-hour background percentage.

## 5.3 Warehouse and SQL analytics endpoint

Warehouse usage is reported as background billing intervals per warehouse or endpoint item, based on vNodes allocated per minute and measured per second. The August 2026 documentation states that the Operation Id shown in the app no longer maps to distributed\_statement\_id. Attribution therefore works in two steps: take the interval start and end from the metrics row, pull every request from queryinsights.exec\_requests\_history whose [start\_time, end\_time) overlaps the interval, and split the interval's CU across those requests in proportion to allocated\_cpu\_time\_ms (or to overlapped elapsed time when CPU is not representative, such as for queries waiting on I/O). Keep login\_name, command and distributed\_statement\_id for the report. Because vNodes are allocated per minute and released with a lag, a warehouse can bill for minutes in which no user query is running; show that as "unattributed idle allocation" rather than forcing it onto the last query.

queryinsights is served by the warehouse engine, so it is the one enrichment source in this design that can be unavailable during background rejection. Harvest it every 15 minutes into Azure storage so the history is already outside when the capacity locks up; new rows will backfill once queries resume.

## 5.4 Pipelines and Dataflow Gen2

A pipeline run produces one DataMovement operation and one ActivityRun operation per run in the model, both background. Copy activity cost is duration-based (1.5 CU-hours per throughput-unit-hour), so a copy that waits on a slow source costs the same as one that moves data. Orchestration cost is 0.0056 CU-hours per non-copy activity run; a ForEach with 5,000 iterations is 28 CU-hours regardless of what the iterations do. Notebook and Dataflow activities invoked from a pipeline bill under their own workload, not the pipeline. Join the model rows to Job Scheduler job instances (item, start, end, invoker) and to the pipeline activity-run query for the activity breakdown. Dataflow Gen2 refreshes bill as "Dataflow Gen2 Run Queries" plus, when staging is on, "High Scale Dataflow Compute - SQL Endpoint Query" under the warehouse meter; the second one is easy to miss when it shows up under a staging warehouse item.

## 5.5 OneLake, Eventhouse, Eventstream, Copilot

These report per item and cannot be attributed to a caller from the model. OneLake read and write CU is usually a by-product of Spark or warehouse work in the same window; show it alongside those operations by workspace rather than pretending to attribute it. Eventhouse uptime and Eventstream hourly meters are continuous; their share is flat and is the easiest to reason about. Copilot and AI functions appear under virtualised items.

# 6. Reference architecture: capacity attribution service on Azure Functions

## 6.1 Design constraints

* No runtime dependency on any production F capacity. The only Fabric dependencies are the metrics app in a Pro workspace and, optionally, a small monitoring capacity for the events path.
* Per-operation grain retained beyond 14 days, with the fields needed to reconstruct smoothed utilisation for any timepoint after the fact.
* Stays inside the executeQueries limits: 120 calls per minute per identity, 100k rows / 1M values / 15 MB per call.
* Schema drift in the metrics model must break a smoke test, not silently produce empty tables.

## 6.2 Component view

FABRIC TENANT AZURE (monitoring subscription)

------------------------------------ ---------------------------------------------

Pro workspace Function App (Durable, Flex Consumption)

Capacity Metrics app Orchestrator: timer, fan-out per capacity

semantic model <-- executeQueries -- MetricsHarvester (2.5 min / 30 min)

(DirectQuery to MS TimepointHarvester (hourly ranges)

telemetry store) SparkEnricher (Livy sessions, jobs)

WarehouseEnricher (queryinsights, TDS)

Production capacities F64 / F32 ... PbiEnricher (Log Analytics KQL)

workspaces --> Log Analytics (Azure) --> Attribution engine (un-smoothing, deltas)

warehouses <-- TDS, Entra token ------- Alerting (thresholds, Teams)

notebooks <-- Fabric REST ------------ |

v

Monitoring capacity F2 (optional) ADLS Gen2: raw JSON per call (replay)

Eventstream: capacity overview events Log Analytics custom tables OR ADX

custom endpoint (Event Hubs) ---------> FabricOps\_CL, FabricTimepoints\_CL,

Eventhouse (optional in-Fabric copy) FabricEvents\_CL (Event Hubs trigger)

Grafana / Workbooks / Power BI (Pro) on top

## 6.3 Harvester: sampling strategy

The detail tables are keyed by a single timepoint, so the extractor chooses which timepoints to ask for. The smoothing rules make this cheap: an interactive operation is present in at least 10 consecutive timepoints (5 minutes), and a background operation is present in 2,880 consecutive timepoints. Sampling every k-th timepoint with k at or below the minimum window length sees every operation at least once.

| **Table** | **Cadence per capacity** | **Why** | **Calls per capacity per day** |
| --- | --- | --- | --- |
| Timepoint Interactive Detail | Every 5th timepoint (2.5 minutes), lagged 20 minutes behind now | Window is at least 10 timepoints, so every operation is captured at least twice; the second capture confirms Total CU is final. The lag covers the 10 to 15 minute ingestion delay. | 576 |
| Timepoint Background Detail | Every 30 minutes, lagged 20 minutes; plus one reconciliation pass at T-25h | Every completed background operation is visible for 24 hours. The 30-minute pass gives freshness; the reconciliation pass catches long-running operations whose Total CU grew after first capture and any rows missed to 429s. | 50 |
| TimePoints aggregates | Hourly, as a range query per capacity | Smoothed CU %, interactive %, background %, throttling and overage per timepoint; the ground truth to validate reconstruction against. | 24 |
| Capacities, Items | Daily after midnight refresh | Dimension snapshot with SKU, so historic utilisation is computed against the SKU of the day. | 2 |

The 10-timepoint minimum for interactive operations comes from the throttling documentation (5-minute minimum window); measure it in week one with a coverage metric (share of operations seen at least twice) and tighten the interactive cadence if the smallest operations turn out to have shorter windows. Ten capacities cost about 6,500 calls per day, which is under 5 calls per minute against a 120 per minute allowance. Fan out per capacity with a concurrency cap of 4 and exponential back-off on 429 and 5xx; the backend behind the model has its own limits that are lower than the REST gateway's.

De-duplication key is capacity\_id + Operation Id. On conflict, keep the row with the later sample time; in-progress rows are superseded by the completed row. Store every raw response in ADLS with the request parameters in the path (capacity=/date=/table=/timepoint=.json) so the warehouse can be rebuilt after a schema change or a bug without re-querying a source that only holds 14 days.

Row limits: a busy F64 during a morning peak can hold tens of thousands of interactive rows per timepoint. At 16 columns the 1,000,000-value cap is reached at about 62,000 rows. When a response comes back truncated (HTTP 200 with a limit warning), re-issue the query split by Billing type and then by Items[Item kind]; below that, split by workspace. The app itself samples above 100k rows, so the source has the same ceiling.

## 6.4 DAX templates

Column and parameter names are those of the current app generation; put them in configuration, not code. The hydration query is issued once per Function invocation before the first parameterised query.

-- 0. hydrate the DirectQuery session

DEFINE MPARAMETER 'RegionName' = "Default"

EVALUATE SUMMARIZECOLUMNS('Capacities'[Capacity Id],

"u", [Average utilization (last 1 hour)])

-- 1. one timepoint, background operations (interactive: swap the table name)

DEFINE

MPARAMETER 'TimePoint' = DATE(2026, 9, 5) + TIME(13, 30, 0)

MPARAMETER 'CapacitiesList' = { "<capacity-guid>" }

VAR \_\_cap = TREATAS({ "<capacity-guid>" }, 'Capacities'[Capacity Id])

EVALUATE

SUMMARIZECOLUMNS(

'Timepoint Background Detail'[Operation Id],

'Timepoint Background Detail'[Operation],

'Timepoint Background Detail'[User],

'Timepoint Background Detail'[Status],

'Timepoint Background Detail'[Billing type],

'Timepoint Background Detail'[Operation start time],

'Timepoint Background Detail'[Operation end time],

'Timepoint Background Detail'[Smoothing start],

'Timepoint Background Detail'[Smoothing end],

'Items'[Workspace Id], 'Items'[Workspace name],

'Items'[Item Id], 'Items'[Item name], 'Items'[Item kind],

\_\_cap,

"duration\_s", CALCULATE(SUM('Timepoint Background Detail'[Duration (s)])),

"total\_cu\_s", CALCULATE(SUM('Timepoint Background Detail'[Total CU (s)])),

"timepoint\_cu\_s",CALCULATE(SUM('Timepoint Background Detail'[Timepoint CU (s)])),

"throttling\_s", CALCULATE(SUM('Timepoint Background Detail'[Throttling (s)]))

)

-- 2. schema smoke test: must return 1 row with the expected columns, else stop

EVALUATE TOPN(1, 'Timepoint Background Detail')

Schema discovery in a Pro workspace: executeQueries refuses INFO and DMV queries, so open the model with a Power BI Desktop live connection to list tables and columns, and use Performance Analyzer on the app's Timepoint and Timepoint Item Detail pages to copy the exact DAX and parameter names the report issues. Repeat this whenever the app version changes.

## 6.5 Storage model

| **Table** | **Key** | **Content** |
| --- | --- | --- |
| ops | capacity\_id, operation\_id | One row per operation: class (interactive/background), workspace, item, item kind, operation, user, status, billing type, start, end, smoothing\_start, smoothing\_end, duration\_s, total\_cu\_s, first\_seen, last\_seen, sample\_count, enrichment pointers (livy\_id, job\_instance\_id, xmla\_request\_id) |
| timepoints | capacity\_id, timepoint | Smoothed CU %, interactive %, background %, throttling and overage percentages from the model; SKU CU of the day |
| capacity\_events | capacity\_id, window\_start | Optional 30-second feed from capacity overview events: capacityUnitMs, three threshold percentages, overage add/burndown/total, workload breakdown JSON, state transitions |
| wh\_requests | warehouse item\_id, distributed\_statement\_id | queryinsights rows: login, start, end, allocated\_cpu\_time\_ms, command |
| spark\_sessions | livy\_id | Livy session fields: submitter, item, cores, executors, queued/running seconds, state, job\_instance\_id |
| pbi\_requests | xmla\_request\_id | From Log Analytics: dataset, user, application, query text hash, cpu\_ms, duration\_ms, throttling\_ms |
| dim\_items, dim\_capacities | item\_id / capacity\_id + valid\_from | Slowly changing snapshots from the model and the admin APIs |

Store choice. Log Analytics custom tables through the Logs Ingestion API is the low-operations option and puts the operations next to PowerBIDatasetsWorkspace for KQL joins, with Azure Monitor alerts and Workbooks out of the box; at typical volumes (100k to 1M operation rows per capacity per day, about 400 bytes each) ingestion is a few dollars per month per capacity. Azure Data Explorer is the choice if volumes exceed a few GB per day or retention beyond two years is required; a free-tier cluster covers the pilot. Azure SQL works for the ops table but is a poor fit for the per-timepoint reconstruction. Do not use a Fabric Lakehouse or Eventhouse for the primary store; that reintroduces the dependency the project exists to remove.

## 6.6 Attribution engine: reversing the smoothing

With total\_cu\_s, class, smoothing\_start and smoothing\_end per operation, the contribution of operation o to timepoint t is:

background: c\_o(t) = total\_cu\_s / 2880

for t in [smoothing\_start, smoothing\_start + 24h)

interactive: c\_o(t) = total\_cu\_s / N\_o, N\_o = (smoothing\_end - smoothing\_start) / 30s

for t in [smoothing\_start, smoothing\_end)

utilisation: U(t) = sum\_o c\_o(t) / (SKU\_CU \* 30)

raw lens: r\_o(t) = total\_cu\_s \* overlap(t, [start\_o, end\_o]) / duration\_s

Validate U(t) against timepoints.cu\_pct for the sampled timepoints and against timepoint\_cu\_s per row where sampled; differences above 1 to 2% indicate missed operations or a smoothing rule you have not modelled (in-progress background operations report partial CU; some workloads report in intervals rather than at completion). Once validated, the same table answers the questions the app cannot:

* Explain timepoint T: rank c\_o(T) by operation, then roll up by item, workspace, user, operation type. Identical to the app's Timepoint page but for any T in your retention, across capacities, in one query.
* Explain the 24-hour background percentage at T: sum total\_cu\_s for background operations with smoothing\_start in (T-24h, T], grouped by item; this is the number surge protection and background rejection act on.
* Root-cause by execution time: group the same operations by hour of start instead of by timepoint. This is where the 02:00 batch that is still weighing on the 14:00 chart shows up.
* Daily delta: total\_cu\_s per item per day versus its trailing 7-day median; alert on items whose cost doubled and on new items in the top 20.
* Forecast: given the current carry-forward from the events feed and the scheduled background operations still to smooth in, compute the timepoint at which the 10-minute and 60-minute projections cross 100% and the burndown time if nothing new starts.

// KQL sketch: reconstruct smoothed contribution per item for one hour window

let T0 = datetime(2026-09-05 13:00); let T1 = datetime(2026-09-05 14:00);

FabricOps\_CL

| where capacity\_id == "<cap>" and smoothing\_start < T1 and smoothing\_end > T0

| extend n\_tp = iff(class == "background", 2880.0, (smoothing\_end - smoothing\_start) / 30s)

| extend tp\_overlap = (min\_of(smoothing\_end, T1) - max\_of(smoothing\_start, T0)) / 30s

| extend cu\_in\_window = total\_cu\_s / n\_tp \* tp\_overlap

| summarize cu\_in\_window = sum(cu\_in\_window), ops = count()

by workspace\_name, item\_name, item\_kind, operation

| order by cu\_in\_window desc

## 6.7 Near-real-time path (optional)

Capacity overview events are the only sub-minute signal and the only place the three throttling thresholds are published as numbers. Subscribe each production capacity to one Eventstream in a monitoring workspace on a small dedicated capacity (F2 is enough; the feed is one small event per capacity per 30 seconds), add a custom endpoint destination, and read it from the Function App with the Event Hubs trigger. De-duplicate on capacityId + windowStartTime and drop windows where utilisation exceeds 500% (the documented artefact when a paused capacity flushes its smoothed usage on resume). Alert when interactiveRejectionThresholdPercentage crosses 80 and when a State event arrives with reason InteractiveDelay, InteractiveRejected, BackgroundRejected or SurgeProtectionActive. The attribution engine then explains the alert from the harvested operations.

This path does depend on Fabric (the Eventstream). It fails independently of the production capacities as long as the monitoring capacity is separate, and its failure degrades alert latency to the 15-minute metrics-model path, not attribution.

## 6.8 Behaviour during a production capacity outage

| **Scenario** | **What keeps working** | **What degrades** |
| --- | --- | --- |
| Capacity in interactive delay / rejection | Everything. Metrics model (Pro workspace) keeps receiving usage; harvester, Spark API, Log Analytics unaffected. | Nothing. |
| Capacity in background rejection | Harvester and events feed. Rejected operations appear with status Rejected and limited fields. | queryinsights over TDS may fail or be slow; use the last harvested copy. New Spark sessions cannot start, so nothing new to enrich. |
| Capacity paused | Harvester continues; model shows the flush of smoothed usage into the last window. Events feed emits a State event and then nothing. | No new usage. Expect the 288,000% artefact in events; filtered by the 500% rule. |
| Metrics model backend incident | Events feed still gives capacity-level utilisation and throttling state. | Per-operation attribution pauses; ADLS raw store and the 14-day source window allow catch-up harvest afterwards because background operations stay visible for 24 hours and everything stays in the model for 14 days. |
| Monitoring capacity (F2) throttled | Harvester, enrichment, attribution. | Events feed. Alert latency falls back to about 15 minutes. |

## 6.9 Identity, permissions, tenant settings

* One Entra service principal (or the Function App's managed identity, which is a service principal) in a security group that is allowed under "Service principals can use Fabric APIs" and "Semantic Model Execute Queries REST API".
* Member on the metrics app workspace (gives Build on the model). Viewer with Build is enough if the workspace admin prefers.
* Capacity administrator on each capacity is required to subscribe to capacity overview events and to install the app; the harvester itself does not need it.
* Viewer or Contributor on production workspaces for the Livy sessions and Job Scheduler APIs; a workspace-level read is sufficient.
* Warehouse: a database user for the service principal with VIEW DATABASE PERFORMANCE STATE or membership in the workspace Viewer role; connect over TDS with an Entra token.
* Log Analytics Reader on the workspace receiving semantic model logs; Monitoring Metrics Publisher on the data collection rule used for ingestion.
* Secrets in Key Vault; no credentials in app settings.

## 6.10 Cost of the monitoring stack

| **Component** | **Sizing basis** | **Order of magnitude per month** |
| --- | --- | --- |
| Function App, Flex Consumption | About 7,000 short executions per day per 10 capacities plus enrichment | Under 20 USD |
| ADLS Gen2 raw store | 1 to 5 GB per capacity per month, cool tier | A few USD |
| Log Analytics custom tables | 0.1 to 1 GB per capacity per day at 2.76 USD per GB ingested (pay-as-you-go), 30-day free interactive retention | 10 to 80 USD per capacity, much less on a commitment tier |
| Log Analytics for semantic models | Microsoft's figure: about 35 GB per month for an average Premium capacity | About 100 USD per capacity; scope to shared-model workspaces |
| Monitoring capacity F2 (optional, events path) | Reserved instance | About 160 USD; pay-as-you-go can be paused outside working hours if alerting is not needed then |
| Alternative: Azure Data Explorer dev cluster | Single Dev/Test SKU | About 100 to 150 USD; free tier for the pilot |

# 7. Decisions

1. Install the Capacity Metrics app in a Pro workspace and read it through executeQueries. Rationale: removes any dependency on the capacities being monitored; the model is DirectQuery to Microsoft telemetry, so a Pro workspace changes nothing about what it can see.
2. Harvest at the operation grain, not the app's aggregates. Rationale: Total CU, Smoothing start and Smoothing end per operation are sufficient to rebuild every aggregate the app shows and several it does not.
3. Sample interactive detail every 2.5 minutes and background detail every 30 minutes with a 20-minute lag and a T-25h reconciliation. Rationale: the smoothing windows guarantee coverage at this cadence; the call budget is under 5% of the REST limit for ten capacities.
4. Keep raw responses in ADLS and serve queries from Log Analytics custom tables (Azure Data Explorer if volume demands). Rationale: replayable after schema changes; KQL joins with semantic model logs; alerts and workbooks without extra components.
5. Reconstruct smoothed utilisation from operations and validate it against the model's TimePoints table before trusting attribution reports. Rationale: the validation catches unmodelled reporting rules per workload.
6. Use capacity overview events for alerting and forecast only, hosted on a separate F2 with an Eventstream custom endpoint feeding an Event Hubs trigger. Rationale: sub-minute latency and published thresholds; no per-item data, so it cannot replace the harvester.
7. Enrich Power BI operations from Azure Log Analytics (XmlaRequestId), Spark from the Livy sessions API, warehouses from queryinsights by time overlap. Rationale: these are the only documented or observed join paths; the warehouse per-query link no longer exists.
8. Pin the metrics app version, disable automatic template-app updates on that workspace, and run the schema smoke test before every harvest. Rationale: documented renames of tables, columns and parameters between versions.
9. Do not build the primary store in a Fabric Lakehouse or Eventhouse and do not adopt FUAM as the runtime. Rationale: both consume CU from a Fabric capacity and stop when that capacity is rejected; FUAM stays useful as a reference for DAX and layout.
10. Enable surge protection with a workspace-level limit on the capacities that host shared Power BI models once attribution data shows the background load distribution. Rationale: it is the only control that stops a runaway background job before it takes interactive users down with it.

# 8. Known gaps and risks

| **Risk** | **Impact** | **Mitigation** |
| --- | --- | --- |
| Metrics model is declared unsupported for external consumption | Microsoft may change the schema, the parameters or the DirectQuery connector without notice | Version pinning, smoke test, DAX in configuration, ADLS replay; FUAM and commercial tools share the exposure and act as an early-warning community |
| Warehouse per-query link removed in 2026 | Warehouse attribution is proportional, not exact | Time-overlap plus CPU share; report idle allocation separately; push the warehouse team toward workload isolation per warehouse item so item-level attribution is meaningful |
| Spark Operation Id = Livy id reported broken in August 2026 by one source | Spark enrichment falls back to item plus time-window matching | Validate in your tenant in week one; sessions per item per hour are usually few enough for time matching to be unambiguous |
| Sampling above 100k rows per timepoint on very busy capacities | Long tail of small interactive operations is aggregated by the source | Split queries by billing type, item kind, workspace; accept aggregation of the tail below a CU threshold |
| Rate limiting from the model backend below the REST gateway limit | Missed samples | Concurrency cap, back-off, reconciliation pass, coverage metric (share of operations seen at least twice) |
| Reporting timing per workload not fully documented (long-running Spark, Eventhouse uptime) | Reconstruction differs from the model for in-flight operations | Validation step against TimePoints; treat in-progress rows as provisional until Status is final |
| Log Analytics volume on large Power BI estates | Cost | Enable on the workspaces hosting shared models only; use the Activity Events API elsewhere |
| Protected sensitivity label on the app's model | Service principal queries refused | Check the label on install; one community report needed a composite model workaround for Semantic Link, not observed for executeQueries with SP |

# 9. Two-week validation plan

1. Day 1 to 2: install the app in a Pro workspace, create the service principal, grant Build, enable the tenant settings, connect Power BI Desktop live to enumerate tables and capture the report's DAX with Performance Analyzer.
2. Day 3 to 4: a single Function that runs the hydration query, the smoke test and one timepoint query per table for one capacity; write raw JSON to ADLS. Measure latency between an operation's end time and its first appearance.
3. Day 5 to 7: sampling loop for all capacities; ingestion to Log Analytics; coverage metric; compare reconstructed U(t) with TimePoints for three days.
4. Day 8 to 9: enrichment. Confirm XmlaRequestId match on Log Analytics, confirm or refute Livy id match, implement queryinsights overlap for the two busiest warehouses.
5. Day 10: subscribe capacities to overview events on a monitoring F2, custom endpoint, Event Hubs trigger, first alerts.
6. Day 11 to 12: Workbook or Grafana dashboards: explain timepoint, 24-hour background by item, hourly root cause, daily deltas. Walk through the last real throttling incident with the data.
7. Day 13 to 14: decide on surge protection thresholds and on Spark Autoscale Billing per capacity from the measured distribution; write the run-book for app version upgrades.

# Appendix A. Sources

Microsoft Learn

* Understand capacity throttling and smoothing: https://learn.microsoft.com/en-us/fabric/enterprise/throttling
* Metrics app timepoint page: https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app-timepoint-page
* Metrics app timepoint summary page: https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app-timepoint-summary-page
* Metrics app timepoint item detail page: https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app-timepoint-item-detail-page
* Metrics app overview (retention, latency, support statement): https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app
* Install the metrics app (Pro workspace guidance): https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app-install
* Fabric operations and CU rates: https://learn.microsoft.com/en-us/fabric/enterprise/fabric-operations
* Surge protection: https://learn.microsoft.com/en-us/fabric/enterprise/surge-protection
* Chargeback app: https://learn.microsoft.com/en-us/fabric/enterprise/chargeback-app
* Explore Fabric capacity overview events (schema): https://learn.microsoft.com/en-us/fabric/real-time-hub/explore-fabric-capacity-overview-events
* Capacity overview events in Real-Time hub: https://learn.microsoft.com/en-us/fabric/real-time-hub/create-streams-fabric-capacity-overview-events
* Workspace monitoring overview: https://learn.microsoft.com/en-us/fabric/fundamentals/workspace-monitoring-overview
* Azure Log Analytics for Power BI, overview and configuration: https://learn.microsoft.com/en-us/power-bi/transform-model/log-analytics/desktop-log-analytics-overview and .../desktop-log-analytics-configure and .../desktop-log-analytics-faq
* Warehouse consumption and utilisation (vNode model, 2026): https://learn.microsoft.com/en-us/fabric/data-warehouse/usage-reporting
* How to observe warehouse utilisation (Operation Id no longer maps to distributed statement id): https://learn.microsoft.com/en-us/fabric/data-warehouse/how-to-observe-utilization
* Pipeline pricing: https://learn.microsoft.com/en-us/fabric/data-factory/pricing-pipelines
* Monitor Spark capacity consumption: https://learn.microsoft.com/en-us/fabric/data-engineering/monitor-spark-capacity-consumption
* Spark monitoring APIs: https://learn.microsoft.com/en-us/fabric/data-engineering/spark-monitoring-api-overview
* Livy sessions list API: https://learn.microsoft.com/en-us/rest/api/fabric/notebook/livy-sessions/list-livy-sessions
* Spark diagnostic emitters to Event Hubs: https://learn.microsoft.com/en-us/fabric/data-engineering/azure-fabric-diagnostic-emitters-azure-event-hub
* Autoscale Billing for Spark: https://learn.microsoft.com/en-us/fabric/data-engineering/autoscale-billing-for-spark-overview
* Job Scheduler, list item job instances: https://learn.microsoft.com/en-us/rest/api/fabric/core/job-scheduler/list-item-job-instances
* Power BI executeQueries API and limits: https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/execute-queries-in-group
* Azure Monitor supported metrics, Microsoft.PowerBIDedicated: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/supported-metrics/microsoft-powerbidedicated-capacities-metrics

Microsoft blogs and open source

* Capacity health, timepoint summary, timepoint detail, chargeback GA (May 2026): https://community.fabric.microsoft.com/t5/Fabric-Updates-Blog/Providing-more-insights-amp-tools-Capacity-health-timepoint/ba-p/5176753
* Capacity Overview Events GA (August 2026): https://community.fabric.microsoft.com/blog/fbc\_fabricupdatesblogs/capacity-overview-events-in-real-time-hub-generally-available/5356729
* Understanding warehouse consumption with Capacity Metrics and Query Insights (August 2026): https://community.fabric.microsoft.com/blog/fbc\_fabricupdatesblogs/understanding-warehouse-consumption-with-capacity-metrics-and-query-insights/5357458
* New ExecutionMetrics event in Azure Log Analytics: https://community.fabric.microsoft.com/blog/fbc\_pbiupdatesblog/new-executionmetrics-event-in-azure-log-analytics-for-power-bi-semantic-models/5174476
* FUAM, Fabric Unified Admin Monitoring: https://github.com/microsoft/fabric-toolbox/blob/main/monitoring/fabric-unified-admin-monitoring/README.md
* Fabric Platform Monitoring, Capacity Events (Jumpstart): https://jumpstart.fabric.microsoft.com/catalog/fpm-capacity-events/

Community sources (methods, measurements, breakages)

* Peter Beck, Fabric Capacity Units: understanding smoothing for query-level consumption (July 2026): https://medium.com/@peterjohnbeck/fabric-capacity-units-understanding-smoothing-0541802f706c
* Chris Webb, How to get the details of Power BI operations seen in the Capacity Metrics app (September 2025): https://blog.crossjoin.co.uk/2025/09/14/how-to-get-the-details-of-power-bi-operations-seen-in-the-capacity-metrics-app/
* Chris Webb, Linking warehouse SQL queries and Spark jobs to the Capacity Metrics app, with August 2026 update: https://blog.crossjoin.co.uk/2025/09/28/linking-fabric-warehouse-sql-queries-and-spark-jobs-to-the-capacity-metrics-app/
* PBI Guy, How to extract data from the Fabric Metrics app, parts 1 and 2 (DAX and loop): https://pbi-guy.com/2024/03/30/how-to-extract-data-from-the-fabric-metrics-app-part-1/ and https://pbi-guy.com/2024/04/24/how-to-extract-data-from-the-fabric-metrics-app-part-2/
* bits2BI, Tools and tricks to connect and extract data from the Capacity Metrics app: https://bits2bi.com/2025/03/15/tools-and-tricks-to-connect-and-extract-data-from-the-fabric-capacity-metrics-app/
* bits2BI, Tracking CU consumption for DAX queries: https://bits2bi.com/2025/10/05/tracking-capacity-unit-consumption-for-dax-queries-with-fabric-notebooks/
* Bradley Schacht, Using sempy to get SQL query CU cost from the Capacity Metrics app: https://bradleyschacht.com/using-sempy-to-get-sql-query-cu-cost-from-the-fabric-capacity-metrics-app
* data-marc, Extracting and storing Fabric admin and capacity metrics data: https://data-marc.com/2025/07/30/your-metrics-your-rules-extracting-and-storing-fabric-admin-and-capacity-metrics-data-in-fabric/
* SpendWeave, Fabric capacity events in the Real-Time hub (June 2026): https://spendweave.com/blog/fabric-capacity-events-real-time-hub/ and Alternatives to the Capacity Metrics app: https://spendweave.com/blog/capacity-metrics-app-alternatives/
* DataMartIn, What does monitoring your Fabric capacity actually cost (May 2026): https://datamartin.ca/2026/05/03/fabric-monitoring-cu-cost/