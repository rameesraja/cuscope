"""HTTP clients for Power BI executeQueries and the Fabric REST API, with retry on 429/5xx.

Identity comes from DefaultAzureCredential: the Function App's managed identity in Azure,
or AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET when run locally.
"""

import logging
import time
from functools import lru_cache

import requests
from azure.identity import DefaultAzureCredential

PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
SQL_SCOPE = "https://database.windows.net/.default"
PBI_API = "https://api.powerbi.com/v1.0/myorg"
FABRIC_API = "https://api.fabric.microsoft.com/v1"

log = logging.getLogger(__name__)


class QueryError(Exception):
    pass


@lru_cache(maxsize=1)
def credential():
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def token(scope):
    return credential().get_token(scope).token


def request(method, url, scope, json_body=None, params=None, max_attempts=6):
    for attempt in range(1, max_attempts + 1):
        resp = requests.request(
            method, url, json=json_body, params=params, timeout=180,
            headers={"Authorization": f"Bearer {token(scope)}"},
        )
        if resp.status_code != 429 and resp.status_code < 500:
            return resp
        wait = float(resp.headers.get("Retry-After", 2 ** attempt))
        log.warning("HTTP %s from %s, retry %d in %.0fs", resp.status_code, url, attempt, wait)
        time.sleep(min(wait, 60))
    return resp


class SemanticModel:
    """One semantic model reached through executeQueries."""

    def __init__(self, workspace_id, dataset_id):
        self.url = f"{PBI_API}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"

    def query(self, dax):
        """Return (rows, raw_payload). Raises QueryError; errors can arrive with HTTP 200."""
        body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
        resp = request("POST", self.url, PBI_SCOPE, body)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        if resp.status_code != 200:
            raise QueryError(f"HTTP {resp.status_code}: {str(payload)[:1000]}")
        if "error" in payload:
            raise QueryError(str(payload["error"])[:1000])
        result = payload["results"][0]
        if "error" in result:
            raise QueryError(str(result["error"])[:1000])
        table = (result.get("tables") or [{}])[0]
        if "error" in table:
            raise QueryError(str(table["error"])[:1000])
        return table.get("rows", []), payload


def fabric_get_all(path, params=None):
    """GET a paginated Fabric list endpoint and return all items in 'value'."""
    url = f"{FABRIC_API}/{path.lstrip('/')}"
    items = []
    while url:
        resp = request("GET", url, FABRIC_SCOPE, params=params)
        resp.raise_for_status()
        body = resp.json()
        items.extend(body.get("value", []))
        url, params = body.get("continuationUri"), None
    return items


def fabric_get(path):
    resp = request("GET", f"{FABRIC_API}/{path.lstrip('/')}", FABRIC_SCOPE)
    resp.raise_for_status()
    return resp.json()
