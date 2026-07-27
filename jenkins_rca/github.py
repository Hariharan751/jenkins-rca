"""GitHub commit metadata fetch for SCM/compliance RCA.

For commit-SHA hashes mentioned in the log, query GitHub REST API to get
author / date / message / files-changed summary. Helps RCA explain WHY two
commits differ (e.g. signed-off vs resolved).

Auth: GITHUB_PAT loaded from env (Secrets Manager → JENKINS_RCA_GITHUB_PAT).
Cached 24h via existing tool_cache.

We don't know the service's actual git repo in general, so we accept a hint
(`service` name) and try common org repo names. If repo unknown, fetches
commits using the bare SHA-only endpoint when possible (org-wide search).
"""
import os
import re
import sys
import httpx
from . import cache
from . import mcp_tools


def _log(msg: str) -> None:
    print(f"[github] {msg}", file=sys.stderr, flush=True)


SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")
SHORT_SHA_RE = re.compile(r"\b([0-9a-f]{7,12})\b")

GH_PAT = os.environ.get("JENKINS_RCA_GITHUB_PAT", "")
GH_ORG = os.environ.get("JENKINS_RCA_GITHUB_ORG", "BLACKBUCK-LABS")
MAX_COMMITS = 4   # cap how many commits we fetch per call


def _extract_shas(text: str) -> list[str]:
    """Return unique 40-char SHAs in encounter order, capped."""
    seen = []
    for m in SHA_RE.finditer(text or ""):
        s = m.group(1)
        if s not in seen:
            seen.append(s)
        if len(seen) >= MAX_COMMITS:
            break
    return seen


def _candidate_repos(service: str) -> list[str]:
    """Guess likely repo names for a service. First match wins on GitHub.

    Priority:
      1. Explicit git_repo / repo field in config.json (from service.lookup)
      2. Service name as-is
      3. Underscore ↔ hyphen variants
    """
    cands: list[str] = []

    # 1. Authoritative from service config
    try:
        svc = mcp_tools.service_lookup(service) or {}
        for key in ("git_repo", "github_repo", "repo", "repo_name", "service_repo"):
            v = svc.get(key) if isinstance(svc, dict) else None
            if isinstance(v, str) and v:
                # Strip org prefix if present (e.g. "BLACKBUCK-LABS/foo" -> "foo")
                cands.append(v.split("/")[-1])
    except Exception:
        pass

    # 2. Service name as-is
    s = service.strip()
    cands.append(s)

    # 3. Common BlackBuck transforms
    if "_" in s:
        cands.append(s.replace("_", "-"))
    if "-" in s:
        cands.append(s.replace("-", "_"))

    return list(dict.fromkeys(c for c in cands if c))


async def fetch_commit(repo: str, sha: str) -> dict | None:
    """Fetch one commit from GitHub. Returns slim dict or None on 404/error."""
    cached = cache.get_tool_cache("gh_commit", {"repo": repo, "sha": sha})
    if cached is not None:
        return cached if cached else None

    if not GH_PAT:
        return None

    url = f"https://api.github.com/repos/{GH_ORG}/{repo}/commits/{sha}"
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 404:
                cache.set_tool_cache("gh_commit", {"repo": repo, "sha": sha}, {})
                return None
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None

    commit = data.get("commit") or {}
    author = commit.get("author") or {}
    files = data.get("files") or []
    slim = {
        "repo": repo,
        "sha": data.get("sha", sha)[:12],
        "author": author.get("name"),
        "email": author.get("email"),
        "date": author.get("date"),
        "message": (commit.get("message") or "")[:300],
        "files_changed": [
            {"path": f.get("filename"), "status": f.get("status"),
             "additions": f.get("additions"), "deletions": f.get("deletions")}
            for f in files[:10]
        ],
        "files_total": len(files),
    }
    cache.set_tool_cache("gh_commit", {"repo": repo, "sha": sha}, slim)
    return slim


async def _resolve_repo_for_sha(sha: str, service: str) -> tuple[str, dict] | None:
    """Try candidate repos for this service until one returns a hit."""
    for repo in _candidate_repos(service):
        result = await fetch_commit(repo, sha)
        if result:
            return repo, result
    return None


async def fetch_commits_from_log(log_window: str, service: str) -> list[dict]:
    """Extract SHAs from log, fetch commit details. Returns list of slim dicts."""
    if not GH_PAT:
        _log("GH_PAT not set — skipping commit fetch")
        return []
    shas = _extract_shas(log_window)
    if not shas:
        _log(f"no 40-char SHAs found in log for service={service}")
        return []

    repos = _candidate_repos(service)
    _log(f"service={service} shas={shas} candidate_repos={repos}")

    results = []
    for sha in shas:
        hit = await _resolve_repo_for_sha(sha, service)
        if hit:
            repo, commit = hit
            _log(f"sha={sha[:8]} -> {repo} ({commit.get('author')})")
            results.append(commit)
        else:
            _log(f"sha={sha[:8]} NOT FOUND in any candidate repo")
    return results


# ─── Agent-mode tool functions (Phase 2 additions) ─────────────────────


async def find_pr_for_commit(repo: str, sha: str) -> dict | None:
    """Find the merged PR that contains a commit. Returns the first
    merged PR (or first listed if none merged), or None.

    Used by the agent tool `github_find_pr_for_commit` — needed for
    compliance Mode 5 (PR title must contain Jira ticket key).
    """
    if not GH_PAT:
        return None
    cache_key = {"repo": repo, "sha": sha}
    cached = cache.get_tool_cache("gh_pr_for_commit", cache_key)
    if cached is not None:
        return cached if cached else None

    url = f"https://api.github.com/repos/{GH_ORG}/{repo}/commits/{sha}/pulls"
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 404:
                cache.set_tool_cache("gh_pr_for_commit", cache_key, {})
                return None
            r.raise_for_status()
            prs = r.json() or []
    except Exception:
        return None
    chosen = next((p for p in prs if p.get("merged_at")), prs[0] if prs else None)
    if not chosen:
        cache.set_tool_cache("gh_pr_for_commit", cache_key, {})
        return None
    slim = {
        "number": chosen.get("number"),
        "title": chosen.get("title"),
        "merged_at": chosen.get("merged_at"),
        "author": (chosen.get("user") or {}).get("login"),
        "url": chosen.get("html_url"),
        "state": chosen.get("state"),
    }
    cache.set_tool_cache("gh_pr_for_commit", cache_key, slim)
    return slim


async def read_file(repo: str, path: str, ref: str,
                    start: int = 1, end: int = 100) -> str:
    """Read a line slice from a GitHub repo at a specific ref via the
    Contents API. Returns numbered lines string.

    Used for service repos NOT cloned locally (alchemist, demand, etc.).
    For jenkins_pipeline / InfraComposer use mcp_tools.repo_read_file
    instead — local + fresher.
    """
    import base64
    if not GH_PAT:
        return "error: GH_PAT not configured"
    cache_key = {"repo": repo, "path": path, "ref": ref,
                 "start": start, "end": end}
    cached = cache.get_tool_cache("gh_read_file", cache_key)
    if cached is not None:
        return cached

    url = f"https://api.github.com/repos/{GH_ORG}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers, params={"ref": ref})
            if r.status_code == 404:
                return f"error: file {repo}/{path}@{ref} not found"
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return f"error: {e}"

    if isinstance(data, list):
        names = ", ".join(item.get("name", "?") for item in data[:20])
        return f"error: path is a directory. children: {names}"
    enc = data.get("encoding") or ""
    if enc != "base64":
        return f"error: unexpected encoding '{enc}'"
    try:
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception as e:
        return f"error: base64 decode failed: {e}"

    lines = content.splitlines()
    s = max(1, int(start)) - 1
    e = min(len(lines), int(end))
    sliced = lines[s:e]
    out = "\n".join(f"{i + s + 1}: {ln}" for i, ln in enumerate(sliced))
    cache.set_tool_cache("gh_read_file", cache_key, out)
    return out


async def recent_commits(repo: str, branch: str = "main",
                          n: int = 10) -> list[dict]:
    """Recent commits on a GitHub repo branch. Returns slim dicts."""
    if not GH_PAT:
        return [{"error": "GH_PAT not configured"}]
    cache_key = {"repo": repo, "branch": branch, "n": n}
    cached = cache.get_tool_cache("gh_recent_commits", cache_key)
    if cached is not None:
        return cached

    url = f"https://api.github.com/repos/{GH_ORG}/{repo}/commits"
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params = {"sha": branch, "per_page": min(int(n), 30)}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=headers, params=params)
            if r.status_code == 404:
                return [{"error": f"repo or branch not found: {repo}@{branch}"}]
            r.raise_for_status()
            data = r.json() or []
    except Exception as e:
        return [{"error": f"gh recent commits failed: {e}"}]

    out = []
    for c in data[:n]:
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        out.append({
            "sha": (c.get("sha") or "")[:12],
            "author": author.get("name"),
            "date": author.get("date"),
            "message": (commit.get("message") or "").splitlines()[0][:200],
        })
    cache.set_tool_cache("gh_recent_commits", cache_key, out)
    return out
