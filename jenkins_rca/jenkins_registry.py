"""Multi-Jenkins registry — resolve webhook buildUrl to correct base + auth.

Loaded from env at module-import time. Two sources feed the registry:

1. LEGACY single-Jenkins env (backward compat — zinka setup):
     JENKINS_RCA_JENKINS_URL       e.g. http://10.34.42.254:8080
     JENKINS_RCA_JENKINS_USER      e.g. g.hariharan@blackbuck.com
     JENKINS_RCA_JENKINS_TOKEN     (from Secrets Manager)

2. NEW multi-Jenkins JSON env — JENKINS_RCA_JENKINS_INSTANCES:
     [
       {
         "name": "finserv",
         "base_url": "https://finserv-jenkins.jinka.in",
         "user": "devops.jira@blackbuck.com",
         "token_ref": "JENKINS_RCA_FINSERV_TOKEN"   # env var holding the actual token
       },
       {
         "name": "tzf",
         "base_url": "https://tzf-jenkins.jinka.in",
         "user": "...",
         "token_ref": "JENKINS_RCA_TZF_TOKEN"
       }
     ]

Router logic: match webhook `buildUrl` prefix against each instance's
`base_url`. First match wins. Legacy zinka env is added last so specific
instances take precedence when URLs overlap.

Falls back to legacy env when JENKINS_RCA_JENKINS_INSTANCES is unset —
keeps single-Jenkins deployments working unchanged.
"""
from __future__ import annotations

import json
import os


class RegistryConfigError(RuntimeError):
    """Raised when JENKINS_RCA_JENKINS_INSTANCES is set but unparseable.

    Fail-CLOSED: rather than silently ignoring malformed JSON and letting
    the legacy zinka fallback serve foreign-Jenkins webhooks with the
    WRONG credentials (fail-open state drift), we bubble the parse error
    up so the caller returns HTTP 500 and operators notice.
    """


def _load_instances() -> list[dict]:
    """Parse env each call. No caching — env may change between requests when
    Secrets Manager rotates.

    Fail-CLOSED semantics:
      - If JENKINS_RCA_JENKINS_INSTANCES is set but malformed JSON → raises
        RegistryConfigError. Prevents silent fallback to legacy zinka creds
        for what may have been a finserv/tzf webhook.
      - If an individual instance in the JSON is malformed (missing url /
        token) → THAT instance is skipped with a log line, but other valid
        instances still register. Safer than dropping the whole registry.
    """
    out: list[dict] = []

    raw = os.environ.get("JENKINS_RCA_JENKINS_INSTANCES", "").strip()
    if raw:
        try:
            entries = json.loads(raw)
        except Exception as e:
            # Fail-closed: don't hide config errors behind legacy fallback.
            raise RegistryConfigError(
                f"failed to parse JENKINS_RCA_JENKINS_INSTANCES as JSON: {e}"
            )
        if not isinstance(entries, list):
            raise RegistryConfigError(
                f"JENKINS_RCA_JENKINS_INSTANCES must be a JSON list, got {type(entries).__name__}"
            )
        for inst in entries:
            if not isinstance(inst, dict):
                print(f"[jenkins_registry] skipping non-dict entry: {inst!r}")
                continue
            token_ref = inst.get("token_ref")
            token = os.environ.get(token_ref, "") if token_ref else inst.get("token", "")
            if not token:
                print(f"[jenkins_registry] skipping instance {inst.get('name')!r}: no token from ref={token_ref!r}")
                continue
            base_url = (inst.get("base_url") or "").rstrip("/")
            if not base_url:
                print(f"[jenkins_registry] skipping instance {inst.get('name')!r}: no base_url")
                continue
            out.append({
                "name": inst.get("name") or base_url,
                "base_url": base_url,
                "user": inst.get("user") or "",
                "token": token,
            })

    # Legacy single-Jenkins fallback — always registered as "zinka" so
    # single-Jenkins deployments still work with only the legacy env set.
    legacy_url = (os.environ.get("JENKINS_RCA_JENKINS_URL") or "").rstrip("/")
    if legacy_url:
        out.append({
            "name": "zinka",
            "base_url": legacy_url,
            "user": os.environ.get("JENKINS_RCA_JENKINS_USER", ""),
            "token": os.environ.get("JENKINS_RCA_JENKINS_TOKEN", ""),
        })

    return out


def _prefix_matches(build_url: str, base_url: str) -> bool:
    """Safe prefix match — rejects host-suffix injection attacks.

    `startswith` alone lets a malicious buildUrl of
      https://finserv-jenkins.jinka.in.evil.com/job/x/1/
    match a registered base of
      https://finserv-jenkins.jinka.in
    because '.evil.com' just continues after the base. That would route the
    request to evil.com using finserv credentials → credential leak.

    Require the character AFTER the base to be either:
      - '/'  (path separator — normal case)
      - ''   (buildUrl exactly equals base_url — no path)
      - '?'  (query string, rare in Jenkins buildUrls)
      - '#'  (fragment, rare)
    """
    if not build_url.startswith(base_url):
        return False
    if len(build_url) == len(base_url):
        return True  # exact match
    return build_url[len(base_url)] in ("/", "?", "#")


def resolve(build_url: str) -> tuple[str, tuple[str, str], str]:
    """Return (base_url, (user, token), instance_name) for a given webhook buildUrl.

    Raises ValueError when no registered Jenkins matches.
    Raises RegistryConfigError when env is malformed (fail-closed).
    """
    build_url = (build_url or "").rstrip("/")
    if not build_url:
        raise ValueError("buildUrl is empty — cannot resolve Jenkins instance")

    for inst in _load_instances():
        if _prefix_matches(build_url, inst["base_url"]):
            return inst["base_url"], (inst["user"], inst["token"]), inst["name"]

    raise ValueError(
        f"buildUrl {build_url!r} does not match any registered Jenkins instance. "
        f"Known: {[i['name'] + '=' + i['base_url'] for i in _load_instances()]}"
    )


def all_instances() -> list[dict]:
    """Debug helper — list registered Jenkins instances (name + base_url only, no tokens)."""
    return [{"name": i["name"], "base_url": i["base_url"], "user": i["user"]} for i in _load_instances()]


def resolve_by_name(name: str) -> tuple[str, tuple[str, str]] | None:
    """Look up an instance by name (used by CLI paths that pass job+build directly)."""
    for inst in _load_instances():
        if inst["name"] == name:
            return inst["base_url"], (inst["user"], inst["token"])
    return None
