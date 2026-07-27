"""Post-LLM evidence verification.

LLMs occasionally invent file:line citations that don't exist in the repo.
This module checks each evidence entry against the synced repos on disk and
annotates with `verified: true|false`. Does not fail the RCA — just adds
ground-truth signal for the operator.
"""
import os
from pathlib import Path
import re

REPOS_DIR = Path(os.environ.get("JENKINS_RCA_REPOS_DIR", str(Path(__file__).resolve().parent.parent / "repos")))

# Sources that are tool/pipeline outputs (not files on disk) — auto-verified
_TOOL_SOURCES = {
    "jenkins_log", "build_meta", "console",
    "jira.tickets", "jira",
    "service.lookup", "service",
    "source.trace",
    "docs", "docops",
}

# Match `path/to/file.ext` or `path/to/file.ext:NN`. Requires at least one
# slash to avoid matching tool-source dotted names like "jira.tickets".
_PATH_RE = re.compile(r"^([A-Za-z0-9_./-]+/[A-Za-z0-9_.-]+\.[A-Za-z]+)(?::(\d+))?$")


def _resolve(source: str) -> tuple[Path | None, int | None]:
    """Return (absolute_path, line) if source looks like a repo path, else (None, None)."""
    m = _PATH_RE.match(source.strip())
    if not m:
        return None, None
    rel, line = m.group(1), m.group(2)
    candidate = REPOS_DIR / rel
    return candidate, int(line) if line else None


def verify(evidence: list[dict]) -> list[dict]:
    """Mark each evidence entry with `verified` boolean.

    - jenkins_log / build_meta sources: always verified=true (came from us)
    - Anything matching a repo path: check file exists. If line given, also
      check that line number is within file bounds.
    """
    for ev in evidence:
        source = (ev.get("source") or "").strip()
        # Tool/log sources: trust them (came from pre-fetch we did)
        if source in _TOOL_SOURCES or source == "":
            ev["verified"] = True
            continue
        # Check for tool-source prefix (e.g. "jira.tickets[FMSCAT-1234]")
        if any(source.startswith(s) for s in _TOOL_SOURCES):
            ev["verified"] = True
            continue

        path, line = _resolve(source)
        if path is None:
            ev["verified"] = None
            continue

        if not path.exists():
            ev["verified"] = False
            continue

        if line is not None:
            try:
                total = sum(1 for _ in path.open())
                ev["verified"] = 1 <= line <= total
            except Exception:
                ev["verified"] = False
        else:
            ev["verified"] = True
    return evidence
