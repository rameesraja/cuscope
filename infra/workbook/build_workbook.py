"""Generate the Azure Workbook (cuscope.workbook.json) from the queries below.

Every view is one KQL query over the saved functions in kql/. The same queries are meant to
be reused by the Power BI report, so keep the logic here, not in the workbook JSON.

  python build_workbook.py            # writes cuscope.workbook.json (deployed by main.bicep)
  python build_workbook.py --test     # runs every query against Log Analytics via `az rest`

Workbook parameters used in queries: {Capacity}, {TimeRange:start|end}, {Window:start|end}.
"""

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "cuscope.workbook.json"
LA_TYPE = "microsoft.operationalinsights/workspaces"
# Queries filter time explicitly from the parameters; the item time context only has to be
# wide enough not to cut off harvested rows (the metrics app keeps 14 days).
QUERY_SPAN_MS = 30 * 24 * 3600 * 1000

PRELUDE = """let cap = '{Capacity}';
let t0 = {TimeRange:start};
let t1 = {TimeRange:end};
let w0 = {Window:start};
let w1 = {Window:end};
"""
# The portal expands time-range parameters to KQL expressions (ago(12h), now(), datetime(...)),
# not strings, so they must not be quoted or passed through todatetime().

Q = {}

# Smoothed utilisation as the capacity felt it, split by class, next to the app's own figure.
# Background: rolling 24 h sum of operations by smoothing start (series_fir over 30 s steps).
# Interactive: each operation spread evenly over its own smoothing window.
# Points are averaged into bins of at least 30 s, at most ~1500 per series, because the chart
# stops drawing above 10,000 points (5 series x 2880 timepoints is already 14,400 for 24 h).
Q["utilisation"] = PRELUDE + """let budget = todouble(toscalar(FabricSkuCu(cap))) * 30.0;
let step = 30s * toint(ceiling(((t1 - t0) / 30s) / 1500.0));
let ops = FabricOpsLatest() | where capacity_id == cap;
let bg = ops
    | where class == "background" and smoothing_start between ((t0 - 24h) .. t1)
    | make-series cu = sum(total_cu_s) default = 0 on smoothing_start from bin(t0 - 24h, 30s) to t1 step 30s
    | extend roll = series_fir(cu, repeat(1, 2880), false, false)
    | mv-expand timepoint = smoothing_start to typeof(datetime), roll to typeof(double)
    | where timepoint >= t0
    | project timepoint, background = 100.0 * roll / 2880.0 / budget;
let it = ops
    | where class == "interactive" and smoothing_end > t0 and smoothing_start < t1
    | extend n = toint(n_tp)
    | mv-expand k = range(0, n - 1, 1) to typeof(int)
    | extend timepoint = smoothing_start + k * 30s
    | where timepoint between (t0 .. t1)
    | summarize interactive = 100.0 * sum(total_cu_s / n_tp) / budget by timepoint = bin(timepoint, 30s);
let app = FabricTimepoints_CL
    | where capacity_id =~ cap and timepoint between (t0 .. t1)
    | summarize arg_max(TimeGenerated, metrics) by timepoint
    | project timepoint, app = 100.0 * todouble(metrics.cu_ratio);
union
    (bg | project timepoint, background, interactive = 0.0, app = real(null)),
    (it | project timepoint, background = 0.0, interactive, app = real(null)),
    (app | project timepoint, background = real(null), interactive = real(null), app)
| summarize background = sum(background), interactive = sum(interactive), app = max(app) by timepoint
| summarize ['Background %'] = round(avg(background), 2), ['Interactive %'] = round(avg(interactive), 2),
            ['App utilisation %'] = round(avg(app), 2) by timepoint = bin(timepoint, step)
| extend ['Reconstructed total %'] = ['Background %'] + ['Interactive %'], ['Throttle limit %'] = 100.0
| order by timepoint asc"""

# One row per tile (Metric, Value) so the workbook can draw them as tiles.
Q["kpis"] = PRELUDE + """let app = FabricTimepoints_CL
    | where capacity_id =~ cap and timepoint between (t0 .. t1)
    | summarize arg_max(TimeGenerated, metrics) by timepoint
    | extend p = 100.0 * todouble(metrics.cu_ratio);
let a = app | summarize avgp = avg(p), maxp = max(p), over = countif(p > 100);
let bg24 = toscalar(FabricBackground24h(cap, now()) | summarize sum(pct_of_daily_budget));
let h = FabricHarvestHealth() | summarize errors = sum(errors), last_run = max(last_run);
union
    (a | project Metric = 'Average utilisation (%)', Value = round(avgp, 1), Ord = 1),
    (a | project Metric = 'Peak utilisation (%)', Value = round(maxp, 1), Ord = 2),
    (a | project Metric = 'Timepoints over 100%', Value = todouble(over), Ord = 3),
    (print Metric = 'Background, last 24 h (% of daily budget)', Value = round(todouble(bg24), 1), Ord = 4),
    (h | project Metric = 'Harvest errors (24 h)', Value = todouble(errors), Ord = 5),
    (h | project Metric = 'Minutes since last harvest', Value = todouble(datetime_diff('minute', now(), last_run)), Ord = 6)
| order by Ord asc
| project Metric, Value"""

Q["health"] = "FabricHarvestHealth()"

Q["explain_items"] = PRELUDE + """FabricExplainWindow(cap, w0, w1)
| project class, workspace_name, item_name, item_kind, operation,
          ['CU (s) in window'] = round(cu_in_window, 2), ['% of window budget'] = pct_of_window_budget,
          operations = ops"""

Q["explain_chart"] = PRELUDE + """FabricExplainWindow(cap, w0, w1)
| summarize cu = sum(cu_in_window) by item = strcat(item_name, ' (', class, ')')
| top 15 by cu desc"""

Q["explain_users"] = PRELUDE + """FabricOpsLatest()
| where capacity_id == cap and smoothing_start < w1 and smoothing_end_eff > w0
| extend tp_overlap = todouble((min_of(smoothing_end_eff, w1) - max_of(smoothing_start, w0)) / 30s)
| summarize ['CU (s) in window'] = round(sum(total_cu_s / n_tp * tp_overlap), 2), operations = count()
    by user_name, class
| order by ['CU (s) in window'] desc"""

Q["background_items"] = PRELUDE + """FabricBackground24h(cap, now())
| project workspace_name, item_name, item_kind, ['CU (s) last 24h'] = round(total_cu_s, 1),
          operations = ops, ['% of daily budget'] = pct_of_daily_budget"""

Q["background_chart"] = PRELUDE + """FabricBackground24h(cap, now())
| project item_name, ['% of daily budget'] = pct_of_daily_budget
| top 15 by ['% of daily budget'] desc"""

# Charts keep the ten biggest items and fold the rest into one series so they stay readable.
# Items are pivoted into columns: the workbook draws one series per numeric column.
Q["rootcause_chart"] = PRELUDE + """let top_items = FabricRootCauseByHour(cap, t0, t1)
    | summarize s = sum(total_cu_s) by item_name | top 10 by s desc | project item_name;
FabricRootCauseByHour(cap, t0, t1)
| extend item = iff(item_name in (top_items), item_name, 'other items')
| summarize cu = round(sum(total_cu_s), 1) by hour, item
| evaluate pivot(item, sum(cu), hour)
| order by hour asc"""

Q["rootcause_table"] = PRELUDE + """FabricRootCauseByHour(cap, t0, t1)
| project hour, class, workspace_name, item_name, item_kind, ['CU (s)'] = round(total_cu_s, 1), operations = ops
| order by ['CU (s)'] desc"""

Q["changes_table"] = PRELUDE + """FabricDailyDelta(cap)
| project item_name, item_kind, ['CU (s) yesterday'] = round(cu_s, 1),
          ['7-day median'] = round(median_cu_s, 1), ratio, is_new"""

# Always the last 14 days, whatever the time range: a daily chart over 24 h has only two bars.
Q["changes_trend"] = PRELUDE + """let in_range = FabricOpsLatest()
    | where capacity_id == cap
    | extend day = startofday(coalesce(end_time, start_time))
    | where day >= startofday(ago(13d));
let top_items = in_range | summarize s = sum(total_cu_s) by item_name | top 10 by s desc | project item_name;
in_range
| extend item = iff(item_name in (top_items), item_name, 'other items')
| summarize cu = round(sum(total_cu_s), 1) by day = format_datetime(day, 'yyyy-MM-dd'), item
| evaluate pivot(item, sum(cu), day)
| order by day asc"""

# The Overview shows the same top contributors as the Explain tab, for the explain window.
Q["overview_top"] = Q["explain_chart"]

Q["details_ops"] = PRELUDE + """FabricOpsLatest()
| where capacity_id == cap and start_time < w1 and coalesce(end_time, start_time) >= w0
| project start_time, end_time, class, item_name, item_kind, operation, user_name, status,
          ['Total CU (s)'] = round(total_cu_s, 2), ['Duration (s)'] = duration_s, operation_id
| order by ['Total CU (s)'] desc
| take 500"""

Q["details_spark"] = PRELUDE + """FabricSparkAttribution(cap, t0, t1)
| project start_time, end_time, item_name, operation, user_name, status,
          ['Total CU (s)'] = round(total_cu_s, 1), ['Linked to Livy session'] = matched,
          ['Running (s)'] = running_s, ['Queued (s)'] = queued_s, job_instance_id, operation_id"""

Q["details_warehouse"] = PRELUDE + """FabricWarehouseAttribution(cap, t0, t1)
| project item_name, login_name, ['Attributed CU (s)'] = round(attributed_cu_s, 2), ['SQL queries'] = sql_queries,
          sample_command"""

Q["validation"] = PRELUDE + """FabricReconstructionCheck(cap, w0, w1)
| project timepoint, ['App %'] = model_pct, ['Reconstructed %'] = recon_pct, ['Difference'] = diff_pct"""


# ---------------------------------------------------------------------------------------
# Workbook JSON
# ---------------------------------------------------------------------------------------

def _id():
    return str(uuid.uuid4())


def text(md, name):
    return {"type": 1, "content": {"json": md}, "name": name}


def bar(column, palette="blue", max_value=None):
    """Grid column drawn as a bar."""
    opts = {"palette": palette}
    if max_value is not None:
        opts["min"], opts["max"] = 0, max_value
    return {"columnMatch": column, "formatter": 3, "formatOptions": opts}


def heat(column, palette="orange"):
    return {"columnMatch": column, "formatter": 8, "formatOptions": {"palette": palette}}


def red_when_positive(column):
    """Grid column that turns red when above zero, green at zero."""
    return {"columnMatch": column, "formatter": 18, "formatOptions": {"thresholdsOptions": "colors",
            "thresholdsGrid": [{"operator": ">", "thresholdValue": "0", "representation": "redBright", "text": "{0}{1}"},
                               {"operator": "Default", "representation": "green", "text": "{0}{1}"}]}}


def grid(*formatters):
    return {"gridSettings": {"filter": True, "formatters": list(formatters)}}


def tiles():
    return {"tileSettings": {"showBorder": True,
            "titleContent": {"columnMatch": "Metric", "formatter": 1},
            "leftContent": {"columnMatch": "Value", "formatter": 12, "formatOptions": {"palette": "auto"},
                            "numberFormat": {"unit": 17, "options": {"maximumFractionDigits": 1}}}}}


# showMetrics off: the legend would otherwise print the sum of every percentage in the range.
UTILISATION_CHART = {"chartSettings": {"ymin": 0, "showMetrics": False, "seriesLabelSettings": [
    {"seriesName": "Background %", "color": "blue"},
    {"seriesName": "Interactive %", "color": "orange"},
    {"seriesName": "Reconstructed total %", "color": "gray"},
    {"seriesName": "App utilisation %", "color": "green"},
    {"seriesName": "Throttle limit %", "color": "redBright"}]}}


def query(key, title, viz="table", size=0, brush=False, extra=None):
    content = {
        "version": "KqlItem/1.0",
        "query": Q[key],
        "size": size,
        "title": title,
        "timeContext": {"durationMs": QUERY_SPAN_MS},
        "queryType": 0,
        "resourceType": LA_TYPE,
        "visualization": viz,
    }
    if brush:
        content["timeBrushParameterName"] = "Window"
        content["timeBrushExportOnlyWhenBrushed"] = True
    if viz == "table":
        content["gridSettings"] = {"filter": True}
        content["exportToExcelOptions"] = "visible"
    content.update(extra or {})
    return {"type": 3, "content": content, "name": key}


def group(tab, items):
    return {
        "type": 12,
        "content": {"version": "NotebookGroup/1.0", "groupType": "editable", "items": items},
        "conditionalVisibility": {"parameterName": "tab", "comparison": "isEqualTo", "value": tab},
        "name": f"group-{tab}",
    }


def build():
    day = 24 * 3600 * 1000
    parameters = {
        "type": 9,
        "content": {
            "version": "KqlParameterItem/1.0",
            "parameters": [
                {"id": _id(), "version": "KqlParameterItem/1.0", "name": "Capacity", "label": "Capacity",
                 "type": 2, "isRequired": True, "queryType": 0, "resourceType": LA_TYPE,
                 "query": "FabricCapacities_CL\n| summarize arg_max(TimeGenerated, capacity_name, sku) by value = tolower(capacity_id)\n"
                          "| project value, label = strcat(capacity_name, ' (', sku, ')')",
                 "value": "value::1", "typeSettings": {"showDefault": False},
                 "timeContext": {"durationMs": QUERY_SPAN_MS}},
                {"id": _id(), "version": "KqlParameterItem/1.0", "name": "TimeRange", "label": "Time range",
                 "type": 4, "isRequired": True, "value": {"durationMs": day},
                 "typeSettings": {"allowCustom": True, "selectableValues": [
                     {"durationMs": d * 3600 * 1000} for d in (1, 4, 12, 24, 48, 72, 168, 336)]}},
                {"id": _id(), "version": "KqlParameterItem/1.0", "name": "Window",
                 "label": "Explain window (or brush the chart)", "type": 4, "isRequired": True,
                 "value": {"durationMs": 3600 * 1000},
                 "typeSettings": {"allowCustom": True, "selectableValues": [
                     {"durationMs": m * 60 * 1000} for m in (5, 15, 30, 60, 240)]}},
                {"id": _id(), "version": "KqlParameterItem/1.0", "name": "tab", "type": 1,
                 "value": "overview", "isHiddenWhenLocked": True},
            ],
            "style": "pills",
            "queryType": 0,
            "resourceType": LA_TYPE,
        },
        "name": "parameters",
    }
    tabs = [("overview", "Overview"), ("explain", "Explain a window"), ("background", "24h background"),
            ("rootcause", "Root cause by run time"), ("changes", "Daily changes"), ("details", "Details")]
    tab_links = {
        "type": 11,
        "content": {"version": "LinkItem/1.0", "style": "tabs", "links": [
            {"id": _id(), "cellValue": "tab", "linkTarget": "parameter", "linkLabel": label,
             "subTarget": key, "style": "link"} for key, label in tabs]},
        "name": "tabs",
    }
    items = [
        text("## Fabric CU attribution\nRead from the Capacity Metrics model by the cuscope harvester "
             "(outside Fabric). CU figures are CU-seconds; percentages are of the capacity's base budget.",
             "title"),
        parameters,
        tab_links,
        group("overview", [
            query("kpis", "Summary for the time range", viz="tiles", size=4, extra=tiles()),
            query("utilisation", "Smoothed utilisation: background and interactive (reconstructed) vs the app. "
                  "The red line is the throttling threshold. Drag across the chart to set the explain window.",
                  viz="timechart", brush=True, extra=UTILISATION_CHART),
            query("overview_top", "Top contributors in the explain window", viz="barchart"),
            query("health", "Harvester health (last 24 h). Errors should be 0.",
                  extra=grid(red_when_positive("errors"))),
        ]),
        group("explain", [
            text("What filled the **Explain window**. Smoothed CU: background operations that finished up "
                 "to 24 h earlier are included, which is what throttling reacts to.", "explain-help"),
            query("explain_chart", "Top contributors in the window", viz="barchart"),
            query("explain_items", "By item and operation",
                  extra=grid(bar("% of window budget", "blue"))),
            query("explain_users", "By user"),
        ]),
        group("background", [
            text("Background operations completed in the last 24 h, as a share of the daily budget. "
                 "This total drives background rejection and surge protection.", "background-help"),
            query("background_chart", "Share of daily budget by item", viz="barchart"),
            query("background_items", "Detail", extra=grid(bar("% of daily budget", "blue"))),
        ]),
        group("rootcause", [
            text("CU by the hour each operation **actually ran**, not when it was smoothed in. "
                 "Use this to find the batch job behind a later spike. The ten biggest items are shown; "
                 "the rest are grouped as *other items*.", "rootcause-help"),
            query("rootcause_chart", "CU (s) by run hour, stacked by item", viz="barchart"),
            query("rootcause_table", "Detail", extra=grid(heat("CU (s)", "orange"))),
        ]),
        group("changes", [
            text("Items whose CU yesterday was well above their 7-day median, and items seen for the first time. "
                 "A **ratio** of 2 means the item used double its usual amount.", "changes-help"),
            query("changes_table", "Items that doubled yesterday vs their 7-day median, and new items",
                  size=4, extra={**grid(heat("ratio", "redBright")),
                                 "noDataMessage": "No item doubled yesterday, and no new items appeared."}),
            query("changes_trend", "Daily CU (s) by item, last 14 days", viz="barchart"),
        ]),
        group("details", [
            query("details_ops", "Operations in the explain window (top 500 by CU)",
                  extra=grid(heat("Total CU (s)", "orange"))),
            query("details_spark", "Spark operations linked to Livy sessions"),
            query("details_warehouse", "Warehouse CU split across SQL queries",
                  extra=grid(heat("Attributed CU (s)", "orange"))),
            query("validation", "Validation: reconstructed vs app utilisation in the explain window",
                  viz="timechart", extra={"noDataMessage":
                      "No app timepoints in this window yet. They are harvested hourly and lag 1-2 hours: "
                      "pick an explain window that ended at least 2 hours ago."}),
        ]),
    ]
    return {
        "version": "Notebook/1.0",
        "items": items,
        "fallbackResourceIds": ["__WORKSPACE_RESOURCE_ID__"],
        "$schema": "https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json",
    }


# ---------------------------------------------------------------------------------------
# Test: run each query with concrete parameter values
# ---------------------------------------------------------------------------------------

def test(workspace_customer_id, capacity_id):
    values = {
        "{Capacity}": capacity_id,
        "{TimeRange:start}": "ago(24h)",
        "{TimeRange:end}": "now()",
        "{Window:start}": "ago(3h)",
        "{Window:end}": "ago(2h)",
    }
    az = shutil.which("az") or shutil.which("az.cmd")
    if not az:
        sys.exit("Azure CLI not found on PATH. Install it and run `az login` first.")
    failed = 0
    for key, kql in Q.items():
        for k, v in values.items():
            kql = kql.replace(k, v)
        body = HERE / ".test_body.json"
        body.write_text(json.dumps({"query": kql}))
        proc = subprocess.run(
            [az, "rest", "--method", "post", "--url",
             f"https://api.loganalytics.io/v1/workspaces/{workspace_customer_id}/query",
             "--resource", "https://api.loganalytics.io", "--body", f"@{body}", "-o", "json"],
            capture_output=True, text=True)
        body.unlink(missing_ok=True)
        try:
            result = json.loads(proc.stdout)
            if "error" in result:
                # Partial failures (memory limits) still return a table; the portal shows a warning.
                failed += 1
                print(f"PART {key:20s} {json.dumps(result['error'])[:600]}")
                continue
            table = result["tables"][0]
            cols = [c["name"] for c in table["columns"]]
            print(f"OK   {key:20s} {len(table['rows']):6d} rows  cols={cols[:6]}")
            if table["rows"]:
                print(f"     first row: {[str(v)[:28] for v in table['rows'][0][:7]]}")
        except Exception:
            failed += 1
            print(f"FAIL {key:20s} {(proc.stderr or proc.stdout)[:600]}")
    return failed


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(1 if test(sys.argv[2], sys.argv[3]) else 0)
    OUT.write_text(json.dumps(build(), indent=2), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(Q)} queries)")
