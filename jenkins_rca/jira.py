"""Jira ticket fetch + extraction.

Detects Jira ticket IDs in text via regex (e.g. FMSCAT-5887), fetches summary
+ status + key fields via Jira REST API. Result fed into LLM tool context.

Auth uses email + API token (Atlassian Cloud). Set via secrets:
- jira_url       e.g. https://blackbuck.atlassian.net
- jira_user      operator email
- jira_api_token Atlassian API token

Cached for TOOL_CACHE_TTL (24h) — ticket details don't change every minute.
"""
import os
import re
import base64
import httpx
from . import cache


TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
MAX_TICKETS = 3

JIRA_URL = os.environ.get("JENKINS_RCA_JIRA_URL", "")
JIRA_USER = os.environ.get("JENKINS_RCA_JIRA_USER", "")
JIRA_TOKEN = os.environ.get("JENKINS_RCA_JIRA_API_TOKEN", "")


def extract_tickets(text: str) -> list[str]:
    """Return unique ticket keys in encounter order, capped at MAX_TICKETS."""
    seen = []
    for m in TICKET_RE.finditer(text or ""):
        k = m.group(1)
        if k not in seen:
            seen.append(k)
        if len(seen) >= MAX_TICKETS:
            break
    return seen


def _auth_header() -> dict:
    creds = f"{JIRA_USER}:{JIRA_TOKEN}".encode()
    return {"Authorization": "Basic " + base64.b64encode(creds).decode(), "Accept": "application/json"}


# Known org-specific custom field IDs (from JiraDetailsCompliance.md Lessons).
# Always surfaced regardless of name match.
KNOWN_CUSTOM_FIELDS = {
    "customfield_10973": "Signed Off Commit ID",
}

# Substring matchers for surfacing org-specific custom fields by human name.
# Jira custom fields are returned as customfield_NNNNN; we get human names via
# expand=names. These substrings match against the human name (lower-cased).
_INTERESTING_FIELD_KEYWORDS = (
    "signed off commit", "signoff commit", "sign-off commit",
    "commit id", "commit hash", "release tag", "tag",
    "deployment", "deploy id",
    "approver", "approved by",
    "build number",
)

_SHA_LIKE_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _flatten_field_value(v):
    """Reduce arbitrary Jira field value to a printable scalar."""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        # common Jira value containers
        for k in ("value", "name", "displayName", "key"):
            if k in v:
                return v[k]
        return str(v)[:200]
    if isinstance(v, list):
        return [_flatten_field_value(x) for x in v]
    return str(v)[:200]


async def fetch_ticket(key: str) -> dict:
    """Fetch ticket with all fields. Surfaces custom fields whose human name
    contains commit/tag/approval keywords. Returns slim dict; on error returns
    {error}."""
    if not (JIRA_URL and JIRA_USER and JIRA_TOKEN):
        return {"error": "jira creds not configured"}

    cached = cache.get_tool_cache("jira_ticket", {"key": key})
    if cached is not None:
        return cached

    url = f"{JIRA_URL.rstrip('/')}/rest/api/2/issue/{key}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=_auth_header(), params={
                "fields": "*all",
                "expand": "names",
            })
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        result = {"error": f"jira fetch failed: {e}", "key": key}
        return result

    f = data.get("fields", {}) or {}
    names = data.get("names", {}) or {}  # customfield_id -> human name

    # Standard fields
    result = {
        "key": data.get("key", key),
        "summary": f.get("summary", ""),
        "status": (f.get("status") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "assignee": (f.get("assignee") or {}).get("displayName"),
        "reporter": (f.get("reporter") or {}).get("displayName"),
        "labels": f.get("labels", []),
        "components": [c.get("name") for c in (f.get("components") or [])],
        "fix_versions": [v.get("name") for v in (f.get("fixVersions") or [])],
        "resolution": (f.get("resolution") or {}).get("name") if f.get("resolution") else None,
        "description": (f.get("description") or "")[:800],
    }

    # Surface custom fields. Priority:
    # 1. Known org-specific field IDs (from runbook): always included with
    #    canonical name.
    # 2. Fields whose human name matches commit/tag/approval keywords.
    # 3. Fields whose VALUE looks like a SHA — catches mis-named fields.
    custom = {}
    sha_fields = {}
    for fid, fval in f.items():
        if not fid.startswith("customfield_"):
            continue
        if fval in (None, "", [], {}):
            continue
        flat = _flatten_field_value(fval)

        # 1. Known field — use canonical name
        if fid in KNOWN_CUSTOM_FIELDS:
            custom[KNOWN_CUSTOM_FIELDS[fid]] = flat
            continue

        human = (names.get(fid) or "").lower()
        # 2. Name keyword match
        if any(kw in human for kw in _INTERESTING_FIELD_KEYWORDS):
            custom[names.get(fid, fid)] = flat
            continue
        # 3. Value looks like a SHA
        flat_str = str(flat)
        if _SHA_LIKE_RE.search(flat_str):
            sha_fields[names.get(fid, fid)] = flat

    if custom:
        result["custom_fields"] = custom
    if sha_fields:
        result["sha_like_fields"] = sha_fields

    cache.set_tool_cache("jira_ticket", {"key": key}, result)
    return result


async def fetch_all(keys: list[str]) -> list[dict]:
    """Fetch multiple tickets sequentially (small N, no need for parallel)."""
    return [await fetch_ticket(k) for k in keys]


async def search(jql: str, max: int = 10) -> list[dict]:
    """JQL search. Returns ticket summaries (no full fields — call
    fetch_ticket per key if you need detail).

    Used by the agent-mode RCA tool `jira_search`. JQL example:
    'issuekey in (X, Y, Z)' to fetch clone-chain summaries in one call.
    """
    if not (JIRA_URL and JIRA_USER and JIRA_TOKEN):
        return [{"error": "jira creds not configured"}]
    cache_key = {"jql": jql, "max": max}
    cached = cache.get_tool_cache("jira_search", cache_key)
    if cached is not None:
        return cached

    url = f"{JIRA_URL.rstrip('/')}/rest/api/2/search"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                url,
                headers=_auth_header(),
                params={
                    "jql": jql,
                    "maxResults": max,
                    "fields": "summary,status,assignee",
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return [{"error": f"jira search failed: {e}"}]

    out = []
    for it in data.get("issues", [])[:max]:
        f = it.get("fields") or {}
        out.append({
            "key": it.get("key"),
            "summary": f.get("summary"),
            "status": (f.get("status") or {}).get("name"),
            "assignee": (f.get("assignee") or {}).get("displayName"),
        })
    cache.set_tool_cache("jira_search", cache_key, out)
    return out
