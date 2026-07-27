"""Per-RCA freshness pull for the local repo clones.

Hybrid model: keep local clones for fast ripgrep / file reads, but right before
each RCA call we run a shallow `git fetch && git reset --hard origin/<branch>`
so the LLM sees the latest commit. Cron at /etc/cron.d/jenkins-rca-sync stays as
a backstop (every 6h) in case this fast path is skipped.

Design notes:
  * 3-second timeout per repo — if GitHub is slow / offline, we silently fall
    back to whatever's on disk (best-effort).
  * In-process dedup: if we already fetched the same repo in the last 60s
    (e.g. concurrent webhooks for the same build retry), skip.
  * Runs as the ubuntu user via `chown`/`chmod u+w` self-heal so the agent
    can re-read the freshened files.
"""
import os
import subprocess
import time
from pathlib import Path


REPOS_DIR = Path(os.environ.get("JENKINS_RCA_REPOS_DIR", str(Path(__file__).resolve().parent.parent / "repos")))
FETCH_TIMEOUT_SEC = 3
DEDUP_WINDOW_SEC = 60

# (repo, branch) -> unix timestamp of last successful fetch
_last_fetch: dict[tuple[str, str], float] = {}


def ensure_fresh(repo: str, branch: str | None = None) -> dict:
    """Pull latest commits for `repo` on `branch`. Best-effort.

    Returns a dict describing what happened — useful for surfacing in the
    audit log / agent tool output. Never raises.

    Result shape:
      {"repo": "...", "branch": "...", "status": "fresh|cached|fallback|missing",
       "head": "<sha>", "elapsed_ms": 1234, "error": "..." (optional)}
    """
    repo_path = REPOS_DIR / repo
    if not (repo_path / ".git").exists():
        return {"repo": repo, "status": "missing", "head": None}

    branch = branch or _default_branch(repo_path)
    key = (repo, branch)
    now = time.monotonic()
    if (now - _last_fetch.get(key, 0)) < DEDUP_WINDOW_SEC:
        return {
            "repo": repo, "branch": branch, "status": "cached",
            "head": _head_sha(repo_path), "elapsed_ms": 0,
        }

    t0 = time.monotonic()
    try:
        # Self-heal perms (someone else's `chmod -R a-w` would block `git reset`)
        subprocess.run(
            ["chmod", "-R", "u+w", str(repo_path)],
            timeout=FETCH_TIMEOUT_SEC, capture_output=True, check=False,
        )
        # Shallow fetch — only the tip of the requested branch
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "--depth", "1", "--quiet",
             "origin", branch],
            timeout=FETCH_TIMEOUT_SEC, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "reset", "--hard", "--quiet",
             f"origin/{branch}"],
            timeout=FETCH_TIMEOUT_SEC, capture_output=True, check=True,
        )
        _last_fetch[key] = now
        return {
            "repo": repo, "branch": branch, "status": "fresh",
            "head": _head_sha(repo_path),
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {
            "repo": repo, "branch": branch, "status": "fallback",
            "head": _head_sha(repo_path),
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": "fetch timeout",
        }
    except subprocess.CalledProcessError as e:
        return {
            "repo": repo, "branch": branch, "status": "fallback",
            "head": _head_sha(repo_path),
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": (e.stderr.decode("utf-8", "replace")[:200] if e.stderr else "fetch failed"),
        }
    except Exception as e:
        return {
            "repo": repo, "branch": branch, "status": "fallback",
            "head": _head_sha(repo_path),
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": str(e)[:200],
        }


def ensure_fresh_many(repos: list[tuple[str, str | None]]) -> list[dict]:
    """Refresh several (repo, branch) pairs sequentially. Sequential keeps the
    operation predictable + bounded — total worst case = N × FETCH_TIMEOUT_SEC.
    """
    return [ensure_fresh(r, b) for r, b in repos]


def _head_sha(repo_path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--short", "HEAD"],
            timeout=2, capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _default_branch(repo_path: Path) -> str:
    """Resolve the default branch for `repo_path`.

    Priority:
      1. Hardcoded mapping for the two repos we care about (overridable via
         env). `jenkins_pipeline` tracks `master` — that's the canonical
         branch where landed fixes live (e.g. JiraDetails build-param
         fallback). The release branch lagged behind master and made the
         RCA agent read stale code; we track master now. Operators who
         need to diagnose a historical build against an older branch can
         override via env: JENKINS_RCA_JP_BRANCH=<branch>.
      2. `symbolic-ref refs/remotes/origin/HEAD` for any other repo (kept as
         a generic fallback for future additions).
      3. Literal "main" as last resort.
    """
    import os as _os
    name = repo_path.name
    if name == "jenkins_pipeline":
        return _os.environ.get("JENKINS_RCA_JP_BRANCH", "master")
    if name == "InfraComposer":
        return _os.environ.get("JENKINS_RCA_IC_BRANCH", "main")
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "symbolic-ref", "--short",
             "refs/remotes/origin/HEAD"],
            timeout=2, capture_output=True, text=True, check=True,
        )
        ref = out.stdout.strip()  # "origin/main" → "main"
        if "/" in ref:
            return ref.split("/", 1)[1]
    except Exception:
        pass
    return "main"
