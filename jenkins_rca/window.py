"""Build a sanitized, compressed log window for LLM consumption.

Strategy:
- Always scan the FULL log (post noise-filter) for error markers, NOT just the
  tail. Real causes are often buried before retry noise / cleanup blocks.
- For each marker, include ±CONTEXT_LINES of surrounding context.
- Always include the last 50 lines (final state / exit reason).
- Cap at MAX_LINES; if exceeded, keep last MAX_LINES (most recent errors win).
- deep=true uses wider context (DEEP_CONTEXT_LINES) and higher cap.

Unconditional pre-window cleaning:
- ANSI escape strip
- Drop low-signal noise: INFO/DEBUG/[Pipeline] markers, Maven Downloaded from,
  Progress percent lines, blank lines
- Dedupe consecutive identical lines (retry-spam compression)
"""
import re

# ANSI escape code pattern
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Patterns that indicate error lines
ERROR_PATTERNS = re.compile(
    r'(error|ERROR:|Exception|FAILURE|FATAL|parse error|Caused by|'
    r'stack trace|exit code|Result !=0|BUILD FAILED|fatal:|FAILED)',
    re.IGNORECASE,
)

# Lines to drop pre-window (low-signal noise that bloats tokens)
NOISE_PATTERNS = re.compile(
    r'^\s*(\[Pipeline\] (?:end|}|stage|node|withCredentials|withEnv|script|sh)|'
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*?DEBUG\b|'
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*?INFO\b|'
    r'\[INFO\]|\[DEBUG\]|'
    r'Downloading from|Downloaded from|'
    r'Progress \(\d+\):|'
    r'\d+/\d+ KB|'
    # Canary / Kayenta polling spam (often 100s of repeats per build)
    r'Got canary run result successfully\.|'
    r'Canary run triggered successfully\.|'
    r'Response from Kayenta\{|'
    r'triggering canary run for canary|'
    # Generic poll/wait spam
    r'Waiting for|'
    r'Polling for|'
    # Maven / gradle download chatter
    r'Resolved \S+ in \d+(\.\d+)? s|'
    r'^\s*\.{3,}\s*$|'
    # Terraform plan refresh noise
    r'Refreshing state\.\.\.|'
    r'Reading\.\.\.|'
    r'Read complete after|'
    # AWS CLI text-format table rows (TSV-style noise from describe-rules etc.)
    r'^RULES\s|^ACTIONS\s|^TARGETGROUPS\s|^TARGETGROUPSTICKINESSCONFIG\s|'
    r'^CONDITIONS\s|^VALUES\s|^FORWARDCONFIG\s)\b',
    re.IGNORECASE,
)

# Lines that contain non-fatal noise anywhere (not just at line start).
# Drop these whole — they bloat tokens AND cause classifier false-matches.
NOISE_CONTAINS = re.compile(
    # JVM startup flags from config.json server_command dumps. The literal
    # text `OutOfMemoryError` in `-XX:+HeapDumpOnOutOfMemoryError` false-matched
    # the java_runtime classifier rule.
    r'-XX:[+-]?[A-Za-z]|'
    # NewRelic appName-not-registered XML response. Non-fatal observability
    # gap, not a deploy failure. Pipeline has SSM fallback.
    r'<error>Application .+ does not exist\.</error>|'
    r'<\?xml version="1\.0"|'
    r'^\s*<errors>|^\s*</errors>',
    re.IGNORECASE,
)

# Multi-line non-fatal blocks stripped from raw log before line-splitting.
# Each pair: (regex matching whole block, replacement marker).
# Kept short to preserve a breadcrumb that the noise existed without bloating
# tokens or confusing the LLM into treating it as the root cause.
_MULTILINE_NOISE = [
    # OpenSSH known-hosts mismatch banner — pipeline has SSM fallback for
    # instance login, so this warning is non-fatal. Strip the entire block.
    (
        re.compile(
            r"@{20,}\s*\n"
            r"@\s+WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED![^\n]*\n"
            r"@{20,}[\s\S]*?"
            r"UpdateHostkeys is disabled because the host key is not trusted\.\s*\n",
            re.MULTILINE,
        ),
        "[noise stripped: SSH host-key mismatch warning — non-fatal, SSM fallback in use]\n",
    ),
]

CONTEXT_LINES = 50          # default ±N around each error hit
MAX_LINES = 400             # default cap (raised from 300 — see all errors)
DEEP_CONTEXT_LINES = 100    # deep mode
DEEP_MAX_LINES = 800        # deep cap


MAX_LINE_LEN = 250  # truncate long lines (config dumps, large JSON blobs)
# Single-line dict/JSON dumps: 3+ key=value pairs separated by commas →
# drop entirely. These are big config dumps that bloat tokens but rarely
# hold the failure cause.
_BIG_DICT_RE = re.compile(r"=\S+\s*,\s*\S+=\S+\s*,\s*\S+=\S+\s*,")


# Health check polling spam: `Health Status for  after N iterations: unhealthy`
# repeats up to 50 times in a row. Each line is different (N varies) so the
# simple consecutive-dedupe doesn't collapse it. Detect run + collapse to a
# single summary line so LLM sees the signal without the bulk.
_UNHEALTHY_ITER_RE = re.compile(
    r"Health Status for .* after (\d+) iterations:\s*unhealthy",
    re.IGNORECASE,
)


def _strip_multiline_noise(raw_log: str) -> str:
    """Pre-pass: replace multi-line non-fatal banners with one-line markers."""
    for pat, replacement in _MULTILINE_NOISE:
        raw_log = pat.sub(replacement, raw_log)
    return raw_log


def _collapse_unhealthy_run(lines: list[str]) -> list[str]:
    """Collapse runs of `Health Status ... iterations: unhealthy` lines.

    Keeps the first + last iteration line + summary count. Operator still sees
    the iteration loop happened without 50 nearly-identical lines bloating the
    window.
    """
    out = []
    i = 0
    while i < len(lines):
        if _UNHEALTHY_ITER_RE.search(lines[i]):
            j = i
            while j < len(lines) and _UNHEALTHY_ITER_RE.search(lines[j]):
                j += 1
            run_len = j - i
            if run_len >= 3:
                out.append(lines[i])
                out.append(f"[{run_len - 2} more iterations elided, all unhealthy]")
                out.append(lines[j - 1])
            else:
                out.extend(lines[i:j])
            i = j
        else:
            out.append(lines[i])
            i += 1
    return out


def _strip_and_filter(raw_log: str) -> list[str]:
    """ANSI strip, drop noise lines, dedupe consecutive duplicates, truncate long lines."""
    raw_log = _strip_multiline_noise(raw_log)
    out = []
    prev = None
    for line in raw_log.splitlines():
        line = ANSI_ESCAPE.sub('', line)
        if not line.strip():
            continue
        if NOISE_PATTERNS.search(line):
            continue
        if NOISE_CONTAINS.search(line):
            continue
        if line == prev:
            continue
        # Drop massive single-line dict/JSON dumps (service config snapshots, etc.)
        if len(line) > 400 and _BIG_DICT_RE.search(line):
            continue
        # Truncate any remaining long lines (large JSON values, command output)
        if len(line) > MAX_LINE_LEN:
            line = line[:MAX_LINE_LEN] + " …[truncated]"
        out.append(line)
        prev = line
    return _collapse_unhealthy_run(out)


# Stage markers: "[Pipeline] { (Build)", "[Pipeline] { (Rollout)", etc.
_STAGE_RE = re.compile(r"\[Pipeline\] \{ \(([^)]+)\)")

# Failure markers: anything past these lines is post-failure cleanup, not
# the failing stage. "Declarative: Post Actions" runs *after* failure so we
# must stop scanning before it.
_FAILURE_MARKER_RE = re.compile(
    r"(Rolling Back as Result|Rollout back as Canary|"
    r"BUILD FAILED|ERROR:|hudson\.AbortException|"
    r"Canary run failed for canary run id|"
    r'"canary_run_status"\s*:\s*"Fail"|'
    r"Stage \".*?\" skipped due to earlier failure)",
    re.IGNORECASE,
)

# Stages we always ignore — they wrap post{} blocks, not real pipeline work
_IGNORED_STAGES = {
    "declarative: post actions",
    "declarative: tool install",
    "declarative: agent setup",
    "post actions",
}

# In-stage error markers used by Strategy A — these signal "the failure
# happened inside THIS stage". Distinct from _FAILURE_MARKER_RE which marks
# end-of-pipeline rollback noise.
_IN_STAGE_ERROR_RE = re.compile(
    r"(Error in [A-Za-z_]+|script returned exit code \d+|BUILD FAILED|"
    r"hudson\.AbortException|Health Status failed to move to healthy)",
    re.IGNORECASE,
)

# Marks a stage as deliberately skipped (skip-cascade after earlier failure).
# Strategy B uses this to exclude post-failure stage markers like "Rollout"
# that appear in the log but never actually ran.
_STAGE_SKIPPED_RE = re.compile(
    r'Stage "([^"]+)" skipped due to earlier failure',
)


def extract_failed_stage(raw_log: str) -> str | None:
    """Find the user-defined stage where the failure actually happened.

    Strategy A (primary): For each stage, look at the lines from its
    `[Pipeline] { (Name)` marker up to the next stage marker. If that block
    contains an in-stage error marker (Error in.../exit code/BUILD FAILED),
    that stage is the real failure point — return it.

    Strategy B (fallback): If no stage matches Strategy A, return the last
    real stage NOT followed by a "Stage X skipped due to earlier failure"
    line. This handles legacy pipelines whose error markers are bare `ERROR:`
    lines without the explicit "Error in <stage>" form.
    """
    lines = raw_log.splitlines()
    stages: list[tuple[str, int]] = []   # [(name, start_idx)]
    skipped: set[str] = set()

    for i, line in enumerate(lines):
        m = _STAGE_RE.search(line)
        if m:
            name = m.group(1)
            if name.lower() not in _IGNORED_STAGES:
                stages.append((name, i))
            continue
        sm = _STAGE_SKIPPED_RE.search(line)
        if sm:
            skipped.add(sm.group(1))

    # Strategy A: first stage whose body contains an in-stage error marker.
    for k, (name, start) in enumerate(stages):
        if name in skipped:
            continue
        end = stages[k + 1][1] if k + 1 < len(stages) else len(lines)
        for j in range(start, end):
            if _IN_STAGE_ERROR_RE.search(lines[j]):
                return name

    # Strategy B fallback: last non-skipped stage before pipeline gave up.
    last_real_stage = None
    for line in lines:
        m = _STAGE_RE.search(line)
        if m:
            name = m.group(1)
            if name.lower() in _IGNORED_STAGES or name in skipped:
                continue
            last_real_stage = name
            continue
        if _FAILURE_MARKER_RE.search(line):
            break
    return last_real_stage


def _anchored_window(lines: list[str], context: int, cap: int) -> str:
    """Error-anchored window: ±context around each error hit + last 50 lines."""
    hits = {i for i, l in enumerate(lines) if ERROR_PATTERNS.search(l)}
    if not hits:
        return '\n'.join(lines[-cap:])

    selected = set()
    for idx in hits:
        for j in range(max(0, idx - context), min(len(lines), idx + context + 1)):
            selected.add(j)
    # always include last 50
    for j in range(max(0, len(lines) - 50), len(lines)):
        selected.add(j)

    result = [lines[i] for i in sorted(selected)]
    if len(result) > cap:
        result = result[-cap:]
    return '\n'.join(result)


def extract_window(raw_log: str, deep: bool = False) -> str:
    """Return sanitized + compressed log window. Scans FULL log for errors."""
    lines = _strip_and_filter(raw_log)
    if not lines:
        return ""

    if deep:
        return _anchored_window(lines, DEEP_CONTEXT_LINES, DEEP_MAX_LINES)
    return _anchored_window(lines, CONTEXT_LINES, MAX_LINES)
