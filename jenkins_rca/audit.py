"""Persist every RCA call to disk for debugging + future RAG corpus.

Each call writes one JSON file at /var/log/jenkins-rca/<request_id>.json with:
- request_id, timestamp, job, build, service
- error_class
- token usage + cost
- prompt (clean log window only, not full prompt — re-derivable)
- response (LLM JSON output)
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(os.environ.get("JENKINS_RCA_AUDIT_DIR", "/var/log/jenkins-rca"))


_REQUEST_ID_RE = __import__("re").compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def record(payload: dict) -> Path | None:
    """Write one RCA record. Returns path or None on failure."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        request_id = payload.get("request_id", "unknown")
        path = LOG_DIR / f"{request_id}.json"
        payload = {"recorded_at": datetime.now(timezone.utc).isoformat(), **payload}
        path.write_text(json.dumps(payload, indent=2, default=str))
        return path
    except Exception as e:
        # Audit must never fail the RCA call
        print(f"[audit] failed to record: {e}")
        return None


def list_recent(days: int = 2) -> list[dict]:
    """Return audit records from the last N days, newest first.

    Used by the /dashboard view to surface every recent RCA grouped by
    pipeline (job). Reads each file under LOG_DIR, filters by
    `recorded_at >= now - days`, returns trimmed records (no full
    `prompt` / `response` bodies — caller doesn't need them).

    Filesystem cost: ~50 RCAs/day × 2 days = ~100 files, ~5 KB each.
    Fast enough to scan per request. Cache in-memory later if it grows.
    """
    from datetime import timedelta
    if not LOG_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    for path in LOG_DIR.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except Exception:
            continue
        # Filter by recorded_at if present, else file mtime
        ts_str = rec.get("recorded_at")
        try:
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc)
        except Exception:
            ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if ts < cutoff:
            continue
        rca = rec.get("rca") or {}
        # Legacy audit records may have URL-encoded job names (e.g.
        # "Stagger%20Prod%20Plus%20One") from earlier curl-driven tests.
        # Decode here so dashboard grouping merges them with newer records
        # that store the decoded form.
        raw_job = rec.get("job") or rca.get("job") or "(unknown)"
        try:
            from urllib.parse import unquote as _unq
            raw_job = _unq(raw_job)
        except Exception:
            pass
        out.append({
            "request_id": rec.get("request_id"),
            "job": raw_job,
            "build": rec.get("build") or rca.get("build"),
            "service": rec.get("service") or rca.get("service") or "",
            "error_class": rca.get("error_class") or rec.get("error_class") or "unknown",
            "failed_stage": rca.get("failed_stage") or "",
            "summary": rca.get("summary") or "",
            "recorded_at": ts.isoformat(),
            "_ts": ts,
            "cost_usd": rca.get("cost_usd"),
            "model_used": rca.get("model_used"),
            "needs_deeper": rca.get("needs_deeper", False),
            "llm_error": rca.get("_llm_error", False),
        })
    out.sort(key=lambda r: r["_ts"], reverse=True)
    # Dedup by (job, build) — when an operator re-triggers the same build
    # with `deep:true` multiple times, each call writes a fresh audit
    # record. The dashboard should show only the latest per build.
    # `out` is already sorted newest-first, so the first occurrence per
    # (job, build) is the keeper.
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for r in out:
        key = (r["job"], r.get("build"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    # Strip internal _ts before returning
    for r in deduped:
        r.pop("_ts", None)
    return deduped


def read_by_request_id(request_id: str) -> dict | None:
    """Load an audit record by request_id. Returns the parsed dict or None.

    Validates the request_id matches the canonical uuid pattern before touching
    disk — defense-in-depth against path traversal since the value may come
    from an HTTP path param.
    """
    if not _REQUEST_ID_RE.match(request_id or ""):
        return None
    path = LOG_DIR / f"{request_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"[audit] failed to read {request_id}: {e}")
        return None
