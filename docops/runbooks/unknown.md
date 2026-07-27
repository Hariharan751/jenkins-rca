# Runbook: unknown (fallback)

## When to pick this class
- The log error doesn't match any other runbook's detect signals.
- Multiple signals match weakly (no clear winner).
- A new failure pattern jenkins-rca hasn't seen before.

Do NOT pick `unknown` to bail early. Pick it only when you've genuinely
ruled out the other classes by reading their detect-signals sections.

## What you must still do
You still cite `jenkins_pipeline/<file>:<line>` in evidence (mandatory
cross-check). You still iterate until clear RCA. `unknown` doesn't
relax those requirements — it just means the drill plan is generic.

## Generic drill plan — keep going until clear

### Step 1: Pipeline source
1. `get_jenkins_job_config(job)` → scriptPath
2. `repo_read_file("jenkins_pipeline", <scriptPath>, ...)` around the
   failed stage from log markers

### Step 2: Regex the log for IDs and call the matching tool

Scan the log for ALL of these patterns. For each match, call the tool:

| Regex | Tool |
|---|---|
| `\b([A-Z]+-\d+)\b` (Jira ticket key) | `jira_get_ticket(<key>)` |
| `\b[0-9a-f]{7,40}\b` (git SHA) | `github_get_commit(<repo>, <sha>)` |
| `\bi-[0-9a-f]{8,17}\b` (EC2 instance) | `aws_describe_instance(<id>)` |
| `\barn:aws:elasticloadbalancing:[^ ]+listener-rule[^ ]+` | `aws_describe_listener_rule(<arn>)` |
| `\barn:aws:elasticloadbalancing:[^ ]+targetgroup[^ ]+` | `aws_describe_target_group(<arn>)` + `aws_describe_target_health(<arn>)` |
| `vars/(\w+)\.groovy` | `repo_read_file("jenkins_pipeline", "vars/<name>.groovy", ...)` |
| `[\w/]+\.groovy:(\d+)` (stack frame) | `repo_read_file("jenkins_pipeline", <path>, <line>-10, <line>+10)` |
| `[A-Z][a-zA-Z]+Exception` (Java exc class) | `repo_search("jenkins_pipeline", "<ExceptionClass>")` |

### Step 3: Recent commits
3. `repo_recent_commits("jenkins_pipeline", 10)` — any just-pushed change?
4. `repo_recent_commits("InfraComposer", 10)` — same for infra
5. If service_repo from service.lookup: `github_recent_commits(<service_repo>, "main", 10)`

### Step 4: If still unclear after steps 1-3
6. `repo_search("jenkins_pipeline", "<exact unusual string from log>")` — text search
7. `code_review(<best-guess fix>, "is this likely to fix the error: <log line>")`
   for a second-opinion sanity check

### Step 5: ONLY emit final JSON when you can name
- a specific file:line that caused the failure, OR
- the external system (Jira / AWS / GitHub) state that caused it, OR
- a recent commit that changed the failing area

## When you really cannot find a cause
Emit JSON with `needs_deeper: true` AND in `suggested_fix.Finding`:
- list what you investigated
- list what files / external systems you ruled out
- list what specific evidence is missing (e.g. "need ALB access logs",
  "need NewRelic transaction trace for time X", "need service repo
  access for `xyz`")

## Output schema notes
- `error_class: "unknown"`
- `evidence[]` must include:
  - `jenkins_log` snippets you reasoned from
  - `jenkins_pipeline/<file>:<line>` (still mandatory — at least the
    stage block from scriptPath)
  - Any tool results you got from steps 1-4

## STRICT rules
- DO NOT shortcut to `unknown` without checking the other runbooks first.
- DO NOT set `needs_deeper: false` if you couldn't name a concrete cause.
- DO NOT cite files you didn't open via a tool.
