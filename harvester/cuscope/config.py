"""Settings from environment variables (Function App settings, or .env when run locally)."""

import os
from dataclasses import dataclass
from pathlib import Path


def _list(name):
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _int(name, default):
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    capacity_ids: list
    metrics_workspace_id: str
    metrics_dataset_id: str
    # The metrics app reports times in the UTC_offset chosen at install; the harvester
    # converts to UTC on ingest. Validate the assumption with research/scripts/sp_smoke_test.py.
    utc_offset_hours: float
    lag_minutes: int
    interactive_step_s: int
    background_step_s: int
    max_catchup_samples: int
    split_rows: int
    max_concurrency: int
    enrich_workspace_ids: list
    raw_storage_url: str
    state_storage_url: str
    local_output_dir: Path
    dcr_endpoint: str
    dcr_immutable_id: str
    app_version: str
    loadgen_workspace_id: str
    loadgen_dataset_id: str


def load():
    return Settings(
        capacity_ids=_list("CAPACITY_IDS"),
        metrics_workspace_id=os.environ.get("METRICS_WORKSPACE_ID", ""),
        metrics_dataset_id=os.environ.get("METRICS_DATASET_ID", ""),
        utc_offset_hours=float(os.environ.get("METRICS_UTC_OFFSET", "0")),
        lag_minutes=_int("HARVEST_LAG_MINUTES", 20),
        interactive_step_s=_int("INTERACTIVE_STEP_SECONDS", 150),
        background_step_s=_int("BACKGROUND_STEP_SECONDS", 1800),
        max_catchup_samples=_int("MAX_CATCHUP_SAMPLES", 24),
        split_rows=_int("SPLIT_ROWS", 60000),
        max_concurrency=_int("MAX_CONCURRENCY", 4),
        enrich_workspace_ids=_list("ENRICH_WORKSPACE_IDS"),
        raw_storage_url=os.environ.get("RAW_STORAGE_URL", ""),
        state_storage_url=os.environ.get("STATE_STORAGE_URL", ""),
        local_output_dir=Path(os.environ.get("LOCAL_OUTPUT_DIR", "local_output")),
        dcr_endpoint=os.environ.get("DCR_ENDPOINT", ""),
        dcr_immutable_id=os.environ.get("DCR_IMMUTABLE_ID", ""),
        app_version=os.environ.get("METRICS_APP_VERSION", "unknown"),
        loadgen_workspace_id=os.environ.get("LOADGEN_WORKSPACE_ID", ""),
        loadgen_dataset_id=os.environ.get("LOADGEN_DATASET_ID", ""),
    )
