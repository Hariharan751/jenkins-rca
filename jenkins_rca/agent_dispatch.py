"""Single name → callable map for the agent loop's tool dispatcher.

Imports the actual implementations from their natural homes:
  - mcp_tools: repo_*, get_jenkins_job_config, list_runbooks, read_runbook
  - jira     : jira_get_ticket, jira_search
  - github   : github_get_commit, github_find_pr_for_commit,
               github_read_file, github_recent_commits

Phase 5 will extend this with aws_* tools from aws_tools.py.
Phase 6 will add code_review from claude_review.py (will likely be
renamed since we use OpenAI gpt-4o-mini, not Anthropic).

agent.py's dispatcher reads TOOL_DISPATCH to resolve LLM-requested
tool names → Python callables. Each value is sync or async; the
dispatcher awaits coroutines.
"""
import os

from . import aws_tools, github, jenkins as jenkins_api, jira, mcp_tools


async def _jenkins_node_info(node_name: str) -> str:
    """Adapter — calls jenkins.get_node_info using the env-resolved
    base URL + Basic auth. Returns JSON string of node state so the
    LLM can use the result verbatim in evidence + suggested_commands.
    """
    base = os.environ.get("JENKINS_RCA_JENKINS_URL", "").rstrip("/")
    user = os.environ.get("JENKINS_RCA_JENKINS_USER", "")
    token = os.environ.get("JENKINS_RCA_JENKINS_TOKEN", "")
    if not (base and user and token):
        return ("jenkins_node_info unavailable: JENKINS_RCA_JENKINS_URL / "
                "JENKINS_RCA_JENKINS_USER / JENKINS_RCA_JENKINS_TOKEN missing")
    try:
        info = await jenkins_api.get_node_info(node_name, base, (user, token))
        import json as _json
        return _json.dumps(info, default=str)
    except Exception as e:
        return f"jenkins_node_info error: {e}"


# Jenkins MCP plugin client — wraps `mcp-server` plugin
# (https://plugins.jenkins.io/mcp-server/) for capabilities not in the
# REST helpers. Imported lazily — agent loop still starts when the
# plugin isn't installed or env vars are missing.
try:
    from . import jenkins_mcp as _jenkins_mcp
except Exception:
    _jenkins_mcp = None


def _jmcp_unavailable() -> str:
    return ("jenkins_mcp unavailable: import failed OR "
            "JENKINS_RCA_JENKINS_URL / JENKINS_RCA_JENKINS_USER / "
            "JENKINS_RCA_JENKINS_TOKEN env vars missing. Use the REST-based "
            "tools instead.")


async def _jmcp_search_log(job: str, build: int, regex: str,
                           lines_after: int = 0) -> str:
    if _jenkins_mcp is None:
        return _jmcp_unavailable()
    return await _jenkins_mcp.search_build_log(job, build, regex, lines_after)


async def _jmcp_get_test_results(job: str, build: int) -> str:
    if _jenkins_mcp is None:
        return _jmcp_unavailable()
    return await _jenkins_mcp.get_test_results(job, build)


async def _jmcp_get_changesets(job: str, build: int) -> str:
    if _jenkins_mcp is None:
        return _jmcp_unavailable()
    return await _jenkins_mcp.get_changesets(job, build)


async def _jmcp_find_jobs_with_scm(scm_url: str) -> str:
    if _jenkins_mcp is None:
        return _jmcp_unavailable()
    return await _jenkins_mcp.find_jobs_with_scm(scm_url)

# rag is imported lazily inside the dispatch wrapper so the agent loop
# can still start when Postgres / pgvector aren't installed. R1 is
# dormant infra; this lets non-RAG environments run unchanged.
try:
    from . import rag as _rag_mod
except Exception:
    _rag_mod = None


def _rag_search_wrapper(query: str, k: int = 5,
                        source_types: list[str] | None = None,
                        error_class: str | None = None) -> str:
    """Adapter — returns a formatted string of top-k hits so the LLM
    can read it back cleanly. Falls back to a clear error string when
    rag.py is unimportable or PG is down."""
    if _rag_mod is None:
        return ("rag_search unavailable: psycopg/pgvector not installed "
                "or rag module failed to import. Use repo_search / "
                "list_docs as fallback.")
    try:
        hits = _rag_mod.search(
            query, k=int(k or 5),
            source_types=source_types, error_class=error_class,
        )
    except Exception as e:
        return f"rag_search error: {e}"
    if not hits:
        return "rag_search: no matches"
    lines = []
    for h in hits:
        meta = h.get("meta") or {}
        eclass = meta.get("error_class") or ""
        lines.append(
            f"[{h['score']:.3f}] {h['source_type']}/{h['source_id']}"
            f"{(' (class=' + eclass + ')') if eclass else ''}\n"
            f"  {h['chunk_text'][:600]}…"
        )
    return "\n\n".join(lines)


TOOL_DISPATCH: dict[str, callable] = {
    # ── runbook (error-class drill plans) ──
    "list_runbooks":               mcp_tools.list_runbooks,
    "read_runbook":                mcp_tools.read_runbook,

    # ── job_flow (per-pipeline-family orientation docs) ──
    "list_job_flows":              mcp_tools.list_job_flows,
    "read_job_flow":               mcp_tools.read_job_flow,

    # ── org docs (broader docops/*.md beyond runbooks + job_flows) ──
    "list_docs":                   mcp_tools.list_docs,
    "read_doc":                    mcp_tools.read_doc,

    # ── local repos (jenkins_pipeline / InfraComposer) ──
    "repo_read_file":              mcp_tools.repo_read_file,
    "repo_list_dir":               mcp_tools.repo_list_dir,
    "repo_search":                 mcp_tools.repo_search,
    "repo_find_function":          mcp_tools.repo_find_function,
    "repo_recent_commits":         mcp_tools.repo_recent_commits,

    # ── Jenkins API ──
    # NOTE: existing get_jenkins_job_config lives in main.py / jenkins.py.
    # Phase 3 will wire this entry; left as None placeholder if not yet
    # importable from mcp_tools.
    # "get_jenkins_job_config":    will be wired in Phase 3.

    # ── Jira (live API, shares jira.py creds) ──
    "jira_get_ticket":             jira.fetch_ticket,
    "jira_search":                 jira.search,

    # ── GitHub (live API, shares github.py creds: JENKINS_RCA_GITHUB_PAT) ──
    "github_get_commit":           github.fetch_commit,
    "github_find_pr_for_commit":   github.find_pr_for_commit,
    "github_read_file":            github.read_file,
    "github_recent_commits":       github.recent_commits,

    # ── AWS cross-account (Option A — single generic describe) ──
    # Replaces the four narrow tools (describe_target_health,
    # _target_group, _instance, _listener_rule). Same coverage, less
    # spec spam, auto-extends to RDS / Lambda / Logs / etc. without
    # new tool definitions.
    # SSM SendCommand is intentionally NOT exposed — Option C decision:
    # RCA never logs into instances; operator uses `bbctl shell <id>`
    # themselves when service-side detail is needed.
    "aws_describe":                 aws_tools.describe,

    # ── RAG semantic search (R2) ──
    # Postgres + pgvector. Wrapper imports rag.py lazily and degrades
    # gracefully when PG is offline so the agent loop still works on
    # non-RAG hosts. See bbctl/docs/rca/RAGflow.md for design.
    "rag_search":                   _rag_search_wrapper,

    # ── Jenkins MCP plugin (https://plugins.jenkins.io/mcp-server/) ──
    # Server-side log grep, JUnit results, change sets, cross-pipeline
    # scm lookup. Auth reuses existing JENKINS_RCA_JENKINS_USER + _TOKEN.
    # Each wrapper degrades to a clear "unavailable" string when env
    # vars are missing or plugin isn't installed.
    "jenkins_mcp_search_log":        _jmcp_search_log,
    "jenkins_mcp_get_test_results":  _jmcp_get_test_results,
    "jenkins_mcp_get_changesets":    _jmcp_get_changesets,
    "jenkins_mcp_find_jobs_with_scm": _jmcp_find_jobs_with_scm,

    # ── Jenkins node → EC2 instance-ID resolver (Phase 7) ──
    # Kills the `<slave-instance-id>` placeholder pattern in
    # jenkins_agent_offline RCAs. LLM calls this when log mentions
    # a slave name; gets back instance_id + online state.
    "jenkins_node_info":            _jenkins_node_info,

    # ── Sanity (Phase 6) ──
    # "code_review":                  claude_review.code_review,
}
