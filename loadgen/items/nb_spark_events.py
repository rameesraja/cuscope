# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "{{LAKEHOUSE_ID}}",
# META       "default_lakehouse_name": "lh_loadgen",
# META       "default_lakehouse_workspace_id": "{{WORKSPACE_ID}}"
# META     }
# META   }
# META }

# MARKDOWN ********************

# # nb_spark_events
# Load generator for the cuscope pilot. Appends synthetic rows to `synthetic_events` and trims
# the table to 3 days. Scheduled every 3 hours by `loadgen/deploy_loadgen.py`; each run is one
# Spark background operation in the Capacity Metrics app. Volume follows the business-day mood
# in harvester/cuscope/loadgen.py (shared day regime, 2-hour storm blocks, exponential run size):
# 0.1M-8M rows, a heavy rollup on big runs, about one run in ten does nothing and about one in
# thirty fails, as real jobs do.

# CELL ********************

import random
from datetime import datetime, timedelta, timezone

from pyspark.sql import functions as F

local = datetime.now(timezone.utc) - timedelta(hours=6)
doy = local.timetuple().tm_yday
bucket = (doy * doy * 31 + doy * 17) % 101 % 10
day = 0.3 if local.weekday() >= 5 else 0.4 if bucket < 2 else 1.0 if bucket < 6 else 1.8 if bucket < 9 else 2.6
hour = 1.5 if 8 <= local.hour < 18 else 0.8 if 18 <= local.hour < 22 else 0.4
block = doy * 12 + local.hour // 2
storm = 4.0 if (block * block * 31 + block * 17 + 5) % 101 % 10 == 9 else 1.0
mood = min(8.0, day * hour * storm) * random.expovariate(1.0)  # exponential run size: mostly small, sometimes huge
ROWS = int(min(8_000_000, max(100_000, 1_000_000 * mood)))
print(f"mood {mood:.2f} (day {day}, hour {hour}, storm {storm}), rows {ROWS}")
if random.random() < 0.1:
    notebookutils.notebook.exit("skipped: nothing new upstream")
regions = F.array(*[F.lit(r) for r in ["north", "south", "east", "west"]])

df = (
    spark.range(ROWS)
    .withColumnRenamed("id", "event_id")
    .withColumn("event_time", F.expr("current_timestamp() - make_interval(0, 0, 0, 0, 0, 0, event_id % 3600)"))
    .withColumn("category", F.concat(F.lit("cat_"), (F.col("event_id") % 20).cast("string")))
    .withColumn("region", F.element_at(regions, (F.col("event_id") % 4 + 1).cast("int")))
    .withColumn("amount", F.round(F.rand() * 1000, 2))
    .withColumn("qty", (F.rand() * 10).cast("int"))
)
df.write.mode("append").format("delta").saveAsTable("synthetic_events")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

if mood > 1.5:
    # Busy runs also rebuild a rollup: a shuffle-heavy aggregation over the whole table.
    (spark.table("synthetic_events")
     .groupBy("category", "region", F.window("event_time", "5 minutes"))
     .agg(F.count("*").alias("events"), F.sum("amount").alias("amount"),
          F.expr("percentile_approx(amount, 0.95)").alias("p95_amount"))
     .write.mode("overwrite").format("delta").saveAsTable("synthetic_rollup"))

if random.random() < 0.03:
    raise RuntimeError("simulated failure: upstream file was malformed")

spark.sql("DELETE FROM synthetic_events WHERE event_time < current_timestamp() - INTERVAL 3 DAYS")
print(spark.table("synthetic_events").count())

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
