"""Deterministic failure-point extractor for Jenkins logs.

Scans the raw build log BACKWARDS from the end to find the actual exit point
(the line where the build's exit code != 0 originated), then extracts:

- step_name        : Jenkins parallel-branch label if present
- exit_code        : the captured non-zero exit code
- failing_command  : the last `+ <cmd>` shell echo before the error line
- line_no          : 1-indexed line number in the raw log
- context          : ~60 lines around the failure point for evidence

The goal is to give downstream classifier + LLM a deterministic, log-grounded
anchor so they don't have to guess. This complements `window.extract_window`
(which collects sanitized log evidence) and `jenkins.get_stage_errors`
(which returns wfapi stage status).

Output schema (dict, all keys optional):
    {
        "step_name":       str | None,
        "exit_code":       int | None,
        "failing_command": str | None,
        "line_no":         int | None,
        "context":         str | None,
        "match_pattern":   str | None,   # which regex hit (for debug)
    }

Empty dict if no failure pattern matched (defensive — caller handles).
"""
from __future__ import annotations

import re

# Patterns ordered by specificity. First-match wins per line.
# All anchored to find the FAILURE LINE itself (not earlier retry-loop noise).
_FAILURE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Jenkins parallel-branch failure:
    #   "Error in Deploy_i-0163ff65637e03b49: script returned exit code 2"
    ("parallel_branch", re.compile(
        r"Error in (?P<step>\S+): script returned exit code (?P<code>\d+)"
    )),
    # Generic Jenkins sh step failure:
    #   "script returned exit code 1"
    ("sh_exit_code", re.compile(
        r"script returned exit code (?P<code>\d+)"
    )),
    # Bash explicit exit:
    #   "+ exit 1"
    ("bash_exit", re.compile(
        r"^\+ exit (?P<code>\d+)\s*$"
    )),
    # Jenkins error step:
    #   "[Pipeline] error" followed by ERROR: ...
    ("pipeline_error", re.compile(
        r"^ERROR:\s+(?P<msg>.+)$"
    )),
    # Fatal markers some scripts emit:
    ("fatal", re.compile(
        r"^FATAL:\s+(?P<msg>.+)$"
    )),
]

# Echo of executed shell command. `+ <cmd>` — bash set -x style.
# Skip purely cosmetic `+ echo "..."` lines because they're not the failing
# command, just prints.
_SHELL_ECHO_RE = re.compile(r"^\+\s+(?P<cmd>.+)$")
_PURE_ECHO_RE = re.compile(r"^\+\s+echo\b")

# Skip lines that are obviously not the failing command (S3 upload progress,
# zip inflate, etc.) so we don't false-positive on retry-loop noise.
_NOISE_LINE_RE = re.compile(
    r"^Completed\s+[\d.]+\s+(KiB|MiB|GiB|bytes)|^upload:\s|^download:\s|"
    r"^\s+(inflating|creating|extracting):\s"
)


def _is_noise(line: str) -> bool:
    return bool(_NOISE_LINE_RE.match(line))


def _find_last_shell_command_before(lines: list[str], idx: int, max_lookback: int | None = None) -> str | None:
    """Walk upward from idx to find the most recent meaningful `+ <cmd>` echo.

    Jenkins parallel branch output is heavily interleaved; the actual failing
    command for `Error in <branch>: ...` may be hundreds-of-thousands of lines
    earlier (other branches' output sits in between). When max_lookback is
    None, scan the entire prefix.

    Two-tier preference:
      1. LIKELY_FAILING_PATTERNS — commands that commonly produce non-zero
         exit (s3 cp, ssh, systemctl, gradle/mvn, deploy scripts, ssm).
      2. Any non-noise `+ cmd` echo as fallback.

    Skips:
      - S3 upload progress lines / zip inflate / `Completed ...` chatter
      - pure `+ echo "..."` (those are output, not commands)
      - read-only AWS describe-* / get-* setup queries
      - `+ chmod` setup commands
    """
    LIKELY_FAILING_PATTERNS = (
        "aws s3 cp ", "aws s3 sync ", "aws ssm send-command",
        "scp ", "ssh ", "ssm-cli", "systemctl", "service ",
        "./gradlew", "./mvnw", "mvn ", "gradle ",
        "curl ", "wget ", "docker ", "make ",
        "deploy_code.sh", "app_restart.sh", "unhealthy.sh", "healthy.sh",
        "post_deploy_cleanup.sh", "prepare_server.sh",
        "python3 ", "python ", "bash ", "sh ",
    )
    SETUP_NOISE = (
        "+ chmod ", "+ aws sts ", "+ aws elbv2 describe-",
        "+ aws ec2 describe-", "+ jq ", "+ echo ", "+ aws ssm describe-",
        "+ true", "+ false", "+ test ", "+ [ ", "+ attempt=",
        "+ sleep ", "+ aws elbv2 modify-",
    )

    start = 0 if max_lookback is None else max(0, idx - max_lookback)
    fallback = None

    for j in range(idx - 1, start - 1, -1):
        ln = lines[j]
        if _is_noise(ln):
            continue
        if _PURE_ECHO_RE.match(ln):
            continue
        m = _SHELL_ECHO_RE.match(ln)
        if not m:
            continue
        cmd = m.group("cmd").strip()
        # Skip clearly read-only setup queries
        if any(ln.startswith(p) for p in SETUP_NOISE):
            continue
        # Prefer the first likely-failing pattern we see walking back
        if any(p in cmd for p in LIKELY_FAILING_PATTERNS):
            return cmd
        if fallback is None:
            fallback = cmd
    return fallback


def _extract_context(lines: list[str], idx: int, before: int = 50, after: int = 5) -> str:
    """Slice ±N lines around idx, dropping S3 upload progress noise."""
    start = max(0, idx - before)
    end = min(len(lines), idx + after + 1)
    out = []
    for j in range(start, end):
        if _is_noise(lines[j]):
            continue
        out.append(lines[j])
    return "\n".join(out)


def _build_result(lines: list[str], i: int, pat_name: str, m: re.Match) -> dict:
    groups = m.groupdict()
    failing_cmd = _find_last_shell_command_before(lines, i)
    return {
        "step_name":       groups.get("step"),
        "exit_code":       int(groups["code"]) if groups.get("code") else None,
        "failing_command": failing_cmd,
        "line_no":         i + 1,
        "context":         _extract_context(lines, i),
        "match_pattern":   pat_name,
        "error_message":   groups.get("msg"),
    }


# Specific patterns take precedence over generic pipeline-level errors.
# A generic "ERROR: parallel branches encountered an error" line ALWAYS
# follows the actual per-branch failure, so without this priority split we'd
# pick the late generic line and miss the real exit point.
_SPECIFIC_PATTERNS = [(n, p) for (n, p) in _FAILURE_PATTERNS
                       if n in ("parallel_branch", "sh_exit_code", "bash_exit")]
_GENERIC_PATTERNS = [(n, p) for (n, p) in _FAILURE_PATTERNS
                      if n in ("pipeline_error", "fatal")]


def locate_failure(raw_log: str) -> dict:
    """Walk log backwards to find the first failure marker. Return structured info.

    Two-pass priority:
      Pass 1: scan backwards for SPECIFIC patterns (parallel-branch, sh exit, bash exit).
              These pinpoint the actual exit point.
      Pass 2: only if no specific match, fall back to GENERIC pipeline-level
              ERROR / FATAL markers (which are usually after-the-fact summaries).

    Returns {} when no failure pattern matches — caller treats as 'unknown
    failure point' and falls back to existing classifier heuristics.
    """
    if not raw_log:
        return {}

    lines = raw_log.splitlines()

    # Pass 1: specific patterns
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if _is_noise(line):
            continue
        for pat_name, pat in _SPECIFIC_PATTERNS:
            m = pat.search(line)
            if m:
                return _build_result(lines, i, pat_name, m)

    # Pass 2: generic fallback
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if _is_noise(line):
            continue
        for pat_name, pat in _GENERIC_PATTERNS:
            m = pat.search(line)
            if m:
                return _build_result(lines, i, pat_name, m)

    return {}


def locate_failing_stage(raw_log: str) -> str | None:
    """Return the Jenkins stage that ACTUALLY failed — anchored on the exit point.

    Why this beats window.extract_failed_stage:
      extract_failed_stage scans stages top-to-bottom and returns the FIRST
      stage whose body holds an in-stage error marker. In multi-phase pipelines
      (Stagger Prod Plus One: a Prod+1 phase THEN a Prod phase) an earlier stage
      often has a transient `script returned exit code N` inside a retry loop
      that recovered — so it mis-attributes to e.g. `Build` even though the
      pipeline aborted much later in `Infra` (prod phase).

    This function instead:
      1. Uses locate_failure() to find the DETERMINISTIC exit point line
         (walks the log backwards for the real failure marker).
      2. Walks UP from that line to the nearest `[Pipeline] { (<stage>)` marker,
         skipping post-action wrappers and skip-cascade stages.

    The result is the stage that encloses the actual abort, not the first stage
    that merely logged an error string. Returns None when no failure line or no
    enclosing stage can be found (caller falls back to other strategies).
    """
    if not raw_log:
        return None
    failure = locate_failure(raw_log)
    if not failure or not failure.get("line_no"):
        return None

    # Reuse window's stage/skip/ignore vocabulary so both stay in lockstep.
    from .window import _STAGE_RE, _IGNORED_STAGES, _STAGE_SKIPPED_RE

    lines = raw_log.splitlines()
    idx = min(failure["line_no"] - 1, len(lines) - 1)

    skipped: set[str] = set()
    for ln in lines:
        sm = _STAGE_SKIPPED_RE.search(ln)
        if sm:
            skipped.add(sm.group(1))

    for j in range(idx, -1, -1):
        m = _STAGE_RE.search(lines[j])
        if not m:
            continue
        name = m.group(1)
        if name.lower() in _IGNORED_STAGES or name in skipped:
            continue
        return name
    return None


def summarize_for_llm(failure: dict, max_cmd_len: int = 500) -> str:
    """Render a human/LLM-readable block of verified facts.

    Used to prepend deterministic ground truth to the LLM prompt so it cannot
    hallucinate failure-point details.
    """
    if not failure:
        return ""
    cmd = failure.get("failing_command") or "(no shell command captured)"
    if len(cmd) > max_cmd_len:
        cmd = cmd[:max_cmd_len] + " ... [truncated]"
    parts = ["## VERIFIED FAILURE FACTS (deterministic — do not contradict)"]
    if failure.get("step_name"):
        parts.append(f"- step_name:       `{failure['step_name']}`")
    if failure.get("exit_code") is not None:
        parts.append(f"- exit_code:       {failure['exit_code']}")
    if failure.get("line_no"):
        parts.append(f"- log_line_no:     {failure['line_no']}")
    parts.append(f"- match_pattern:   {failure.get('match_pattern','?')}")
    if failure.get("error_message"):
        parts.append(f"- error_message:   {failure['error_message']}")
    parts.append("")
    parts.append("- failing_command (last `+ <cmd>` echo before error):")
    parts.append("```")
    parts.append(cmd)
    parts.append("```")
    parts.append("")
    parts.append(
        "Your `root_cause` MUST explain why THIS command produced THIS exit "
        "code. Do NOT invent infra details (ports, health-check paths, ALB "
        "configs, etc.) that do not appear in the log. If a detail is "
        "unknown, write '(unknown — needs investigation)'."
    )
    return "\n".join(parts)
