import hmac
import hashlib
import json
import os
import re
import uuid
from urllib.parse import unquote

from fastapi import FastAPI, APIRouter, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import WebhookPayload, RCARequest, RCAResponse
from .jenkins import get_console_log, get_build_meta, get_stage_errors
from .window import extract_window, extract_failed_stage
from .sanitize import sanitize
from .classifier import classify
from .llm import run_rca, build_initial_tool_ctx
from .agent import run_agent
from .git_fresh import ensure_fresh_many
from .cache import (
    is_duplicate, mark_processed, over_daily_cap, add_spend,
    get_rca, set_rca,
)
from .evidence import verify as verify_evidence
from . import mcp_tools  # for service_lookup in post-RCA value validator
from . import canary_analyzer  # for Kayenta Referee URL extraction on canary_fail
from .audit import record as audit_record, read_by_request_id, list_recent
from . import auth as oauth
from .slack import post as slack_post
import subprocess
import yaml
from pathlib import Path

# Jinja2 environment for HTML report rendering. Autoescape ON for all .html
# templates so values from the audit JSON can't inject script tags.
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

# Color hint per error_class for the badge in the HTML report.
_CLASS_COLORS = {
    "compliance":          "bg-amber-200 text-amber-900",
    "canary_fail":         "bg-red-200 text-red-900",
    "canary_script_error": "bg-violet-200 text-violet-900",
    "health_check":        "bg-rose-200 text-rose-900",
    "aws_limit":           "bg-orange-200 text-orange-900",
    "parse_error":         "bg-yellow-200 text-yellow-900",
    "java_runtime":        "bg-red-200 text-red-900",
    "scm":                 "bg-indigo-200 text-indigo-900",
    "network":             "bg-sky-200 text-sky-900",
    "dependency":          "bg-fuchsia-200 text-fuchsia-900",
    "ssm":                 "bg-cyan-200 text-cyan-900",
    "timeout":             "bg-amber-200 text-amber-900",
    "unknown":             "bg-slate-200 text-slate-700",
}

app = FastAPI(title="jenkins-rca", version="0.1.0")
# Session middleware for Google SSO dashboard auth. Cookie is signed with
# JENKINS_RCA_SESSION_SECRET via itsdangerous (HMAC). If the env var is
# unset, session reads return {} and auth is effectively disabled (dev
# mode — see jenkins_rca/auth.py::is_enabled).
app.add_middleware(
    SessionMiddleware,
    secret_key=oauth.SESSION_SECRET or "insecure-dev-only-please-set-JENKINS_RCA_SESSION_SECRET",
    session_cookie="jenkins_rca_session",
    https_only=True,
    same_site="lax",
    max_age=24 * 60 * 60,  # 24h
)
# All routes go on this router so we can mount them at both root and /rca.
# /rca prefix is for ALB path-based routing (jenkins-rca.jinka.in/rca/*);
# root mount keeps direct-port access working for backward compat.
router = APIRouter()

# Config loaded from env (set via SOPS decrypt on startup)
JENKINS_URL = os.environ.get("JENKINS_RCA_JENKINS_URL", "http://10.34.42.254:8080")
JENKINS_USER = os.environ.get("JENKINS_RCA_JENKINS_USER", "g.hariharan@blackbuck.com")
JENKINS_TOKEN = os.environ.get("JENKINS_RCA_JENKINS_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("JENKINS_RCA_WEBHOOK_SECRET", "")
LLM_API_KEY = os.environ.get("JENKINS_RCA_LLM_API_KEY", "")
LLM_PROVIDER = os.environ.get("JENKINS_RCA_LLM_PROVIDER", "gemini")

JENKINS_AUTH = (JENKINS_USER, JENKINS_TOKEN)


@router.get("/healthz")
async def health():
    return {"status": "ok", "provider": LLM_PROVIDER}


def verify_hmac(payload: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/v1/rca/webhook")
async def rca_webhook(
    request: Request,
    x_bbctl_signature: str = Header(None),
):
    body = await request.body()

    if WEBHOOK_SECRET and x_bbctl_signature:
        if not verify_hmac(body, x_bbctl_signature):
            raise HTTPException(status_code=401, detail="invalid signature")

    payload = WebhookPayload(**json.loads(body))

    # Multi-Jenkins routing — resolve buildUrl to the correct Jenkins base
    # + credentials. Zinka behavior preserved via legacy env fallback in
    # jenkins_registry. Failures return meaningful HTTP status instead of
    # opaque 500 (previously any Jenkins auth/network error surfaced as 500).
    from . import jenkins_registry
    from httpx import HTTPStatusError, RequestError

    build_url_hint = (payload.buildUrl or "").strip() if hasattr(payload, "buildUrl") else ""
    # Fallback: reconstruct a probe URL when webhook didn't include buildUrl
    # (older Jenkins plugin). Use legacy JENKINS_URL prefix so the router
    # still lands on zinka.
    if not build_url_hint:
        build_url_hint = f"{JENKINS_URL.rstrip('/')}/job/{payload.job}/{payload.build}/"

    try:
        base_url, auth, jenkins_name = jenkins_registry.resolve(build_url_hint)
    except jenkins_registry.RegistryConfigError as e:
        # Env misconfigured — operator issue, not caller's fault.
        raise HTTPException(status_code=500, detail=f"jenkins registry misconfigured: {e}")
    except ValueError as e:
        # Unknown Jenkins — caller sent a buildUrl we don't recognize.
        raise HTTPException(status_code=400, detail=str(e))

    try:
        return await _run_rca(
            payload.job, payload.build, payload.service, deep=False,
            jenkins_base=base_url, jenkins_auth=auth, jenkins_name=jenkins_name,
        )
    except HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            raise HTTPException(status_code=401,
                detail=f"Jenkins {jenkins_name} auth failed ({code}). Check registry credentials.")
        if code == 404:
            raise HTTPException(status_code=404,
                detail=f"Jenkins {jenkins_name}: build {payload.job}/{payload.build} not found")
        raise HTTPException(status_code=502,
            detail=f"Jenkins {jenkins_name} returned HTTP {code}")
    except RequestError as e:
        raise HTTPException(status_code=504,
            detail=f"Jenkins {jenkins_name} unreachable: {e}")


# NOTE on route order: FastAPI evaluates routes in registration order. The
# `.json` route MUST be registered before the catch-all HTML route, otherwise
# the HTML route's `{request_id}` would greedily match `<uuid>.json` (with
# `.json` ending up as part of request_id), failing the uuid regex inside
# `read_by_request_id` and returning a misleading 404.
@router.get("/v1/report/{request_id}.json")
async def rca_report_json(request_id: str):
    """Raw JSON view of the audit record — for debugging / scripts."""
    audit = read_by_request_id(request_id)
    if not audit:
        raise HTTPException(status_code=404, detail="report not found")
    return audit


@router.get("/v1/report/{request_id}", response_class=HTMLResponse)
async def rca_report(request_id: str):
    """Render a stored RCA result as an HTML page.

    Looks up the audit record by request_id (uuid). The audit record is
    written by `audit_record(...)` after every RCA run and lives at
    /var/log/jenkins-rca/<uuid>.json. The HTML view is the shareable canonical
    surface — same URL appears in Jenkins console, Jenkins build description,
    Slack alerts, and VictorOps details.
    """
    audit = read_by_request_id(request_id)
    if not audit:
        raise HTTPException(status_code=404, detail="report not found")
    return _render_report(audit)


@router.get("/v1/auth/login")
async def auth_login(request: Request, next: str = "/rca/v1/dashboard"):
    """Kick off Google OAuth web flow. Redirects browser to Google."""
    if not oauth.is_enabled():
        # Auth not configured — go straight to the requested page
        return RedirectResponse(url=next, status_code=302)
    state = oauth.new_state_token()
    request.session["oauth_state"] = state
    request.session["oauth_next"]  = next
    return RedirectResponse(url=oauth.login_url(state), status_code=302)


@router.get("/v1/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Receive the authorization code from Google, exchange for id_token,
    verify email domain, set session cookie."""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error from Google: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="missing authorization code")
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="invalid state — possible CSRF")
    try:
        tokens = await oauth.exchange_code(code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"token exchange failed: {e}")
    id_token = tokens.get("id_token", "")
    claims = oauth.decode_id_token(id_token)
    email = claims.get("email", "")
    if not oauth.email_allowed(email):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied. Only {oauth.ALLOWED_DOMAIN} accounts are allowed. (got: {email or 'no email'})",
        )
    # Verified — set session
    request.session["user"] = {
        "email":   email,
        "name":    claims.get("name", ""),
        "picture": claims.get("picture", ""),
    }
    next_url = request.session.pop("oauth_next", "/rca/v1/dashboard")
    return RedirectResponse(url=next_url, status_code=302)


@router.get("/v1/auth/logout")
async def auth_logout(request: Request):
    """Clear the session cookie + redirect to landing."""
    request.session.clear()
    return RedirectResponse(url="/rca/v1/dashboard", status_code=302)


@router.get("/v1/dashboard", response_class=HTMLResponse)
async def rca_dashboard(days: int = 14, user: dict = Depends(oauth.require_auth)):
    """Landing page — pipelines (jobs) that had RCAs in the last N days.

    Groups audit records by `job`. Per-pipeline card shows count + most
    recent failure summary + class chip. Click → /v1/dashboard/<job>.
    """
    days = max(1, min(days, 30))  # clamp
    records = list_recent(days=days)
    # Group by job
    by_job: dict[str, list[dict]] = {}
    for r in records:
        by_job.setdefault(r["job"], []).append(r)
    # Build pipeline card list sorted by most-recent-failure DESC
    pipelines = []
    for job, recs in by_job.items():
        recs.sort(key=lambda x: x["recorded_at"], reverse=True)
        latest = recs[0]
        pipelines.append({
            "job": job,
            "count": len(recs),
            "latest": latest,
            "classes": sorted({r["error_class"] for r in recs}),
        })
    pipelines.sort(key=lambda p: p["latest"]["recorded_at"], reverse=True)
    tmpl = _jinja.get_template("dashboard.html")
    return tmpl.render(
        pipelines=pipelines,
        total_rcas=len(records),
        days=days,
        class_colors=_CLASS_COLORS,
        user=user,
    )


@router.get("/v1/dashboard/{job}", response_class=HTMLResponse)
async def rca_pipeline_builds(job: str, days: int = 14, user: dict = Depends(oauth.require_auth)):
    """Per-pipeline view — list of failed builds for one job.

    Each row links to /v1/report/<request_id> for the full RCA.
    """
    # Browser may double-encode the path segment when our dashboard link
    # contains literal '%' characters (legacy encoded job names). Decode
    # the path param so it matches the normalized job stored in audit.
    job = unquote(job)
    days = max(1, min(days, 30))
    records = list_recent(days=days)
    job_records = [r for r in records if r["job"] == job]
    if not job_records:
        # Job had no RCAs in the window — show empty state, not 404
        pass
    job_records.sort(key=lambda x: x["recorded_at"], reverse=True)
    tmpl = _jinja.get_template("pipeline_builds.html")
    return tmpl.render(
        job=job,
        builds=job_records,
        count=len(job_records),
        days=days,
        class_colors=_CLASS_COLORS,
        user=user,
    )


def _render_report(audit: dict) -> str:
    """Build template vars from the audit record and render the HTML page."""
    rca = audit.get("rca") or {}
    fix = rca.get("suggested_fix")
    fix_is_map = isinstance(fix, dict)
    fix_items = list(fix.items()) if fix_is_map else []

    job = audit.get("job", "")
    build = audit.get("build", "")
    build_url = audit.get("build_url") or _guess_build_url(job, build)

    # Extract Jira ticket key from RCA so the report header can deep-link
    # to the ticket. Looks in summary + root_cause + evidence snippets.
    # First match wins. Empty string when no ticket found.
    from . import jira as _jira
    _searchable = " ".join([
        str(rca.get("summary") or ""),
        str(rca.get("root_cause") or ""),
        " ".join(str(e.get("snippet") or "") for e in (rca.get("evidence") or []) if isinstance(e, dict)),
    ])
    jira_keys = _jira.extract_tickets(_searchable)
    jira_key = jira_keys[0] if jira_keys else ""
    jira_base = os.environ.get("JENKINS_RCA_JIRA_URL", "https://blackbuck.atlassian.net").rstrip("/")
    jira_url = f"{jira_base}/browse/{jira_key}" if jira_key else ""

    tmpl = _jinja.get_template("rca_report.html")
    return tmpl.render(
        request_id=audit.get("request_id", ""),
        recorded_at=audit.get("recorded_at", ""),
        job=job,
        build=build,
        build_url=build_url,
        service=audit.get("service", ""),
        error_class=rca.get("error_class") or audit.get("error_class") or "unknown",
        class_color=_CLASS_COLORS.get(rca.get("error_class") or audit.get("error_class"), _CLASS_COLORS["unknown"]),
        failed_stage=rca.get("failed_stage", "—"),
        confidence=rca.get("confidence", "—"),
        needs_deeper=bool(rca.get("needs_deeper")),
        cost_usd=audit.get("cost_usd") or rca.get("cost_usd") or 0,
        tokens_in=(rca.get("tokens_used") or {}).get("input", 0),
        tokens_out=(rca.get("tokens_used") or {}).get("output", 0),
        summary=rca.get("summary", "—"),
        root_cause=rca.get("root_cause", "—"),
        suggested_fix=fix if not fix_is_map else "",
        fix_is_map=fix_is_map,
        fix_items=fix_items,
        suggested_commands=rca.get("suggested_commands", []),
        evidence=rca.get("evidence", []),
        canary_reports=rca.get("canary_reports", []),
        provider=audit.get("provider", "—"),
        redactions=", ".join(audit.get("redactions") or []) or None,
        log_window_chars=audit.get("log_window_chars", 0),
        jira_key=jira_key,
        jira_url=jira_url,
    )


def _guess_build_url(job: str, build) -> str:
    """Best-effort Jenkins URL when the audit record didn't capture build_url."""
    base = JENKINS_URL.rstrip("/")
    if not job or build in (None, ""):
        return base
    # Jenkins URL-encodes path segments but spaces become %20 in the job name.
    safe_job = str(job).replace(" ", "%20")
    return f"{base}/job/{safe_job}/{build}/"


@router.post("/v1/rca")
async def rca_cli(req: RCARequest):
    meta = await get_build_meta(req.job, req.build, JENKINS_URL, JENKINS_AUTH)
    service = meta.get("actions", [{}])[0].get("parameters", [{}])
    # extract SERVICE param
    svc = req.job
    for action in meta.get("actions", []):
        for param in action.get("parameters", []):
            if param.get("name") == "SERVICE":
                svc = param["value"]
    return await _run_rca(req.job, req.build, svc, deep=req.deep)


async def _run_rca(
    job: str,
    build: int,
    service: str,
    deep: bool = False,
    jenkins_base: str | None = None,
    jenkins_auth: tuple[str, str] | None = None,
    jenkins_name: str = "zinka",
) -> dict:
    """Run RCA for one build.

    jenkins_base / jenkins_auth override the module-level defaults when the
    caller has already resolved a specific Jenkins instance (e.g. via
    jenkins_registry.resolve on webhook buildUrl). Callers that don't route
    (CLI, dashboard force-rerun) omit them and fall back to legacy zinka
    JENKINS_URL / JENKINS_AUTH.
    """
    base_for_jenkins = (jenkins_base or JENKINS_URL).rstrip("/")
    auth_for_jenkins = jenkins_auth or JENKINS_AUTH
    # Normalize job name — Jenkins job names with spaces arrive URL-encoded
    # when callers pass them through query params or hand-built JSON
    # ("Stagger%20Prod%20Plus%20One"). Decode once at the boundary so cache
    # key, dedup, audit file, and dashboard grouping all see the same value.
    job = unquote(job)
    if over_daily_cap():
        raise HTTPException(status_code=429, detail="daily cost cap reached")

    # 24h cache: same job+build returns prior RCA without LLM call.
    # `deep=true` bypasses cache (operator explicitly wants re-analysis with
    # wider context). `?nocache=true` query param could be added later.
    if not deep:
        cached = get_rca(job, build)
        if cached:
            cached_copy = dict(cached)
            cached_copy["from_cache"] = True
            return cached_copy

    existing = is_duplicate(job, build)
    if existing and not deep:
        return {"cached": True, "request_id": existing}

    request_id = str(uuid.uuid4())

    # Per-RCA freshness pull (hybrid model). Cheap shallow fetch on both
    # repos so the agent / tool-context sees the latest commit. Cron at
    # /etc/cron.d/jenkins-rca-sync is a backstop; this is the fast path.
    freshness = ensure_fresh_many([
        ("jenkins_pipeline", None),
        ("InfraComposer", None),
    ])

    raw_log = await get_console_log(job, build, base_for_jenkins, auth_for_jenkins)
    build_meta = await get_build_meta(job, build, base_for_jenkins, auth_for_jenkins)
    # Fetch FAILED stage error messages via Jenkins workflow REST API.
    # consoleText may not have flushed the trailing exception trace yet when
    # this endpoint is called from a post.failure block. wfapi/describe
    # populates error.message as soon as the stage transitions to FAILED,
    # so it gives the real exception (e.g. groovy.lang.MissingMethodException)
    # even when the console hasn't caught up.
    stage_errors = await get_stage_errors(job, build, base_for_jenkins, auth_for_jenkins)

    window = extract_window(raw_log, deep=deep)
    # Prepend stage error messages so the classifier + LLM see the real
    # exception string regardless of console-buffer timing.
    if stage_errors:
        err_block_lines = ["=== Failed stages (from Jenkins workflow API) ==="]
        for se in stage_errors:
            err_block_lines.append(f"Stage '{se['name']}' status={se['status']}")
            if se.get("error_message"):
                err_block_lines.append(se["error_message"])
        err_block = "\n".join(err_block_lines) + "\n\n"
        window = err_block + window
    clean_window, redactions = sanitize(window)
    error_class = classify(clean_window)
    # Annotate build_meta with the actual last-entered stage from the log so
    # LLM doesn't guess between similarly-named stages (Prod+1 vs Prod, etc.).
    # Also inject job + build identifiers — Jenkins' /api/json response uses
    # `id`/`number`/`displayName` rather than literal `job`/`build`, but our
    # trace-dump code (and any future consumer of build_meta downstream of
    # the LLM call) needs both to name per-build artifact files.
    # Failed-stage detection — TWO sources, prefer authoritative wfapi:
    #   1. Jenkins wfapi (stage_errors) — knows actual FAILED status per stage.
    #      Use the LAST failed stage as the canonical attribution.
    #   2. extract_failed_stage(raw_log) — regex over log, fallback only.
    # The log-regex path historically mis-attributed failures to retry-loop
    # transient errors inside earlier passing stages (build #5438: matched
    # `AWS S3 CP command failed: script returned exit code 2` inside Prod+1
    # which recovered, instead of the actual final Deploy stage failure).
    detected_stage = None
    # STRATEGY 0: locator-anchored stage. Find the deterministic exit-point
    # line, walk UP to the enclosing `[Pipeline] { (X)` marker. Strongest
    # signal for multi-phase pipelines (Prod+1 THEN Prod) where an earlier
    # stage logged a transient retry error but the real abort is much later.
    # build #5627 telematics-dashcam: real abort in `Infra` (prod env), but
    # extract_failed_stage returned `Build` (transient retry marker) and the
    # LLM guessed `prodPlusOne` (from a `Running prechecks for stage:` line).
    try:
        from . import failure_locator as _fl0
        _st0 = _fl0.locate_failing_stage(raw_log)
        if _st0:
            detected_stage = _st0
    except Exception:
        pass

    # STRATEGY A: failure_locator step_name is the strongest signal — it
    # points to the exact parallel branch that exited non-zero. The parent
    # stage name is the prefix before "_i-<instance-id>" (e.g.
    # "Deploy_i-0e67..." → "Deploy", "Rollout_i-xxx" → "Rollout").
    # wfapi alone is unreliable here because skipped-cascade stages
    # (Rollout/Destroy) often show as FAILED while the actual parent of
    # the broken parallel branches (Deploy) may show as SUCCESS.
    if not detected_stage:
        try:
            from . import failure_locator as _fl
            fl_result = _fl.locate_failure(raw_log)
            step = (fl_result.get("step_name") or "") if fl_result else ""
            # Match "Deploy_i-XXX" → "Deploy", "Rollout_i-XXX" → "Rollout"
            m = re.match(r"^([A-Za-z][A-Za-z0-9 ]*?)_i-", step)
            if m:
                detected_stage = m.group(1).strip()
        except Exception:
            pass

    # STRATEGY B: wfapi authoritative status. Prefer FIRST FAILED
    # non-declarative non-cleanup stage. Cleanup stages (Destroy/Rollout)
    # marked FAILED are almost always cascade-from-earlier-failure.
    if not detected_stage and stage_errors:
        _CLEANUP_STAGES = {"destroy", "rollout", "destroy prod+1"}
        first_failed = None
        first_real_failed = None
        for se in stage_errors:
            name = (se.get("name") or "").strip()
            if not name:
                continue
            if name.lower().startswith("declarative:"):
                continue
            if first_failed is None:
                first_failed = name
            if first_real_failed is None and name.lower() not in _CLEANUP_STAGES:
                first_real_failed = name
        detected_stage = first_real_failed or first_failed

    # STRATEGY C: regex over log (fallback when wfapi unavailable).
    if not detected_stage:
        detected_stage = extract_failed_stage(raw_log)
    build_meta = dict(build_meta)
    build_meta["job"] = job
    build_meta["build"] = build
    if detected_stage:
        build_meta["detected_failed_stage"] = detected_stage
    # Also stash raw_log on build_meta for analyzers that need full log
    # (canary stage parser; health_check `healthy.sh` line parser;
    # failure_locator backwards scan for actual exit-point). All need data
    # that the sanitized window drops. raw_log is stripped from build_meta
    # AFTER the LLM call (~line 605) so it never leaks into audit/response.
    if not isinstance(build_meta, dict):
        build_meta = dict(build_meta)
    build_meta["_raw_log"] = raw_log

    # Classes worth the agent's deeper trace through the actual source code.
    # Cheap one-shot stays good enough for the rest (timeout, ssm, network,
    # dependency, java_runtime when stack trace is self-explanatory).
    # compliance + unknown are excluded:
    #   - compliance is a Jira-field-missing problem, not a code-trace problem.
    #   - unknown is the catch-all class — when the classifier can't fit the
    #     error, the agent often has no clear source signal to trace either,
    #     so it drifts and emits prose instead of JSON.
    # Primer for both already carries jira.tickets + runbook + (for unknown)
    # the wide source.trace + docs.catalog + self-classify guide, so the
    # one-shot path is the right home for them.
    AGENT_CLASSES = {
        "canary_fail", "canary_script_error",
        # compliance: drill plan is per-MODE (5 plus Mode 6 for the
        # quick-infra family). One-shot can't read read_runbook so
        # the LLM defaults to "add entry to config.json" even for
        # create-quick-infra where that recipe is wrong. Agent path
        # lets the LLM fetch the runbook + match the right Mode.
        # Build 42 of create-quick-infra case.
        "compliance",
        "health_check", "parse_error", "scm",
        # terraform errors benefit from agent code-trace: failed module
        # in main.tf → read module body → identify which AWS resource is
        # wedged. Runbook gives the LLM state-surgery procedures it can
        # surface as suggested_commands.
        "terraform",
        # stale_tf_state: when classifier is right, the LLM needs the
        # stale_tf_state.md runbook (precheck abort recipe) + repo access
        # to walk the precheck.groovy logic. When classifier is wrong
        # (build 5177: real cause was TooMany ALB limit), agent mode gives
        # the LLM tools to read aws_limit runbook + derive ARNs + reclassify
        # in output. One-shot path traps it in the wrong class.
        "stale_tf_state",
        # aws_limit: runbook has exact ARN derivation steps (rule_arn →
        # alb_arn) the LLM cannot improvise. Without tool access, one-shot
        # emits <alb_arn> placeholders (build 5177). Agent path lets the
        # LLM read aws_limit.md + run aws_describe to fill real IDs.
        "aws_limit",
        # config_validation: stage 1.3 of pre_deployment finds drift
        # between config.json and live AWS state (missing AMI/subnet/SG/
        # key-pair/IAM-profile). Needs aws_describe + service_lookup
        # for the offending resource type before suggesting a config.json
        # PR. One-shot path can't run those calls. Build 61 of
        # HotFix-NonCanary case.
        "config_validation",
        # jenkins_agent_offline: slave bounce mid-step. Primary cause is
        # the build agent infrastructure, not the pipeline code. Agent
        # path lets the LLM repo_search the slave-bounce pattern, count
        # occurrences (one-off vs repeating), and read the runbook for
        # the right primary-vs-secondary framing. Build 15 of Stagger
        # Scaling case.
        "jenkins_agent_offline",
        # java_runtime: stack trace points at a file:line. Agent can read
        # that file via repo_read_file and cite the exact line for the
        # operator (e.g. WorkflowScript:330 → create-quick-infra.groovy:330
        # showing the wrong-arg call). One-shot path lacks tool access so
        # it can only paraphrase the stack trace without locating the bug
        # in the source. ~3-4 tool calls typical, $0.10-0.15 added cost.
        "java_runtime",
        # build_tool_crash: Gradle/Maven daemon crashed mid-task. Agent
        # path reads the build.gradle / pom.xml of the failing service
        # (via repo_read_file) to identify the heap config + dependency
        # graph that should be tuned. Runbook gives the daemon-heap recipe
        # the LLM needs to surface as suggested_commands. Build 5225
        # Stagger Prod+1 case (indent-microservice).
        "build_tool_crash",
        # Phase-7 (May 2026): runbooks now exist for these classes —
        # promote them out of the one-shot fallback path so the agent
        # can drill the failing call site + apply the per-class
        # Action template.
        # timeout: pipeline wrapper hit budget. Agent reads the
        # timeout(time:) helper code, identifies which operation, then
        # decides transient-vs-persistent. One-shot was guessing.
        "timeout",
        # network: TCP failure (refused / timed out / unknown host).
        # Agent can repo_search the failing URL, identify caller, +
        # check AWS SG / route table state via aws_describe.
        "network",
        # dependency: Gradle/Maven/npm/pip 404 on an artifact. Agent
        # reads the service repo's build.gradle / pom.xml to identify
        # the bumped version + recent commit that caused the miss.
        "dependency",
        # ssm: SendCommand round-trip failed. Agent checks SSM agent
        # state via aws_describe + identifies whether it's an SSM
        # issue or a re-classifiable shell-script-exit-code issue.
        "ssm",
    }

    # Phase 3: opt-in to all-classes-agent-mode via env var. When set,
    # EVERY error_class routes through the agent loop, dropping the
    # one-shot primer-stuffed path for compliance / aws_limit /
    # parse_error / unknown / etc. Default = off so production keeps
    # current behaviour during soak. Flip default after 2-week soak.
    _force_agent = bool(os.environ.get("JENKINS_RCA_FORCE_AGENT_MODE"))

    try:
        if LLM_PROVIDER == "openai" and (_force_agent or error_class in AGENT_CLASSES):
            # Run the agent. It still wants the same pre-computed tool context
            # as a primer so it doesn't burn calls re-fetching cheap things.
            initial_ctx = await build_initial_tool_ctx(
                service=service, error_class=error_class,
                log_window=clean_window, build_meta=build_meta,
            )
            result = await run_agent(
                api_key=LLM_API_KEY,
                job=job, build=build, service=service,
                build_meta=build_meta,
                log_window=clean_window,
                error_class=error_class,
                initial_tool_ctx=initial_ctx,
                jenkins_url=base_for_jenkins, jenkins_auth=auth_for_jenkins,
            )
        else:
            result = await run_rca(
                LLM_PROVIDER,
                api_key=LLM_API_KEY,
                service=service,
                build_meta=build_meta,
                log_window=clean_window,
                error_class=error_class,
                deep=deep,
            )
    except Exception as e:
        # Catch OpenAI permission / quota / network failures and any other
        # LLM-side crash so the caller (Jenkins post.failure block) gets a
        # clean JSON error response instead of an HTTP 500 with an HTML
        # stack trace. The pipeline can then echo it cleanly and continue
        # the rest of the post block (input prompt, rollback).
        err_class = e.__class__.__name__
        err_msg = str(e)
        # Single source of truth lives in jenkins_rca.agent._DEFAULT_MODEL.
        # Import here rather than at module top to avoid circular load.
        from .agent import _DEFAULT_MODEL as _AGENT_DEFAULT_MODEL
        rca_model = os.environ.get("JENKINS_RCA_MODEL", _AGENT_DEFAULT_MODEL)
        print(f"[main] LLM call failed: {err_class}: {err_msg}",
              file=__import__('sys').stderr, flush=True)
        # Build a stub result that still goes through the normal audit /
        # cache / response path so the operator sees the failure in the
        # report URL like any other RCA.
        hint = ""
        if "model_not_found" in err_msg or "does not have access" in err_msg:
            hint = (f" Model `{rca_model}` is not available in this OpenAI "
                    f"project. Verify with `curl https://api.openai.com/v1/models "
                    f"-H 'Authorization: Bearer $OPENAI_API_KEY' | jq '.data[].id'` "
                    f"and either fix JENKINS_RCA_MODEL or request access on OpenAI dashboard.")
        result = {
            "summary": f"LLM call failed ({err_class}). RCA unavailable for this build.",
            "failed_stage": build_meta.get("detected_failed_stage", "—"),
            "error_class": error_class,
            "root_cause": f"{err_class}: {err_msg[:300]}.{hint}",
            "evidence": [],
            "suggested_fix": "Check jenkins-rca journalctl for the full stack trace. "
                             "If it's a model-access issue, fix JENKINS_RCA_MODEL.",
            "suggested_commands": [],
            "confidence": 0.0,
            "needs_deeper": True,
            "tokens_used": {"input": 0, "output": 0},
            "_llm_error": True,
            "_llm_error_class": err_class,
        }

    # Post-RCA response post-processing has been REMOVED per the
    # phase-10 design decision: do not modify LLM output by code. If
    # the LLM emits a hallucinated value, the fix belongs upstream
    # (better schema, better prompt, better model) — not in a band-aid
    # substitution after the fact. The previous value_validator that
    # rewrote port 8080 / /admin/version / gps.log defaults is gone.
    # Old code path lives at jenkins_rca/value_validator.py but is no
    # longer wired into the request handler.
    #
    # EXCEPTION — failed_stage. This is NOT an inferred infra detail (port,
    # path) the LLM is supposed to derive; it is a DETERMINISTIC fact we
    # already computed by locating the exit-point line in the raw log and
    # walking up to its enclosing `[Pipeline] { (X) }` marker
    # (detected_failed_stage, Strategy 0). In multi-phase pipelines the LLM
    # repeatedly lifts a `Running prechecks for stage: X` phase label of an
    # EARLIER phase that PASSED (build #5627: emitted `prodPlusOne` while the
    # pipeline actually aborted in `Infra`), even when told the verified stage
    # and instructed not to contradict it. Pinning a proven fact over a
    # hallucination is a different category from fabricating analysis values,
    # so we override here. Only when we have a confident detection.
    _det_stage = build_meta.get("detected_failed_stage") if isinstance(build_meta, dict) else None
    if _det_stage and _det_stage != "—" and isinstance(result, dict):
        if result.get("failed_stage") != _det_stage:
            print(f"[main] failed_stage override: LLM said "
                  f"{result.get('failed_stage')!r} -> deterministic {_det_stage!r}",
                  file=__import__('sys').stderr, flush=True)
            result["failed_stage"] = _det_stage

    # Stash freshness info on the result so it surfaces in the audit/report
    result["repos_freshness"] = freshness
    # On canary_fail, persist failed Kayenta Referee report URLs into the
    # rca result so rca_report.html sidebar can link to them. Must run BEFORE
    # raw_log is stripped from build_meta below.
    if error_class == "canary_fail":
        try:
            raw_log_for_canary = (build_meta or {}).get("_raw_log") if isinstance(build_meta, dict) else None
            reports = canary_analyzer.extract_canary_reports(raw_log_for_canary or clean_window or "")
            if reports:
                result["canary_reports"] = reports
        except Exception:
            # Non-fatal — sidebar block simply stays hidden if extraction fails
            pass
    # Persist deterministic failure locator output (step_name, exit_code,
    # failing_command) onto the rca result so report consumers can show the
    # verified ground truth alongside the LLM's narrative. Same raw_log access
    # rule — must run BEFORE raw_log is stripped below.
    try:
        from . import failure_locator
        raw_log_for_locator = (build_meta or {}).get("_raw_log") if isinstance(build_meta, dict) else None
        failure_facts = failure_locator.locate_failure(raw_log_for_locator or clean_window or "")
        if failure_facts:
            # Drop the verbose `context` field from audit (already in evidence
            # by virtue of being in the LLM prompt). Keep the slim summary.
            slim = {k: v for k, v in failure_facts.items() if k != "context"}
            result["failure_locator"] = slim
    except Exception:
        pass  # non-fatal
    # Strip raw_log from build_meta after LLM call so it doesn't leak into
    # audit log / response (it's massive).
    if isinstance(build_meta, dict) and "_raw_log" in build_meta:
        build_meta = {k: v for k, v in build_meta.items() if k != "_raw_log"}
    result["request_id"] = request_id

    # cost estimate by provider. OpenAI side honors JENKINS_RCA_MODEL env so
    # cost reflects the actual model used (delegates to agent._pricing_for).
    tokens_in = result["tokens_used"].get("input", 0)
    tokens_out = result["tokens_used"].get("output", 0)
    if LLM_PROVIDER == "openai":
        from .agent import _pricing_for, _DEFAULT_MODEL as _AGENT_DEFAULT_MODEL
        rca_model = os.environ.get("JENKINS_RCA_MODEL", _AGENT_DEFAULT_MODEL)
        in_per_tok, out_per_tok = _pricing_for(rca_model)
        cost = tokens_in * in_per_tok + tokens_out * out_per_tok
        result["model_used"] = rca_model
    else:
        # gemini-2.0-flash: $0.075/1M input, $0.30/1M output
        cost = (tokens_in / 1_000_000 * 0.075) + (tokens_out / 1_000_000 * 0.30)
    add_spend(cost)
    result["cost_usd"] = round(cost, 6)

    # Verify each evidence citation against repos on disk
    if "evidence" in result:
        result["evidence"] = verify_evidence(result["evidence"])

    # Hallucination gate — flag claims (HealthCheckPath, Target.Port, NOT_IN_CONFIG)
    # that LLM made up without log support. Non-destructive: appends warnings to
    # result["hallucinations"] and result["validator_notes"]. Operators see the
    # flags in the RCA report; doesn't block the response.
    try:
        from . import value_validator
        value_validator.flag_hallucinations(result, clean_window)
    except Exception:
        pass  # non-fatal — never block RCA delivery on validator bug

    mark_processed(job, build, request_id)
    set_rca(job, build, result)  # 24h cache for future repeat queries

    # Audit log + Slack notify (non-blocking, best-effort)
    audit_record({
        "request_id": request_id,
        "job": job,
        "build": build,
        "service": service,
        "error_class": error_class,
        "provider": LLM_PROVIDER,
        "cost_usd": cost,
        "redactions": redactions,
        "log_window_chars": len(clean_window),
        "log_window_sample": clean_window[:500],
        "build_url": build_meta.get("url") if isinstance(build_meta, dict) else None,
        "rca": result,
    })
    await slack_post(result, job, build)

    return result


# Mount routes at both root (for direct port access) and /rca (for ALB
# path-based routing via jenkins-rca.jinka.in/rca/*). Both URL shapes work.
app.include_router(router)
app.include_router(router, prefix="/rca")

# Static assets (theme.css, theme.js, logo fallback) mounted at both
# /static and /rca/static so URLs resolve under either routing prefix.
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.mount("/rca/static", StaticFiles(directory=str(_STATIC_DIR)), name="static-rca")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("jenkins_rca.main:app", host="0.0.0.0", port=7070, reload=False)
