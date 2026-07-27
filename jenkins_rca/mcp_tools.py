import json
import os
import subprocess
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
REPOS_DIR = Path(os.environ.get("JENKINS_RCA_REPOS_DIR", str(_BASE_DIR / "repos")))
DOCS_DIR  = Path(os.environ.get("JENKINS_RCA_DOCS_DIR",  str(_BASE_DIR / "docops")))
CONFIG_JSON = REPOS_DIR / "jenkins_pipeline" / "resources" / "config.json"

_config: dict | None = None


def _load_config() -> dict:
    global _config
    if _config is None:
        with open(CONFIG_JSON) as f:
            _config = json.load(f)
    return _config


def repo_search(repo: str, query: str, max_results: int = 20) -> str:
    """ripgrep search over repo files."""
    repo_path = REPOS_DIR / repo
    if not repo_path.exists():
        return f"repo {repo} not found at {repo_path}"
    result = subprocess.run(
        ["rg", "--line-number", "--context", "3", "-m", str(max_results), query, str(repo_path)],
        capture_output=True, text=True
    )
    return result.stdout or f"no matches for '{query}' in {repo}"


def repo_read_file(repo: str, path: str, start: int = 0, end: int = 0) -> str:
    """Read file from repo with optional line range.

    Line numbers in the returned text are the REAL file line numbers (not
    array indices) so the agent can cite them directly in evidence.
    """
    file_path = REPOS_DIR / repo / path
    if not file_path.exists():
        return f"file not found: {file_path}"
    lines = file_path.read_text(errors="replace").splitlines()
    if start and end:
        sliced = lines[start - 1:end]
        return '\n'.join(f"{start + i}: {l}" for i, l in enumerate(sliced))
    if start:
        sliced = lines[start - 1:start + 99]
        return '\n'.join(f"{start + i}: {l}" for i, l in enumerate(sliced))
    return '\n'.join(f"{i+1}: {l}" for i, l in enumerate(lines))


def repo_list_dir(repo: str, path: str = "") -> list[str]:
    """List immediate children of a directory in the repo. Useful when the
    agent doesn't know the exact filename yet (e.g. exploring `vars/`).
    Directories are returned with a trailing `/`.
    """
    base = REPOS_DIR / repo / path
    if not base.exists():
        return [f"path not found: {path}"]
    if not base.is_dir():
        return [f"not a directory: {path}"]
    out = []
    for child in sorted(base.iterdir()):
        if child.name.startswith(".git"):
            continue
        out.append(child.name + ("/" if child.is_dir() else ""))
    return out


def repo_find_function(repo: str, name: str, max_hits: int = 5) -> str:
    """Find where a Groovy / Java / Python function is *defined* in a repo.

    Matches the common definition patterns used in this codebase:
      Groovy:  `def <name>(`, `static def <name>(`, `<name> = { ... }`
      Java:    `<modifier...> ReturnType <name>(`  (handled via simple grep)
      Python:  `def <name>(`

    Special case — Jenkins shared library: a file `vars/<name>.groovy`
    containing `def call(...)` IS the definition of step `<name>`. The
    raw regex search can't see that (it would match the `def call`
    inside, but the function-name token is "call" not "<name>"). When
    the convention applies, prepend the `def call(` line as the
    authoritative implementation citation before falling back to the
    generic regex hits (which then surface as call-sites).

    Returns ripgrep-style hits with line numbers, capped at max_hits.
    """
    repo_path = REPOS_DIR / repo
    if not repo_path.exists():
        return f"repo {repo} not found at {repo_path}"

    out_lines: list[str] = []

    # Jenkins shared-lib special case: vars/<name>.groovy with `def call(`.
    vars_file = repo_path / "vars" / f"{name}.groovy"
    if vars_file.is_file():
        try:
            with open(vars_file, "r", errors="replace") as _f:
                for i, line in enumerate(_f, start=1):
                    if "def call(" in line or "def call (" in line:
                        rel = vars_file.relative_to(repo_path)
                        out_lines.append(f"{vars_file}:{i}:{line.rstrip()}")
                        out_lines.append(
                            f"# ↑ Jenkins shared-lib convention: "
                            f"vars/{name}.groovy is the implementation of step '{name}()'"
                        )
                        break
        except Exception:
            pass

    # Two patterns OR'd: definition forms vs. assignment forms.
    pattern = rf"(?:def\s+|static\s+def\s+|^\s*){__import__('re').escape(name)}\s*[(=]"
    result = subprocess.run(
        ["rg", "--line-number", "--no-heading", "--type-add", "groovy:*.groovy",
         "-tgroovy", "-tjava", "-tpy",
         "-m", str(max_hits), pattern, str(repo_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode not in (0, 1):
        if out_lines:
            return "\n".join(out_lines)
        return f"search error: {result.stderr.strip()[:200]}"
    rg_out = result.stdout.strip()
    if rg_out:
        if out_lines:
            out_lines.append("# Other matches (call-sites or non-vars defs):")
        out_lines.extend(rg_out.splitlines())

    if not out_lines:
        return f"no definitions found for '{name}' in {repo}"
    return "\n".join(out_lines)


def repo_recent_commits(repo: str, n: int = 10) -> str:
    """Return the last N commits with author + date + short message.

    Helps the agent answer "what changed recently?" — often the actual root
    cause of a freshly-failing pipeline is a commit landed in the last hour.
    """
    repo_path = REPOS_DIR / repo
    if not (repo_path / ".git").exists():
        return f"repo {repo} not a git clone"
    fmt = "%h %ad %an | %s"
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", f"-n{n}",
         "--date=short", f"--pretty=format:{fmt}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return f"git log failed: {result.stderr.strip()[:200]}"
    return result.stdout


_DOCS_EXCLUDE_SUBDIRS = {"runbooks", "job_flows"}


def docs_list() -> list[str]:
    """List available org docs (legacy: top-level docops/*.md only, names
    as strings). Kept for llm.py pre-fetch path."""
    return [f.name for f in sorted(DOCS_DIR.glob("*.md"))]


def docs_get(name: str) -> str:
    """Read doc content (legacy: returns 'doc not found: <name>' on miss).
    Kept for llm.py pre-fetch path.

    Search order: docops/<name>, docops/<name>.md, docops/runbooks/<name>,
    docops/runbooks/<name>.md, docops/job_flows/<name>,
    docops/job_flows/<name>.md. Subfolder fallback added Phase 1 so legacy
    CLASS_DOCS mapping in llm.py can point at canonical runbook files
    (e.g. "health_check.md") after deleting docops/HealthCheckFailure.md.
    """
    candidates = [
        DOCS_DIR / name,
        DOCS_DIR / f"{name}.md",
        DOCS_DIR / "runbooks" / name,
        DOCS_DIR / "runbooks" / f"{name}.md",
        DOCS_DIR / "job_flows" / name,
        DOCS_DIR / "job_flows" / f"{name}.md",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text()
    return f"doc not found: {name}"


def list_docs() -> list[dict]:
    """List org-wide docs under docops/ (excluding runbooks/ + job_flows/
    which have their own list/read tools). Returns name + first-line title
    so the LLM can pick by topic without loading every file.
    """
    if not DOCS_DIR.is_dir():
        return [{"error": f"docs dir not found at {DOCS_DIR}"}]
    out = []
    for p in sorted(DOCS_DIR.rglob("*.md")):
        # Skip subdirs that have dedicated tools.
        rel = p.relative_to(DOCS_DIR)
        if rel.parts and rel.parts[0] in _DOCS_EXCLUDE_SUBDIRS:
            continue
        title = ""
        try:
            for line in p.read_text(errors="replace").splitlines():
                ls = line.strip()
                if ls:
                    title = ls.lstrip("# ").strip()
                    break
        except Exception as e:
            title = f"<read error: {e}>"
        out.append({"name": p.stem, "rel": str(rel), "title": title[:160]})
    return out


def read_doc(name: str) -> str:
    """Read any docops/*.md (or subdir) by stem name. Excludes runbooks/
    + job_flows/ — use read_runbook / read_job_flow for those.
    """
    if not DOCS_DIR.is_dir():
        return f"docs dir not found at {DOCS_DIR}"
    candidates = []
    for p in DOCS_DIR.rglob(f"{name}.md"):
        rel = p.relative_to(DOCS_DIR)
        if rel.parts and rel.parts[0] in _DOCS_EXCLUDE_SUBDIRS:
            continue
        candidates.append(p)
    # Also try name as-is (e.g. user passed full filename).
    p_direct = DOCS_DIR / name
    if p_direct.is_file():
        rel = p_direct.relative_to(DOCS_DIR)
        if not (rel.parts and rel.parts[0] in _DOCS_EXCLUDE_SUBDIRS):
            candidates.append(p_direct)
    if not candidates:
        avail = ", ".join(d["name"] for d in list_docs() if "name" in d)
        return f"doc '{name}' not found. Available: {avail}"
    try:
        return candidates[0].read_text(errors="replace")
    except Exception as e:
        return f"doc read error: {e}"




# ─── Runbook tools (Phase 2 — per-class drill plans) ──────────────────

# Runbooks live under DOCS_DIR/runbooks/. Each is a markdown file named
# after an error class (compliance.md, health_check.md, …). Local-dev
# fallback in the repo tree so tests can run without the EC2 install.
RUNBOOKS_DIR = DOCS_DIR / "runbooks"


def _runbooks_dir() -> Path:
    return RUNBOOKS_DIR


def list_runbooks() -> list[dict]:
    """List runbook files under DOCS_DIR/runbooks/ with one-line summaries.

    Reads the first non-heading paragraph after "## What this class means"
    so the LLM can pick which runbook to read_runbook() without loading
    all of them into context.
    """
    d = _runbooks_dir()
    if not d.is_dir():
        return [{"error": f"runbooks dir not found at {d}"}]
    out = []
    for f in sorted(d.glob("*.md")):
        try:
            text = f.read_text(errors="replace")
        except Exception as e:
            out.append({"name": f.stem, "summary": f"<read error: {e}>"})
            continue
        summary = ""
        marker = "## What this class means"
        if marker in text:
            after = text.split(marker, 1)[1]
            for line in after.splitlines():
                ls = line.strip()
                if ls and not ls.startswith("#"):
                    summary = ls
                    break
        if not summary:
            for line in text.splitlines():
                ls = line.strip()
                if ls and not ls.startswith("#"):
                    summary = ls
                    break
        out.append({"name": f.stem, "summary": summary[:200]})
    return out


def read_runbook(name: str) -> str:
    """Read one runbook by stem (no .md extension)."""
    d = _runbooks_dir()
    p = d / f"{name}.md"
    if not p.is_file():
        avail = ", ".join(sorted(f.stem for f in d.glob("*.md")))
        return f"runbook '{name}' not found. Available: {avail}"
    try:
        return p.read_text(errors="replace")
    except Exception as e:
        return f"runbook read error: {e}"


# ─── Job-flow tools (per-pipeline-family orientation docs) ────────────
#
# Job flows live under DOCS_DIR/job_flows/. Each is a markdown file
# describing the SHAPE of one Jenkins pipeline family — its main
# pipeline file path, top-level stages, which helper file each stage
# delegates to, and where the chain nests (e.g. a stage that wraps
# another helper which has its own sub-stages). LLM reads the matching
# job_flow EARLY so it knows the structure before reading individual
# .groovy files. No example values — only verified facts derived from
# reading the actual pipeline source.
JOB_FLOWS_DIR = DOCS_DIR / "job_flows"
JOB_FLOWS_DIR_FALLBACK = Path(__file__).resolve().parent.parent / "docops" / "job_flows"


def _job_flows_dir() -> Path:
    if JOB_FLOWS_DIR.is_dir():
        return JOB_FLOWS_DIR
    return JOB_FLOWS_DIR_FALLBACK


def list_job_flows() -> list[dict]:
    """List job-flow files with one-line match patterns.

    Reads the first non-heading line after "## Match" so the LLM can
    pick which flow doc to read_job_flow() without loading all of them.
    """
    d = _job_flows_dir()
    if not d.is_dir():
        return [{"error": f"job_flows dir not found at {d}"}]
    out = []
    for f in sorted(d.glob("*.md")):
        if f.stem == "index":
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception as e:
            out.append({"name": f.stem, "match": f"<read error: {e}>"})
            continue
        match = ""
        marker = "## Match"
        if marker in text:
            after = text.split(marker, 1)[1]
            for line in after.splitlines():
                ls = line.strip()
                if ls and not ls.startswith("#"):
                    match = ls
                    break
        if not match:
            for line in text.splitlines():
                ls = line.strip()
                if ls and not ls.startswith("#"):
                    match = ls
                    break
        out.append({"name": f.stem, "match": match[:200]})
    return out


def read_job_flow(name: str) -> str:
    """Read one job-flow doc by stem (no .md extension)."""
    d = _job_flows_dir()
    p = d / f"{name}.md"
    if not p.is_file():
        avail = ", ".join(sorted(f.stem for f in d.glob("*.md") if f.stem != "index"))
        return f"job_flow '{name}' not found. Available: {avail}"
    try:
        return p.read_text(errors="replace")
    except Exception as e:
        return f"job_flow read error: {e}"


_SLIM_FIELDS = (
    "name", "service_name", "service_identifier", "env",
    "aws_account", "region", "aws_region",
    "deploy_type", "infra_type", "is_non_web", "service_type",
    "instance_class", "ami", "target_group_name", "rule_arn",
    "health_check_path", "health_check_port", "canary_threshold", "traffic_values",
    "auto_scaling_group", "min_capacity", "max_capacity", "desired_capacity",
    "git_repo", "github_repo", "repo", "repo_name", "service_repo",
    "branch", "default_branch",
    "new_relic_name", "newrelic_name", "nr_app_name",
    "slack_channel",
    # Surfaced for health_check class so LLM can tell operator where to look
    "log_path", "service_port", "port", "app_port", "container_port",
    # Real-world field names used in this org's config.json (alternatives to
    # the canonical names above). `target_port` is the ALB target group port,
    # `filebeat_log_path` is the service log file path, `key_name` is the SSH
    # key name (operator builds `.pem` path from it), `server_command` is the
    # full `java ...` startup command (contains `-Dlog.dir=` hint).
    "target_port", "filebeat_log_path", "key_name", "server_command",
)


def service_lookup(service_name: str) -> dict:
    """Return slim config.json entry — only fields useful for RCA reasoning.

    Drops verbose metadata (ARNs of unrelated resources, timestamps, tags) to
    cut prompt tokens. Operator can inspect full config via repo_read_file if
    LLM requests it.
    """
    config = _load_config()
    entry = config.get(service_name)
    if not entry:
        matches = [k for k in config if service_name.lower() in k.lower()]
        if matches:
            return {"matches": matches[:5], "hint": "use exact name"}
        return {"error": f"service '{service_name}' not found in config.json"}
    # Keep only meaningful fields, drop None/empty
    slim = {k: entry[k] for k in _SLIM_FIELDS if k in entry and entry[k] not in (None, "", [], {})}
    if not slim:
        # service exists but no matching slim fields — return all top-level keys for visibility
        slim = {"_keys": list(entry.keys())[:20]}
    return slim
