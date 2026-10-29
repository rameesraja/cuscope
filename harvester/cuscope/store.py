"""Raw response store, checkpoint state and the Log Analytics sink.

Each has an Azure implementation (Blob / Logs Ingestion API) and a local-folder fallback,
chosen by whether the corresponding setting is present. The fallback lets the harvester
run from a laptop before any Azure resources exist.
"""

import json
import logging
from functools import lru_cache

from .clients import credential

log = logging.getLogger(__name__)


class _Blob:
    def __init__(self, container_url):
        from azure.storage.blob import ContainerClient
        self.container = ContainerClient.from_container_url(container_url, credential=credential())

    def write(self, path, text):
        self.container.upload_blob(path, text.encode(), overwrite=True)

    def read(self, path):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return self.container.download_blob(path).readall().decode()
        except ResourceNotFoundError:
            return None


class _Local:
    def __init__(self, root):
        self.root = root

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def read(self, path):
        target = self.root / path
        return target.read_text(encoding="utf-8") if target.exists() else None


@lru_cache(maxsize=None)
def _backend(url, local_root):
    return _Blob(url) if url else _Local(local_root)


def raw_store(settings):
    return _backend(settings.raw_storage_url, settings.local_output_dir / "raw")


def state_store(settings):
    return _backend(settings.state_storage_url, settings.local_output_dir / "state")


def read_state(settings, key, default=None):
    text = state_store(settings).read(f"{key}.json")
    return json.loads(text) if text else default


def write_state(settings, key, value):
    state_store(settings).write(f"{key}.json", json.dumps(value, default=str))


@lru_cache(maxsize=None)
def _ingestion_client(endpoint):
    from azure.monitor.ingestion import LogsIngestionClient
    return LogsIngestionClient(endpoint=endpoint, credential=credential())


def emit(settings, table, records):
    """Send records to the custom table `table` (e.g. 'FabricOps_CL')."""
    if not records:
        return
    if settings.dcr_endpoint:
        # The client splits payloads above the 1 MB API limit.
        _ingestion_client(settings.dcr_endpoint).upload(
            rule_id=settings.dcr_immutable_id, stream_name=f"Custom-{table}", logs=records)
        return
    target = settings.local_output_dir / "tables" / f"{table}.ndjson"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
