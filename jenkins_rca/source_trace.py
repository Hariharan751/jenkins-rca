"""Trace error messages back to their origin in pipeline source.

For each distinctive error/exception line in the sanitized log window, ripgrep
the jenkins_pipeline (groovy/java) and InfraComposer (terraform) repos to find
the source file that emits that error. Result fed into LLM tool context so the
LLM cites the real file:line instead of guessing.
"""
import os
import re
import subprocess
from pathlib import Path


REPOS_DIR = Path(os.environ.get("JENKINS_RCA_REPOS_DIR", str(Path(__file__).resolve().parent.parent / "repos")))
SEARCH_DIRS = ["jenkins_pipeline", "InfraComposer"]
MAX_QUERIES = 4
# Wider sweep when the classifier returned `unknown` — let the LLM see more
# candidate origin points so it can self-classify from source evidence.
MAX_QUERIES_DEEP = 10
MAX_HITS_DEEP = 10

# Lines we trace: "ERROR: ...", "FATAL: ...", "Exception: ...", Jenkins error('msg')
_ERROR_LINE_RE = re.compile(
    r"^(?:ERROR|FATAL|FAILURE|FAIL)\s*[:!]\s*(.+?)$"
    r"|^(?:Exception|Caused by):\s*(.+?)$"
    r"|^.*?\b(error|fail|cannot|denied|not found|invalid)\b.*?:\s*(.+?)$",
    re.IGNORECASE,
)


def _extract_queries(log_window: str, max_queries: int = MAX_QUERIES) -> list[str]:
    """Pick distinctive substrings to grep for, capped at max_queries."""
    queries: list[str] = []
    for line in log_window.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _ERROR_LINE_RE.match(line)
        if not m:
            continue
        # Pick the most distinctive capture: prefer first non-empty group
        msg = next((g for g in m.groups() if g), None)
        if not msg:
            continue
        # Strip trailing punctuation, take first ~40 chars
        msg = msg.strip().strip("'\"")
        if len(msg) < 8:
            continue
        # Take a quotable substring (between quotes if present, else first 60 chars)
        q = msg[:60]
        # Strip dynamic bits (UUIDs, numbers in parens) that won't match source code
        q = re.sub(r"\([^)]*\)", "", q).strip()
        q = re.sub(r"\s+", " ", q)
        if q and q not in queries and len(q) >= 8:
            queries.append(q)
        if len(queries) >= max_queries:
            break
    return queries


def _rg(query: str, search_dir: Path, max_hits: int = 5) -> list[str]:
    """Run ripgrep, return matching lines `<relpath>:<line>: <snippet>`."""
    if not search_dir.exists():
        return []
    try:
        r = subprocess.run(
            [
                "rg", "--line-number", "--no-heading",
                "-m", str(max_hits),
                "--fixed-strings",     # treat query as literal, not regex
                query, str(search_dir),
            ],
            capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return []
    if r.returncode not in (0, 1):  # 1 = no matches
        return []
    lines = []
    for raw in r.stdout.splitlines():
        # rg output: /absolute/path:line:content
        try:
            path_part, lineno, content = raw.split(":", 2)
            rel = Path(path_part).relative_to(REPOS_DIR.parent if str(REPOS_DIR) in path_part else search_dir.parent)
            lines.append(f"{rel}:{lineno}: {content.strip()[:150]}")
        except Exception:
            lines.append(raw[:200])
    return lines


def trace(log_window: str, deep: bool = False) -> list[dict]:
    """Return list of {query, hits[]} for each distinctive error in log.

    `deep=True` runs a wider sweep — used when the classifier returned
    `unknown` so the LLM has more candidate origin points to self-classify
    from source evidence.
    """
    max_queries = MAX_QUERIES_DEEP if deep else MAX_QUERIES
    max_hits = MAX_HITS_DEEP if deep else 5
    out = []
    for q in _extract_queries(log_window, max_queries=max_queries):
        hits = []
        for sd in SEARCH_DIRS:
            hits.extend(_rg(q, REPOS_DIR / sd, max_hits=max_hits))
        out.append({"query": q, "hits": hits[: (16 if deep else 8)]})
    return out
