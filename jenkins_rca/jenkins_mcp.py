"""Jenkins MCP Server plugin client.

Wraps the `mcp-server` Jenkins plugin (https://plugins.jenkins.io/mcp-server/)
which exposes Jenkins data via the MCP protocol over HTTP/SSE/stateless
transports. We use the **streamable HTTP** transport at
`<JENKINS_URL>/mcp-server/mcp` (recommended for reliability per plugin
docs).

Auth: Basic Authentication using the existing `jenkins_user` +
`jenkins_token` already in AWS Secrets Manager + exported to env vars
`JENKINS_RCA_JENKINS_USER` and `JENKINS_RCA_JENKINS_TOKEN`. No new secret required —
the plugin uses the same per-user API token as the REST API.

Wire-up: tool schemas in `tool_schemas.py`, dispatchers in
`agent_dispatch.py`. Surfaced tools (cherry-picked from the plugin's
catalog — the ones that give the agent capabilities NOT already
covered by `jenkins_rca/jenkins.py` REST helpers):

- `jenkins_mcp_search_log(job, build, regex, lines_after=0)` — server-
  side log grep. Cheaper than pulling the full log into the agent's
  context for one-off pattern checks.
- `jenkins_mcp_get_test_results(job, build)` — JUnit results
  (failing test names + stack traces). New capability — the REST
  helper doesn't expose this.
- `jenkins_mcp_get_changesets(job, build)` — SCM changes for the
  build (commits since prior build). Complements the GitHub client.
- `jenkins_mcp_find_jobs_with_scm(scm_url)` — find every Jenkins job
  whose pipeline source is the given repo. Useful for cross-pipeline
  impact analysis when a shared helper changes.

MCP protocol notes (JSON-RPC 2.0):
- Stateless transport (`/mcp-server/stateless`) does NOT require an
  initialize/initialized handshake — each POST is independent. We
  use that for one-shot tool calls.
- Each request: {jsonrpc, id, method: "tools/call",
  params: {name, arguments}}. Response: {jsonrpc, id, result:
  {content: [{type:"text", text:"..."}], isError}}.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os

import httpx


_DEFAULT_TIMEOUT = 30.0
_REQ_ID = 0


def _config() -> tuple[str, str]:
    """Return (mcp_endpoint_url, basic_auth_header_value).

    Reuses JENKINS_RCA_JENKINS_URL + JENKINS_RCA_JENKINS_USER + JENKINS_RCA_JENKINS_TOKEN.
    Returns ('', '') if any required env var is missing — callers
    should treat as "tool unavailable" and return a graceful error.
    """
    base = os.environ.get("JENKINS_RCA_JENKINS_URL", "").rstrip("/")
    user = os.environ.get("JENKINS_RCA_JENKINS_USER", "")
    token = os.environ.get("JENKINS_RCA_JENKINS_TOKEN", "")
    if not (base and user and token):
        return "", ""
    # Stateless transport — no session init needed.
    url = f"{base}/mcp-server/stateless"
    creds = base64.b64encode(f"{user}:{token}".encode()).decode()
    return url, f"Basic {creds}"


async def _call_tool(name: str, arguments: dict, *,
                     timeout: float = _DEFAULT_TIMEOUT) -> str:
    """JSON-RPC `tools/call` against the MCP plugin. Returns the text
    payload from `result.content[0].text` or a JSON-encoded error
    string starting with `mcp error:` so the LLM sees the failure
    explicitly.
    """
    global _REQ_ID
    url, auth = _config()
    if not url:
        return ("mcp error: jenkins MCP not configured — set "
                "JENKINS_RCA_JENKINS_URL + JENKINS_RCA_JENKINS_USER + "
                "JENKINS_RCA_JENKINS_TOKEN")
    _REQ_ID += 1
    body = {
        "jsonrpc": "2.0",
        "id": _REQ_ID,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            ct = (r.headers.get("content-type") or "").lower()
            if "text/event-stream" in ct:
                # Some plugin versions return the result as a single
                # SSE event even on the stateless endpoint. Parse the
                # first `data:` line.
                for line in r.text.splitlines():
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        if payload:
                            data = json.loads(payload)
                            break
                else:
                    return f"mcp error: empty SSE response: {r.text[:300]}"
            else:
                data = r.json()
    except httpx.HTTPStatusError as e:
        return (f"mcp error: HTTP {e.response.status_code} from "
                f"{url}: {e.response.text[:300]}")
    except httpx.HTTPError as e:
        return f"mcp error: network: {e}"
    except json.JSONDecodeError as e:
        return f"mcp error: invalid JSON response: {e}"
    if "error" in data:
        return f"mcp error: {data['error']}"
    result = data.get("result") or {}
    if result.get("isError"):
        return f"mcp error (tool): {json.dumps(result.get('content'))}"
    content = result.get("content") or []
    # MCP returns content as an array; concatenate text parts.
    parts = [
        c.get("text", "") for c in content
        if isinstance(c, dict) and c.get("type") == "text"
    ]
    if not parts:
        return json.dumps(result)
    return "\n".join(parts)


# ── Public wrappers (called from agent_dispatch) ─────────────────────

async def search_build_log(job: str, build: int | str, regex: str,
                           lines_after: int = 0) -> str:
    """Server-side grep over a build's console log. Returns the
    matched lines (with optional N lines after each match)."""
    return await _call_tool("searchBuildLog", {
        "jobName": job,
        "buildNumber": int(build),
        "pattern": regex,
        "linesAfter": lines_after,
    })


async def get_test_results(job: str, build: int | str) -> str:
    """JUnit test results for the build — failing test names + stack
    traces. Returns plugin's JSON serialization."""
    return await _call_tool("getTestResults", {
        "jobName": job,
        "buildNumber": int(build),
    })


async def get_changesets(job: str, build: int | str) -> str:
    """SCM change sets for the build (commits since prior build).
    Complements `github_get_commit` — Jenkins-side view of the same
    data, no PAT needed."""
    return await _call_tool("getBuildChangeSets", {
        "jobName": job,
        "buildNumber": int(build),
    })


async def find_jobs_with_scm(scm_url: str) -> str:
    """Find every Jenkins job whose pipeline source is the given
    SCM URL. Useful for cross-pipeline impact analysis when a shared
    helper in jenkins_pipeline changes."""
    return await _call_tool("findJobsWithScmUrl", {"scmUrl": scm_url})


async def who_am_i() -> str:
    """Smoke test — confirms auth + plugin reachability."""
    return await _call_tool("whoAmI", {})


# ── CLI helper for debugging on EC2 ───────────────────────────────────

def _cli(argv: list[str]) -> int:
    """Usage: python -m jenkins_rca.jenkins_mcp <whoami|search|tests|cs|find> [args]

    Examples:
      python -m jenkins_rca.jenkins_mcp whoami
      python -m jenkins_rca.jenkins_mcp search 'Stagger Prod Plus One' 5225 'Gradle'
      python -m jenkins_rca.jenkins_mcp tests   'Stagger Prod Plus One' 5225
      python -m jenkins_rca.jenkins_mcp cs      'Stagger Prod Plus One' 5225
      python -m jenkins_rca.jenkins_mcp find    'git@github.com:BLACKBUCK-LABS/jenkins_pipeline.git'
    """
    if not argv or argv[0] in ("-h", "--help"):
        print(_cli.__doc__)
        return 0
    cmd = argv[0]
    rest = argv[1:]
    try:
        if cmd == "whoami":
            out = asyncio.run(who_am_i())
        elif cmd == "search":
            out = asyncio.run(
                search_build_log(rest[0], rest[1], rest[2],
                                 int(rest[3]) if len(rest) > 3 else 0)
            )
        elif cmd == "tests":
            out = asyncio.run(get_test_results(rest[0], rest[1]))
        elif cmd == "cs":
            out = asyncio.run(get_changesets(rest[0], rest[1]))
        elif cmd == "find":
            out = asyncio.run(find_jobs_with_scm(rest[0]))
        else:
            print(f"unknown command: {cmd}")
            print(_cli.__doc__)
            return 2
    except (IndexError, ValueError) as e:
        print(f"usage error: {e}")
        print(_cli.__doc__)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
