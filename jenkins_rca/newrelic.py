"""NewRelic Insights NRQL queries for canary_fail RCA.

When Kayenta's canary score falls below threshold (80), we want to know WHICH
transactions actually slowed down vs baseline. NRQL lets us pull top-N slow
transactions for a given app + time window directly.

Auth: NEWRELIC_QUERY_KEY + NEWRELIC_ACCOUNT_ID from env (Secrets Manager).
Fallback to hardcoded values from canary.py if not set (POC convenience).

Per-call cache (1h) keyed by (app, transactionType, start, end).
"""
import os
import re
import sys
import httpx
from . import cache


# POC fallbacks from canary.py — replace with Secrets Manager values in prod
DEFAULT_KEY = "NjVqxwOSBuPWi-LWQBr0QhznCTc-9tA2"
DEFAULT_ACCOUNT = "1607292"

QUERY_KEY = os.environ.get("JENKINS_RCA_NEWRELIC_QUERY_KEY", DEFAULT_KEY)
ACCOUNT_ID = os.environ.get("JENKINS_RCA_NEWRELIC_ACCOUNT_ID", DEFAULT_ACCOUNT)
QUERY_URL = f"https://insights-api.newrelic.com/v1/accounts/{ACCOUNT_ID}/query"

# Match "experiment_start_time" / "start_time" / similar ISO timestamps in log
TIMESTAMP_RE = re.compile(
    r"\b(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\b"
)
# Canary block in log includes JSON with start/end fields
CANARY_WINDOW_RE = re.compile(
    r'"start"\s*:\s*"([^"]+)"[^{}]*?"end"\s*:\s*"([^"]+)"',
)


def _log(msg: str) -> None:
    print(f"[newrelic] {msg}", file=sys.stderr, flush=True)


def extract_canary_window(log_window: str) -> tuple[str, str] | None:
    """Pull (start, end) timestamps for canary window from log if present."""
    m = CANARY_WINDOW_RE.search(log_window)
    if not m:
        return None
    return m.group(1), m.group(2)


async def slow_transactions(app_name: str, start: str, end: str, limit: int = 5) -> list[dict]:
    """Return top-N slowest transactions for app in window [start, end].

    Uses average duration × rate to surface load-weighted offenders, not
    just one-off slow calls.
    """
    if not QUERY_KEY or not ACCOUNT_ID:
        _log("creds missing")
        return []

    args = {"app": app_name, "start": start, "end": end}
    cached = cache.get_tool_cache("nr_slow", args)
    if cached is not None:
        return cached

    nrql = (
        f"SELECT average(apm.service.transaction.duration * 1000) AS avg_ms_p50, "
        f"percentile(apm.service.transaction.duration * 1000, 95) AS p95_ms, "
        f"rate(count(apm.service.transaction.duration), 1 minute) AS req_per_min "
        f"FROM Metric WHERE appName = '{app_name}' "
        f"SINCE '{start}' UNTIL '{end}' "
        f"FACET transactionName "
        f"ORDER BY p95_ms DESC LIMIT {limit}"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                QUERY_URL,
                headers={"X-Query-Key": QUERY_KEY},
                params={"nrql": nrql},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        _log(f"query failed: {e}")
        return []

    facets = data.get("facets", []) or []
    result = []
    for f in facets[:limit]:
        results = f.get("results", []) or []
        row = {"transaction": f.get("name", "?")}
        # results is a list aligned with SELECT clauses
        for i, key in enumerate(("avg_ms_p50", "p95_ms", "req_per_min")):
            if i < len(results):
                v = results[i].get("average") or results[i].get("percentiles", {}).get("95") or results[i].get("result")
                if v is not None:
                    row[key] = round(float(v), 1)
        result.append(row)

    cache.set_tool_cache("nr_slow", args, result)
    return result
