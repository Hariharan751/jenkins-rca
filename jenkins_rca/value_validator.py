"""Post-RCA value validator.

gpt-4.1 keeps defaulting to training-data values (port 8080,
/admin/version, /var/log/blackbuck/gps.log) in suggested_commands and
root_cause prose, even when the system prompt + runbook explicitly ban
them. Three rounds of prompt tightening didn't fix it.

This validator runs server-side after the agent emits final JSON.
Walks every string field that could contain hallucinated values and:
  1. Detects forbidden defaults via regex
  2. Substitutes the real value from service.lookup (or replaces with
     a discovery command if the real value is unknown)
  3. Records every correction in result["validator_notes"][] for the
     audit log + dashboard

No LLM involvement — pure mechanical Python. ~$0 cost per RCA. Always
runs (no env flag) because the hallucination is consistent.
"""
from __future__ import annotations

import re
from typing import Any


# Forbidden default + (replacement-from-service-lookup-key, fallback-discovery-command)
_PORT_8080_RE = re.compile(r"(?<![\d])8080(?![\d])")
_ADMIN_VERSION_RE = re.compile(r"/admin/version")
_GPS_LOG_RE = re.compile(r"/var/log/blackbuck/gps\.log")


def _pick_port(service_lookup: dict) -> int | None:
    """Real port from service.lookup. Tries target_port → port → app_port."""
    for k in ("target_port", "port", "service_port", "app_port", "container_port"):
        v = service_lookup.get(k)
        if v is not None and str(v).isdigit() and int(v) > 0:
            return int(v)
    return None


def _pick_log_path(service_lookup: dict, service: str) -> str | None:
    """Real log path. filebeat_log_path wins; else log_path; else derive
    from service name. filebeat_log_path may have a glob (.log*) — strip
    trailing * for non-glob commands."""
    for k in ("filebeat_log_path", "log_path"):
        v = service_lookup.get(k)
        if v and isinstance(v, str) and "NOT_IN_CONFIG" not in v:
            return v.rstrip("*")
    # Last resort: construct from service name
    if service:
        return f"/var/log/blackbuck/{service}.log"
    return None


def _pick_health_check_path(service_lookup: dict) -> str | None:
    """Real health_check_path from service.lookup, if set."""
    v = service_lookup.get("health_check_path")
    if v and isinstance(v, str) and "NOT_IN_CONFIG" not in v:
        return v
    return None


def _fix_string(text: str, *, real_port: int | None, real_log: str | None,
                real_hc_path: str | None, service: str) -> tuple[str, list[str]]:
    """Apply substitutions to one string field. Returns (new_text, notes)."""
    notes: list[str] = []
    new = text

    # Port 8080
    if _PORT_8080_RE.search(new):
        if real_port and real_port != 8080:
            new = _PORT_8080_RE.sub(str(real_port), new)
            notes.append(
                f"port 8080 → {real_port} (from service.lookup.target_port)"
            )
        elif real_port is None:
            notes.append(
                f"port 8080 cited but service.lookup has no target_port — "
                "verify via `bbctl run <id> -- 'sudo ss -tlnp'`"
            )

    # /admin/version
    if _ADMIN_VERSION_RE.search(new):
        if real_hc_path and real_hc_path != "/admin/version":
            new = _ADMIN_VERSION_RE.sub(real_hc_path, new)
            notes.append(
                f"/admin/version → {real_hc_path} "
                "(from service.lookup.health_check_path)"
            )
        else:
            # Don't know real path; replace with discovery hint inline
            new = _ADMIN_VERSION_RE.sub(
                "<health_check_path — discover via `aws elbv2 describe-target-groups`>",
                new,
            )
            notes.append(
                "/admin/version cited but service.lookup has no "
                "health_check_path — replaced with discovery hint"
            )

    # gps.log when service isn't gps
    if _GPS_LOG_RE.search(new) and service and "gps" not in service.lower():
        if real_log and real_log != "/var/log/blackbuck/gps.log":
            new = _GPS_LOG_RE.sub(real_log, new)
            notes.append(
                f"/var/log/blackbuck/gps.log → {real_log} "
                f"(service is {service}, not gps)"
            )

    return new, notes


def validate_and_fix(result: dict, service: str, service_lookup: dict) -> dict:
    """In-place fix every string field in `result` that contains a
    forbidden hallucinated default. Appends validator_notes[].

    Idempotent — calling twice on the same result is a no-op after the
    first pass replaces all matches.
    """
    if not isinstance(result, dict):
        return result

    real_port = _pick_port(service_lookup or {})
    real_log = _pick_log_path(service_lookup or {}, service)
    real_hc_path = _pick_health_check_path(service_lookup or {})

    notes: list[str] = []

    # Walk specific known string fields. Don't blanket-walk because
    # we don't want to touch jenkins_log evidence snippets (those are
    # verbatim quotes from the actual log, not LLM-generated).
    def _fix(text: str) -> str:
        nonlocal notes
        if not isinstance(text, str):
            return text
        new, n = _fix_string(
            text,
            real_port=real_port,
            real_log=real_log,
            real_hc_path=real_hc_path,
            service=service,
        )
        notes.extend(n)
        return new

    # root_cause prose
    if "root_cause" in result:
        result["root_cause"] = _fix(result["root_cause"])

    # suggested_fix dict (Finding/Action/Verify) or string
    sf = result.get("suggested_fix")
    if isinstance(sf, dict):
        for k in list(sf.keys()):
            if isinstance(sf[k], str):
                sf[k] = _fix(sf[k])
    elif isinstance(sf, str):
        result["suggested_fix"] = _fix(sf)

    # suggested_commands[].cmd and .rationale
    for cmd in result.get("suggested_commands") or []:
        if isinstance(cmd, dict):
            if isinstance(cmd.get("cmd"), str):
                cmd["cmd"] = _fix(cmd["cmd"])
            if isinstance(cmd.get("rationale"), str):
                cmd["rationale"] = _fix(cmd["rationale"])

    # Dedup notes (same correction often hits multiple fields)
    if notes:
        seen = set()
        deduped = []
        for n in notes:
            if n not in seen:
                seen.add(n)
                deduped.append(n)
        existing = result.get("validator_notes") or []
        if not isinstance(existing, list):
            existing = []
        result["validator_notes"] = existing + deduped

    return result


# ---------------------------------------------------------------------------
# Hallucination detector — flags fabricated claims that don't appear in log
# ---------------------------------------------------------------------------

# Strings that the LLM commonly invents WITHOUT having seen them in the log.
# Each tuple is (regex_to_find_in_rca_text, regex_to_find_supporting_evidence_in_log).
# If RCA mentions the first but log does NOT contain the second → flag it.
import re as _re

_HALLUCINATION_CHECKS: list[tuple[_re.Pattern, _re.Pattern, str]] = [
    # "HealthCheckPath=NOT_IN_CONFIG" or "HealthCheckPath=<anything>"
    # — only valid if the literal "HealthCheckPath" appears in the log too.
    (_re.compile(r"HealthCheckPath\s*=", _re.IGNORECASE),
     _re.compile(r"HealthCheckPath", _re.IGNORECASE),
     "claim mentions HealthCheckPath but log contains no such field"),
    # "Target.Port=8080" — must have evidence of "Target.Port" or actual port
    # mention in log (port number alone is too weak — too many false positives).
    (_re.compile(r"Target\.Port\s*=\s*\d+", _re.IGNORECASE),
     _re.compile(r"Target\.?Port", _re.IGNORECASE),
     "claim mentions Target.Port=N but log contains no Target.Port field"),
    # "NOT_IN_CONFIG" sentinel — pure hallucination string we've seen
    (_re.compile(r"NOT_IN_CONFIG"),
     _re.compile(r"NOT_IN_CONFIG"),
     "claim contains NOT_IN_CONFIG sentinel without log support"),
    # "state=unhealthy" without unhealthy keyword in log
    (_re.compile(r"state\s*=\s*unhealthy", _re.IGNORECASE),
     _re.compile(r"unhealthy", _re.IGNORECASE),
     "claim mentions state=unhealthy but log contains no 'unhealthy' keyword"),
]


def detect_hallucinations(result: dict, log_window: str) -> list[str]:
    """Return a list of suspect claims in result that lack log support.

    Empty list means clean. Use as a safety gate after `validate_and_fix`.
    """
    if not isinstance(result, dict):
        return []
    text = " ".join(filter(None, [
        result.get("summary") or "",
        result.get("root_cause") or "",
        result.get("suggested_fix") if isinstance(result.get("suggested_fix"), str) else "",
    ]))
    # also include suggested_fix dict fields
    sf = result.get("suggested_fix")
    if isinstance(sf, dict):
        text += " " + " ".join(str(v) for v in sf.values() if isinstance(v, str))

    violations: list[str] = []
    for rca_pat, log_pat, msg in _HALLUCINATION_CHECKS:
        if rca_pat.search(text) and not log_pat.search(log_window or ""):
            violations.append(msg)
    return violations


def flag_hallucinations(result: dict, log_window: str) -> dict:
    """Run detect_hallucinations and stash findings on result['hallucinations'].

    Non-destructive — does NOT rewrite root_cause/suggested_fix. Caller decides
    whether to re-prompt LLM or just surface the warnings to operators.
    """
    if not isinstance(result, dict):
        return result
    hits = detect_hallucinations(result, log_window or "")
    if hits:
        result["hallucinations"] = hits
        # also tack onto validator_notes so existing audit consumers see them
        notes = result.get("validator_notes") or []
        if not isinstance(notes, list):
            notes = []
        notes.extend([f"hallucination_flag: {h}" for h in hits])
        result["validator_notes"] = notes
    return result
