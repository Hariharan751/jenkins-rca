# jenkins-rca — Jenkins Pipeline Auto-RCA

Automated Root Cause Analysis service for Jenkins `stagger-prod-plus-one` + sibling pipelines. On every failed build, Jenkins POSTs a signed webhook to this service; the service fetches the console log via Jenkins REST API, classifies the failure, enriches with context (Jira / GitHub / NewRelic / runbook docs / repo source + RAG semantic memory of past RCAs), calls an LLM, runs LangGraph ULTIMATUM gates + validator hard gates, and returns structured RCA JSON that's printed back into the Jenkins console.

**This doc is operational.** For the design-side picture (RAG +
LangGraph internals, why graphs not nested ifs, cost model) read
[`RAG_and_LangGraph.md`](RAG_and_LangGraph.md). For the
end-to-end request flow with parallel-fetch + gate-routing
diagrams read [`workflow.md`](workflow.md).

**Current status (May 2026):**
- Production branch: `feature/jenkins-rca-agent-RAG-LANG`
- 17 classifier classes routed through agent + LangGraph gates
- 4 layers of validator hardening (schema / runbook / framing / compliance hallucination)
- 3 layers of placeholder-cmd defense (primer pre-fetch / prompt rule / server gate)
- Audit JSONs feed back into RAG (past-incident memory)
- S3 doc sync DROPPED — `docops/` in this repo is canonical

---

## Architecture

```
┌──────────────┐   POST signed webhook    ┌───────────────────────┐
│   Jenkins    ├─────────────────────────▶│  ALB                  │
│ (post.failure)│  HMAC-SHA256 sig        │  jenkins-rca.jinka.in    │
└──────────────┘                          │  /rca/v1/rca/webhook  │
       ▲                                  └──────────┬────────────┘
       │ RCA JSON                                    │ :7070
       │ (printed via                                ▼
       │  renderRca)                       ┌───────────────────────┐
       └──────────────────────────────────│  bbctl-ec2:7070       │
                                          │  FastAPI/uvicorn      │
                                          │  jenkins-rca service    │
                                          └──────────┬────────────┘
                                                     │
              ┌──────────────────────────────────────┼──────────────────────────┐
              ▼                                      ▼                          ▼
    ┌──────────────────┐                  ┌─────────────────┐         ┌──────────────────┐
    │ Jenkins REST API │                  │ OpenAI / Gemini │         │ Jira / GitHub /  │
    │ (console log)    │                  │  (LLM)          │         │ NewRelic / docs  │
    └──────────────────┘                  └─────────────────┘         └──────────────────┘
```

**Pipeline flow** (server side, `jenkins_rca/main.py::_run_rca`):

1. Verify HMAC signature (`X-Bbctl-Signature: sha256=...`)
2. **Per-RCA freshness pull** — `git_fresh.ensure_fresh_many([jenkins_pipeline, InfraComposer])` does a shallow `git fetch && git reset --hard` on both repos (3s timeout each, 60s dedup, falls back to local on failure)
3. Fetch console log + build_meta via Jenkins REST API
4. Sanitize log (regex-based redactions for secrets/credentials)
5. Classify error → one of: `compliance`, `canary_fail`, `canary_script_error`, `aws_limit`, `parse_error`, `java_runtime`, `scm`, `ssm`, `network`, `dependency`, `health_check`, `timeout`, `unknown`
6. Build initial tool-context (class-specific): Jira tickets, GitHub commits, NewRelic slow txns, runbook excerpts, source.trace hits, service config from `repos/jenkins_pipeline/resources/config.json`
7. **Dispatch**: if `error_class ∈ {compliance, canary_fail, canary_script_error, health_check, parse_error, scm, unknown}` → **agent mode** (`jenkins_rca/agent.py`, max 8 tool calls, $0.25 cap). Else → one-shot LLM call (default `gpt-4o`, JSON mode, temp 0.1)
8. Verify each evidence citation against repos on disk
9. Cache 24h in diskcache; record audit log (incl. `repos_freshness`)
10. Return RCA JSON to Jenkins, which renders the compact console block + HTML report URL

---

## EC2 layout (bbctl-ec2 = 10.34.120.223)

**Single source of truth**: `/home/ubuntu/project/Jenkins-rca` is the git clone and the live runtime directory. `git pull` here is the deploy step. `/opt/jenkins-rca/` is no longer used (service migrated away in May 2026 — see "Repo / sync history" item 99).

```
/home/ubuntu/project/Jenkins-rca/
├── jenkins_rca/           # Python service (FastAPI)
├── prompts/             # LLM system + few-shot prompts
├── docops/              # Class-specific runbook docs, job_flows, runbooks
│   ├── runbooks/        # Per-error-class drill plans (health_check.md, aws_limit.md, …)
│   └── job_flows/       # Per-pipeline-family stage→helper maps
├── classifier_rules.yml # Ordered error-class regex rules
├── sanitize_rules.yml   # Log redaction patterns
├── infra/scripts/jenkins-rca-start.sh   # systemd ExecStart target (must be +x)
├── docs/                # Project documentation (this file lives here too)
└── .venv/               # Python venv (not in git) — created via python3 -m venv .venv
```

**Repos**: external git clones (jenkins_pipeline, InfraComposer) are at `JENKINS_RCA_REPOS_DIR`, which defaults to `$BASE_DIR/repos` (relative to the package, i.e. `/home/ubuntu/project/Jenkins-rca/repos/`). The start script sets `JENKINS_RCA_REPOS_DIR=/var/cache/jenkins-rca/repos` so writes land in the writable cache dir (required by `ProtectSystem=strict`). `repos/*/` is `.gitignore`d.

**Python path constants** (`mcp_tools.py`, `git_fresh.py`, `evidence.py`, `source_trace.py`) use `Path(__file__).resolve().parent.parent` as `_BASE_DIR` with `os.environ.get("JENKINS_RCA_REPOS_DIR/DOCS_DIR", str(_BASE_DIR/...))` fallback — no hardcoded `/opt/` paths remain.

---

## Service operations

### systemd unit

```
/etc/systemd/system/jenkins-rca.service
User=ubuntu
ExecStart=/home/ubuntu/project/Jenkins-rca/infra/scripts/jenkins-rca-start.sh
ReadWritePaths=/var/cache/jenkins-rca /var/log/jenkins-rca /tmp
ProtectSystem=strict
```

The start script (`infra/scripts/jenkins-rca-start.sh`) fetches secrets from AWS Secrets Manager (`jenkins-rca/prod` in `ap-south-1`) using the instance's IAM role, exports them as env vars, sets `JENKINS_RCA_REPOS_DIR=/var/cache/jenkins-rca/repos` (writable under `ProtectSystem=strict`), then launches uvicorn from `APP_DIR=/home/ubuntu/project/Jenkins-rca` with venv at `VENV=/home/ubuntu/project/Jenkins-rca/.venv` on `0.0.0.0:7070` with 2 workers.

### Common commands

```bash
# status / restart / stop
sudo systemctl status jenkins-rca --no-pager | head -10
sudo systemctl restart jenkins-rca
sudo systemctl stop jenkins-rca

# tail logs (incl. tracebacks)
sudo journalctl -u jenkins-rca -f
sudo journalctl -u jenkins-rca -n 200 --no-pager

# local health checks
curl -sf http://127.0.0.1:7070/healthz
curl -sf http://127.0.0.1:7070/rca/healthz
# public (through ALB)
curl -sf https://jenkins-rca.jinka.in/rca/healthz
```

### Deploy a code change

```bash
# On laptop — commit + push
cd /Users/hariharan/cost_exp_aibot/BBCTLLLM/bbctl
# ...edit code...
git add <files>
git commit -m "fix(rca): ..."
git push origin feature/jenkins-rca-agent-only

# On bbctl-ec2
cd /home/ubuntu/project/Jenkins-rca
git pull origin feature/jenkins-rca-agent-only
sudo systemctl restart jenkins-rca
sudo journalctl -u jenkins-rca -n 30 --no-pager   # confirm clean start
```

> **Note**: Local files in `/Users/hariharan/cost_exp_aibot/BBCTLLLM/bbctl/` are NOT a git repo (workspace). To deploy changes made locally, either push via another git workflow or SCP to the EC2 git repo, commit there, then pull.

### Refresh external repos under `repos/`

```bash
# repos live at JENKINS_RCA_REPOS_DIR = /var/cache/jenkins-rca/repos (set by start script)
cd /var/cache/jenkins-rca/repos/jenkins_pipeline
sudo git fetch && sudo git reset --hard origin/master   # or relevant branch
```

### Cache & dedup

- 24h RCA result cache (diskcache) lives in `/var/cache/jenkins-rca/`. Same `(job, build)` returns prior RCA without re-calling LLM. Marked `from_cache: true` in response.
- 60s dedup cache for in-flight requests.
- To force fresh RCA: call `/v1/rca` directly with `{"deep": true}`.

### Force fresh RCA — full cache wipe

When testing prompt/classifier/sanitizer changes against a *previously-analyzed* build, the 24h cache returns the stale RCA even with `deep:true` in some paths (deep bypasses `get_rca` but not all `is_duplicate` short-circuits). For a guaranteed clean run:

```bash
# Clean restart with cache wipe
sudo systemctl stop jenkins-rca && \
  sudo rm -rf /var/cache/jenkins-rca/* && \
  sudo systemctl start jenkins-rca
sleep 2
curl -sf http://127.0.0.1:7070/healthz && echo " OK"

# Payload-file pattern — easier to edit/reuse than inline -d
echo '{"job":"stagger-prod-plus-devops-test","build":25,"deep":true}' > /tmp/payload_dt25.json
curl -X POST http://localhost:7070/v1/rca \
  -H 'Content-Type: application/json' \
  -d @/tmp/payload_dt25.json | jq
```

Typical latency: 30-60s (Jenkins log fetch + sanitize + LLM call). Typical cost: $0.04-0.06 with `gpt-4o` (bumped from `gpt-4o-mini` for stronger reasoning on multi-step compliance / canary cases).

To inspect specific fields without scrolling the whole JSON:
```bash
curl ... | jq '.evidence, .root_cause'
curl ... | jq '.suggested_fix, .suggested_commands'
curl ... | jq '.error_class, .failed_stage, .confidence, .tokens_used, .cost_usd'
```

---

## Jenkins integration

### Pipeline wiring (`jenkins_pipeline_master/main_stagger_prod_plus_one.groovy`)

In the `post.failure` block, after `rollbackMain(...)`:

```groovy
script {
    rollbackMain("Single Job Rollback", params.SERVICE)

    // ============ BB-AI auto-RCA (Phase A — console + build description only) ============
    // Non-fatal: any error here must not affect rollback or VictorOps alert below.
    try {
        triggerRcaWebhook()
    } catch (Exception e) {
        echo "[BB-AI] non-fatal error: ${e.message}"
    }
    // ========================================================================================

    // ... existing VictorOps alert block continues unchanged ...
}
```

### Shared library (`vars/triggerRcaWebhook.groovy`)

Posts the signed webhook using raw `HttpURLConnection` (no `httpRequest` plugin dependency, since the HTTP Request plugin is **not** installed in the Jenkins controller). Helpers:

- `triggerRcaWebhook()` — entry point called from `post.failure`; returns parsed RCA Map for Phase B/C reuse
- `postWebhook(url, payload, sig)` — `@NonCPS`, pure-Java POST; throws on transport error
- `parseJson(text)` — `@NonCPS` wraps `JsonSlurper.parseText`
- `renderRca(rca)` — prints compact console block + sets rich `currentBuild.description` with link to HTML report
- `rcaReportUrl(requestId)` — canonical URL builder (`https://jenkins-rca.jinka.in/rca/v1/report/<uuid>`); override base via `JENKINS_RCA_REPORT_BASE_URL` env
- `hmacSha256(secret, body)` — `@NonCPS` HMAC-SHA256 for request signing
- `buildAlertMessage(rca)` — one-paragraph summary for VictorOps + Slack enrichment

### Notification helper (`src/com/blackbuck/utils/Notification.groovy`)

New `rcaAlert(script, service, branch, slack_channel, rca)` static method posts the BB-AI RCA summary to the per-service Slack channel. Reuses the existing `slack-stagger-bot` Jenkins credential — no new Slack app or webhook URL needed. Orange color (`#ff8c00`) differentiates from red `Notification.failure` alerts.

### Credentials in Jenkins

- **Secret text** with ID `bbctl-webhook-secret` matching `WEBHOOK_SECRET` in AWS Secrets Manager `jenkins-rca/prod`.

### What the operator sees in the build console (current — compact)

```
╔══════════════════════════════════════════════════════════════════╗
║               Jenkins Build RCA — Powered by BB-AI               ║
╚══════════════════════════════════════════════════════════════════╝
  class:        compliance
  failed_stage: Jira Details
  summary:      Jira ticket PEB-7 is missing the 'Signed Off Commit ID'...

  Full RCA report: https://jenkins-rca.jinka.in/rca/v1/report/<uuid>
  request_id:      <uuid>
```

Full RCA (Root cause, Suggested fix, Commands, Evidence, Metadata) lives at the HTML report URL — operator clicks through. Keeps Jenkins console scrollable.

Build description (sidebar) shows two compact lines:
```
BB-AI: <code>class</code> · <code>stage</code>
<trimmed summary>… Open RCA →   ← clickable link to full HTML report
```

### HTML report (`/rca/v1/report/<request_id>`)

Polished, self-contained dark-theme HTML page served by FastAPI. Same URL appears in Jenkins console, sidebar description, Slack message, and VictorOps `details`. Loaded from `jenkins_rca/templates/rca_report.html`.

**Sections (top → bottom):**
- **Sticky topbar** — title + clickable build link + anchor nav (Summary / Root cause / Fix / Commands / Evidence)
- **Hero card** — class pill (color-coded per `error_class`), stage pill, `needs_deeper` pill if set, service code chip, action pills linking to Jenkins build / Console log / Raw JSON
- **Summary** — one-line LLM-generated summary
- **Two-column grid** — Root cause | Suggested fix (Map form splits into Finding / Action / Verify dt-dd rows)
- **Suggested commands** — dark terminal-style blocks with tier pill (`safe` green / `restricted` amber), one-line rationale, syntax-highlighted command, and a Copy button (clipboard)
- **Evidence** — colored ✓/✗/? badges with source label + snippet
- **Metadata** — request_id, provider, redactions, log_window_chars, recorded_at
- **Footer** — `BB-AI · powered by jenkins-rca` + `Built by Hariharan G, DevOps`

**Design choices:**
- Dark navy palette (`#0a0f1c` bg) with subtle dot pattern, calm low-light feel
- Translucent class-colored pills with matching borders → glow effect on dark
- Inter-style system-font stack (`-apple-system`, `Segoe UI`, etc.) — zero CDN deps (works in restricted networks)
- Self-contained CSS; no Tailwind / external fonts
- Sticky frosted-glass topbar

**Endpoints:**
- `GET /rca/v1/report/<request_id>` — HTML page
- `GET /rca/v1/report/<request_id>.json` — raw audit JSON (for scripts/debugging)

---

## Configuration

### Environment variables (set by `infra/scripts/jenkins-rca-start.sh` from Secrets Manager)

| Var                     | Purpose                                              |
| ----------------------- | ---------------------------------------------------- |
| `JENKINS_RCA_JENKINS_URL`     | Jenkins controller URL                               |
| `JENKINS_RCA_JENKINS_USER`    | Jenkins API user                                     |
| `JENKINS_RCA_JENKINS_TOKEN`   | Jenkins API token                                    |
| `JENKINS_RCA_WEBHOOK_SECRET`  | HMAC secret shared with Jenkins                      |
| `JENKINS_RCA_LLM_API_KEY`     | OpenAI / Gemini API key                              |
| `JENKINS_RCA_LLM_PROVIDER`    | `openai` (default) or `gemini`                       |
| `JENKINS_RCA_URL`         | (Jenkins-side env override) full webhook URL         |
| `AWS_REGION`            | `ap-south-1`                                         |
| `JENKINS_RCA_SECRET_ID`       | `jenkins-rca/prod`                                     |

### ALB routing

- Listener: HTTPS:443 on `app/stagger-FE/...`
- Rule: host `jenkins-rca.jinka.in` + path `/rca/*` → target group `jenkins-rca-tg` → bbctl-ec2:7070
- FastAPI mounts the same `APIRouter` at both `/` and `/rca/` so direct-port access (`:7070/healthz`) and ALB-routed (`/rca/healthz`) both work.

### Cost guardrails

- Per-call cost estimated from token counts (`gpt-4o`: $2.50/1M input, $10.00/1M output)
- Daily spend cap enforced via `cache.over_daily_cap()` → HTTP 429
- Cached responses (24h) skip the LLM call entirely

---

## Repo / sync history (the migration that got us here)

Before: `/opt/jenkins-rca/` was a flat copy of the Python service, manually rsynced from laptop. `/home/ubuntu/project/Jenkins-rca/` was a separate git clone used only for reading source via `repo_read_file`. Two copies drift; one fix lands in git but not in `/opt`, and a redeploy quietly breaks.

**May 2026 migration (item 99 below)**: `/opt/jenkins-rca/` removed entirely. Service now runs directly from `/home/ubuntu/project/Jenkins-rca/`. All hardcoded `/opt/jenkins-rca/` paths in Python files made relative. systemd `ExecStart` points to `~/project/bbctl/infra/scripts/jenkins-rca-start.sh`. New venv at `~/project/bbctl/.venv`. `git pull` in `~/project/bbctl/` is the one and only deploy step.

Rollback: re-clone at `/opt/jenkins-rca/` is no longer the rollback path. Use `git revert` + `git pull` + `systemctl restart` instead.

---

## Troubleshooting

### Pipeline aborts with `NoSuchMethodError: No such DSL method 'httpRequest'`
HTTP Request plugin is not installed on Jenkins. The shared library was already migrated to `HttpURLConnection`; ensure `vars/triggerRcaWebhook.groovy` matches `bbctl/infra/jenkins/post_failure_rca.groovy` (no `httpRequest(...)` call).

### Service exits with `status=203/EXEC`
Missing exec bit on `infra/scripts/jenkins-rca-start.sh`. Fix:
```bash
chmod +x /opt/jenkins-rca/infra/scripts/jenkins-rca-start.sh
sudo systemctl restart jenkins-rca
```
Git tracks the mode (`100755`) but a fresh clone on a system where `core.filemode=false` may drop it.

### `FileNotFoundError: .../repos/jenkins_pipeline/resources/config.json`
External clones missing under `repos/`. Re-clone:
```bash
cd /opt/jenkins-rca/repos
sudo git clone <jenkins_pipeline_url> jenkins_pipeline
sudo chown -R ubuntu:ubuntu jenkins_pipeline
```

### `pydantic ValidationError: buildUrl / consoleUrl Field required`
Old `WebhookPayload` schema. Pull latest — fields are now `Optional` (default `""`), so the Jenkins groovy payload (`job/build/service` only) validates.

### `HTTP 500` from webhook
Tail logs for traceback:
```bash
sudo journalctl -u jenkins-rca -f
```
Common causes: missing/expired Jenkins API token, OpenAI quota, malformed log window, source-trace repo not cloned.

### Public health check fails but local works
ALB target group health, security group ingress, or HTTPS cert. Quick checks:
```bash
curl -sf http://127.0.0.1:7070/healthz                # service alive
curl -sf https://jenkins-rca.jinka.in/rca/healthz      # ALB path works
# AWS console: target group jenkins-rca-tg → Targets → status
```

### Force re-analysis (skip 24h cache)
```bash
curl -X POST http://127.0.0.1:7070/v1/rca \
  -H 'Content-Type: application/json' \
  -d '{"job": "stagger-prod-plus-one", "build": 12345, "deep": true}'
```

---

## Error classes — current behavior

| Class | Trigger pattern | Tool context fetched | Runbook |
| --- | --- | --- | --- |
| `compliance` | `Signed Off commit id` / `Compliance:` / `COMMIT_ID does not match` | Jira ticket (incl. `customfield_10973` Signed Off Commit ID) + GitHub commits for both SHAs | `JiraDetailsCompliance.md` |
| `canary_script_error` | `Traceback...canary.py` / `TypeError ... round ... NoneType` | `canary.py:LINE±10` from deepest traceback frame + NewRelic-data hint | `StaggerProdPlusOneDeploy.md` |
| `canary_fail` | `Rollout back as Canary failed` / `Rolling Back as Result !=0` / `canary_run_status: "Fail"` | canary stage-by-stage analysis (5/20/50/100% pass/fail) + canary.groovy + judge logic + NR slow tx | `StaggerProdPlusOneDeploy.md` |
| `health_check` | `Health Status failed to move to healthy` / `iterations: unhealthy` / `Error in Deploy_i-` | Parsed TG ARN/name + instance ID + region from `healthy.sh` line + service `log_path`/port/health endpoint + NR slow tx (if any) | `HealthCheckFailure.md` |
| `aws_limit` | `TooMany*` / `LimitExceeded` / `QuotaExceeded` | — | `AwsLimitTroubleshoot.md` |
| `parse_error` | `parse error:` / `jq: error` / `Invalid numeric literal` | `createGreenInfra.groovy:330-345` | `ConfigJsonParseError.md` |
| `java_runtime` | `java.lang.*Exception/Error` (must have full FQN — bare `OutOfMemoryError` no longer matches JVM startup flags) | source.trace hits | — |
| `scm` | `git fetch failed` / `Authentication failed.*github` / `fatal: repository` | GitHub commits | `SCMTroubleshoot.md` |
| `health_check`, `network`, `ssm`, `dependency`, `timeout`, `unknown` | various | source.trace + jira (if ticket keys in log) | — |

Classifier rule order matters — first match wins. `health_check` is above `java_runtime` so ALB-probe failures aren't masked by stray Java class references.

### `health_check` class specifics

**Org access pattern**: instance access goes through `bbctl` (org-standard CLI), NOT raw `ssh`. RCA action items are templated to use:
- `bbctl shell <instance-id>` — interactive shell on the failing instance
- `bbctl run <instance-id> -- '<cmd>'` — one-shot command (preferred for `suggested_commands` array)

The LLM is instructed to substitute the real instance_id from `health_check.target` and never emit `<instance-id>` placeholders. SSM and raw ssh are mentioned only as fallbacks.

When Jenkins `Deploy` stage runs `healthy.sh <tg-arn> <region> <instance-id> <env>` and the ALB target group probe stays unhealthy for the full poll window (typically 50 × ~6s = 5 min), pipeline aborts with:

```
Health Status failed to move to healthy within the time limit
Error in Deploy_i-<instance-id>: script returned exit code 1
```

Tool context auto-populated for the LLM:
- `health_check.target`: `target_group_name`, `target_group_arn`, `instance_id`, `region`, `env`, `failed_iterations`
- `health_check.service_config`: `log_path`, `service_port` / `port`, `health_check_path`, `health_check_port` from `config.json`
- `newrelic.slow_transactions`: NR app-name candidates for the deploy window (if empty → service never reported a single txn → likely never started)
- `health_check.guide`: 6 ordered likely causes (service didn't start / port mismatch / health endpoint 5xx / SG block / slow boot vs threshold / dependency unreachable)
- `docs.HealthCheckFailure.md` runbook content

LLM is instructed to **never** cite SSH host-key warnings or NewRelic `Application X does not exist` as root cause — both are non-fatal upstream noise the pipeline tolerates via SSM fallback / unregistered apps.

---

## Phase roadmap

- **Phase A (LIVE)** — Auto-RCA prints to console + build description on failure. Webhook is non-fatal; nothing about the existing alert flow changed.
- **Phase B (LIVE)** — VictorOps incident `message` field now includes `buildAlertMessage(rca)` so on-call sees the RCA summary inside the page itself. RCA fields (`rcaErrorClass`, `rcaFailedStage`, `rcaConfidence`, `rcaSummary`, `rcaRequestId`) also injected into the VictorOps `details` panel for structured access. Base message still leads — existing VictorOps filters / dashboards keep working.
- **Phase C (LIVE)** — Per-service Slack channel now receives a `BB-AI Auto-RCA` summary message on every failed pipeline. Uses the org's existing `slack-stagger-bot` Jenkins credential (same one as `Notification.failure`); channel routed via the per-service `config.slack_channel`. **No new infra, no new webhook URL, no Secrets Manager change.**
- **Phase D (later)** — Slack interactive button "🔍 Deep analyze" → POSTs to a new `/v1/rca/deep` endpoint with `deep:true`, replies into the same thread. Requires Slack app with `interactivity` enabled + a public jenkins-rca endpoint (already covered by ALB).
- **Phase E (LIVE)** — Hybrid freshness + agent-mode RCA. Repos pulled per-RCA + agent iteratively reads code to trace the failure backwards from the Jenkins job config to the function that threw. See "Agent mode" below.
- **Future** — fetch real Kayenta canary scores via Kayenta API for `canary_fail` class instead of inferring from build log alone.

---

## Agent mode (Phase E)

For "deep" error classes — `compliance`, `canary_fail`, `canary_script_error`, `health_check`, `parse_error`, `scm`, `unknown` — the RCA is produced by an **OpenAI function-calling agent** instead of a single one-shot LLM call. Other classes (timeout, network, ssm, dependency, java_runtime with a clean stack trace) still use the cheaper one-shot path.

**Architecture**

```
_run_rca()
  ├─► git_fresh.ensure_fresh_many([jenkins_pipeline, InfraComposer])
  │     └─ shallow fetch, 3s timeout/repo, 60s dedup, falls back to local
  ├─► Jenkins API: log + build_meta
  ├─► classify(log) → error_class
  └─► IF error_class ∈ AGENT_CLASSES and provider=openai:
        agent.run_agent(...)
            ├─ Initial primer (one-shot tool context: service.lookup,
            │   source.trace, docs.<class>.md, jira.tickets, github.commits…)
            └─ Tool-use loop (max 8 calls, $0.25 cap):
                 - get_jenkins_job_config(job)        ← almost always first
                 - repo_read_file(...)                ← read entrypoint groovy
                 - repo_find_function(...)            ← locate called helpers
                 - repo_search(...)                   ← grep for error strings
                 - repo_list_dir(...) | repo_recent_commits(...) | service_lookup(...)
      ELSE:
        run_rca(...)  ← one-shot path (cheap classes)
```

**Hybrid freshness model**

- `jenkins_rca/git_fresh.py` runs at the start of every `_run_rca` call. Performs `git fetch --depth 1 && git reset --hard origin/<branch>` on both repos in parallel. Self-heals perms (`chmod -R u+w`) so a `chmod -R a-w` from elsewhere can't permanently break sync. In-memory dedup window of 60s prevents back-to-back fetches when concurrent webhooks fire.
- The `/etc/cron.d/jenkins-rca-sync` cron is now a backstop (frequency can be relaxed from every 2h to every 6h since the per-RCA path keeps repos hot for any active build).
- If a per-RCA fetch fails (GitHub down, network blip, timeout), we silently fall back to whatever's already on disk. The `repos_freshness` block is included in the audit JSON so the operator can see whether the agent saw the latest commit.

**Tool palette exposed to the agent** (defined in `jenkins_rca/agent.py::TOOLS`)

| Tool | Backed by | What it does |
| --- | --- | --- |
| `get_jenkins_job_config(job)` | `jenkins.get_job_config` | Fetch Jenkins job's `config.xml`; surface `scm_url`, `scm_branch`, `scriptPath`. Almost always the agent's first tool call. |
| `repo_read_file(repo, path, start, end)` | `mcp_tools.repo_read_file` | Read a slice of a file. Returns real line numbers (1-based) so the agent can cite them in `evidence`. |
| `repo_search(repo, query, max_results)` | `mcp_tools.repo_search` | ripgrep across a repo for a literal string. |
| `repo_list_dir(repo, path)` | `mcp_tools.repo_list_dir` | List immediate children of a directory. |
| `repo_find_function(repo, name)` | `mcp_tools.repo_find_function` | Find where a Groovy/Java/Python function is *defined* (definition site, not call sites). |
| `repo_recent_commits(repo, n)` | `mcp_tools.repo_recent_commits` | Last N commits with author, date, short message — quickly answers "what changed?" |
| `service_lookup(service)` | `mcp_tools.service_lookup` | Slim view of `config.json` entry for a service. |

**Guards**

- **Iteration cap**: 8 tool calls max per RCA. On the 9th iteration the agent is forced into JSON-only mode (no more tools).
- **Cost cap**: $0.25 per RCA. When the running token spend hits this, the agent gets a "cost cap reached, emit JSON now" message.
- **Per-tool truncation**: any tool result over 8K chars is sliced (prevents a runaway grep from blowing the context window).
- **Logging**: every tool call is printed to stderr as `[agent] iter N tool#M: <name>({args})` — visible via `journalctl -u jenkins-rca -f`.

**Cost expectation**

Typical agent run: 4-6 tool calls, ~25-35K input tokens, ~600-900 output tokens → ~$0.07-0.10 per RCA (vs ~$0.05 for one-shot). Worst-case at the cap: ~$0.25.

**Evidence quality**

Because the agent reads real source, `evidence[].source` for agent-mode RCAs includes paths like `jenkins_pipeline/vars/canary.groovy:47` (with the actual line number from the tool call). This is more grounded than the one-shot path where citations come from `source.trace` hits only.

**Falling back to one-shot**

If `LLM_PROVIDER != "openai"` (e.g. running on Gemini) OR if `error_class` is one of the cheap classes, the dispatcher uses `run_rca(...)` exactly as before. No regression for those paths.

### Files touched by Phase E

| File | Purpose |
| --- | --- |
| `jenkins_rca/git_fresh.py` | NEW. Per-RCA shallow fetch + reset, 3s timeout, 60s dedup, fallback to local clone. |
| `jenkins_rca/jenkins.py` | NEW `get_job_config(job)` — fetch + parse Jenkins `config.xml`. |
| `jenkins_rca/mcp_tools.py` | NEW `repo_list_dir`, `repo_find_function`, `repo_recent_commits`; tightened `repo_read_file` to return real line numbers. |
| `jenkins_rca/agent.py` | NEW. OpenAI function-calling loop with iteration + cost cap. |
| `jenkins_rca/llm.py` | NEW public alias `build_initial_tool_ctx(...)` so the agent can reuse the one-shot primer. |
| `jenkins_rca/main.py` | Dispatcher: `ensure_fresh_many` at the top of `_run_rca`; agent vs one-shot routing on `error_class`. |
| `prompts/rca_agent_system.md` | NEW. Agent system prompt with the trace method, evidence rules, action rules. |

### Phase C — Slack message shape

Triggered from `main_stagger_prod_plus_one.groovy` post.failure block via:
```groovy
com.blackbuck.utils.Notification.rcaAlert(this, params.SERVICE, branchVal, slackCh, rca)
```

Method lives at `src/com/blackbuck/utils/Notification.groovy::rcaAlert(...)`. Uses `slackSend tokenCredentialId: 'slack-stagger-bot'`. Posts to `env["${SERVICE}:slack_channel"]` (the same channel that already receives the `Notification.failure` alert).

```
Build#1234 Test-Supply-Wrapper-Nonweb — BB-AI Auto-RCA  🤖
------------------------------------------------------
Class: health_check   Stage: Deploy   Confidence: 0.85

Summary: Deploy stage failed due to health check failure...

Finding: <if Map-shaped suggested_fix>
Action:  <if Map-shaped; else first 500 chars of fix string>

Commands:
• `[safe] bbctl shell i-02fc813e939bb2b39`
• `[safe] bbctl run i-02fc813e939bb2b39 -- 'sudo ss -tlnp | grep 7005'`
• `[safe] bbctl run i-02fc813e939bb2b39 -- 'curl -i http://localhost:7005/admin/version'`

Job:       <blue-ocean-link>
Branch:    <COMMIT_ID or tag>
Console:   <build_url>/console
RCA id:    <uuid>
Timestamp: <IST>
```

Orange color (`#ff8c00`) distinguishes the RCA message from the red `FAILURE` alert. Both messages land in the same channel so the team sees the failure AND the diagnosis side-by-side.

**Behavior:**
- Non-fatal: any error in `rcaAlert` is caught + echoed; never breaks rollback or VictorOps flow.
- Skipped silently if `rca == null` (webhook failed) or no `slack_channel` configured for the service.
- Skipped for canary failures by the surrounding `if (PROD_PLUS_ONE_COMPLETED && !isCanaryFailure)` guard — same logic that already gates VictorOps.

### Phase B — VictorOps message shape

```
Production pipeline failed for <service> after Prod+1 validation passed.
Rollback initiated. here is the jenkins link : <build_url>console

🤖 *BB-AI RCA* (class: health_check, stage: Deploy, conf: 0.85)
Summary: Deploy stage failed due to health check failure; ALB target group
         probe remained unhealthy for 50 iterations.
Finding: <first line of suggested_fix, if Map-shaped>
Action:  <truncated to 400 chars>
request_id: <uuid>
```

If `suggested_fix` is a single String (some classes use this shape), only the first ~400 chars appear under a `Fix:` label. For full detail, the on-call clicks through to the Jenkins console where `renderRca()` printed the full boxed block.

---

## Recent improvements (May 2026)

1. **Rebrand to BB-AI** — operator-visible heading changed from `jenkins-rca — Auto RCA` to `Jenkins Build RCA — Powered by BB-AI`. Log prefixes `[jenkins-rca]` → `[BB-AI]`. Internal infra names (URL, credential ID, secret ID) unchanged.
2. **`health_check` error class added** — ALB target-group probe failures (`healthy.sh` 50-iteration loop) now classified correctly instead of falling through to `java_runtime` via the `OutOfMemoryError` flag false-match.
3. **Sanitizer: drop SSH host-key + NewRelic appName-404 + JVM flags** — these noise blocks no longer reach the LLM, so RCAs don't incorrectly cite "SSH key mismatch" as the root cause when SSM fallback is present.
4. **Iteration-spam collapse** — runs of `Health Status for  after N iterations: unhealthy` collapse to first + last + `[N-2 more iterations elided, all unhealthy]`. Cuts ~50 nearly-identical lines per failed deploy.
5. **Stage extractor rewrite** — Strategy A (first stage containing `Error in` / `script returned exit code` / `BUILD FAILED`) with Strategy B fallback (last non-skipped stage). Fixes the misclassification where `Stage "Rollout" skipped due to earlier failure` led the extractor to report `Rollout` instead of the real failed stage `Deploy`.
6. **`HealthCheckFailure.md` runbook + wiring** — new docops/ runbook with 6 ordered likely causes + verify commands; wired into `CLASS_DOCS["health_check"]` so the LLM gets it in the prompt automatically.
7. **`log_path` / `service_port` / `health_check_port` surfaced** — `_SLIM_FIELDS` in `mcp_tools.py` now exposes these so the LLM can give the operator EXACT instance-side paths/ports to check.
8. **Live verification** — build 25 (`stagger-prod-plus-devops-test`) re-RCA'd cleanly: `error_class=health_check`, `failed_stage=Deploy`, cites instance `i-02fc813e939bb2b39` + 50 iterations + concrete `ssh ... tail /var/log/blackbuck/<svc>.log` / `ss -tlnp | grep <port>` / `curl /admin/version` commands. Cost: $0.003 / 18K input tokens / 60s latency.
9. **Phase B shipped** — VictorOps incident `message` now carries `buildAlertMessage(rca)` (class/stage/confidence + summary + Finding/Action), and `details` panel adds structured `rcaErrorClass / rcaFailedStage / rcaConfidence / rcaSummary / rcaRequestId`. On-call sees the RCA inside the page itself — no need to click through to Jenkins console to know what failed.
10. **`buildAlertMessage` hardened** — handles both `suggested_fix` shapes (Map with Finding/Action keys, or plain String). Previously String-shaped fixes produced an empty alert body.
11. **Real config field resolution for health_check** — first live RCA emitted `<your-key.pem>`, `<instance-ip>`, `<log_path>`, `<health_check_port>` placeholders because `config.json` for `test-supply-wrapper-nonweb` had every canonical field (`log_path`, `service_port`, `health_check_port`) set to `null`. Root cause: the org uses different field names (`target_port`, `filebeat_log_path`, `key_name`, `server_command`). Fixed by:
    - `_SLIM_FIELDS` extended with the real-world names so `service_lookup` surfaces them.
    - `llm.py` `health_check.service_config` block resolves canonical → actual (`port` ← `target_port`; `log_path` ← `filebeat_log_path`; etc.), parses `-Dlog.dir=` out of `server_command` as a log-location hint, and derives `pem_path_hint = /var/lib/jenkins/.ssh/<key_name>.pem` from `key_name`. Any unresolved field shows as `NOT_IN_CONFIG` so the LLM SEES the absence rather than fabricating.
    - `prompts/rca_system.md` adds STRICT rule: NEVER emit `<placeholder>` strings; if `NOT_IN_CONFIG`, write a concrete discovery command (`ls /var/log/blackbuck/`, `ss -tlnp | grep java`, `aws elbv2 describe-target-groups`, `aws ssm start-session ...`) instead.
12. **BBCTL is the org-standard instance access tool** — RCA action items now use `bbctl shell <instance-id>` (interactive) and `bbctl run <instance-id> -- '<cmd>'` (one-shot, preferred for `suggested_commands`) instead of raw `ssh -i <key>.pem ubuntu@<ip>`. Wired into:
    - `prompts/rca_system.md` — STRICT BBCTL command rules (substitute real `instance_id` from `health_check.target`, never emit `<instance-id>` placeholders; `bbctl run` for one-shots, `bbctl shell` for interactive; SSM and raw ssh = fallback only).
    - `jenkins_rca/llm.py` — `health_check.guide` injects the org access pattern into the LLM prompt at runtime.
    - `docops/HealthCheckFailure.md` — new "Access pattern — use BBCTL" lead section + all verify commands rewritten to use `bbctl run`.
13. **Live verification (round 2)** — same build 25 re-RCA'd cleanly. Output now contains real values everywhere: `bbctl shell i-02fc813e939bb2b39`, port `7005` (resolved from `target_port`), log path `/var/log/blackbuck/test-supply-wrapper-nonweb.log` (org-standard pattern), health endpoint `/admin/version`. All `suggested_commands` tier `safe`. No raw `ssh`, no `<placeholder>`. Cost: $0.003 / 19K input tokens / 62s latency.
14. **Phase C wired (Slack)** — `Notification.rcaAlert(...)` static method added to `com.blackbuck.utils.Notification`; called from `main_stagger_prod_plus_one.groovy` post.failure right after the RCA webhook returns. Uses existing `slack-stagger-bot` Jenkins credential + per-service `config.slack_channel` (via `env["${SERVICE}:slack_channel"]`). No new Secrets Manager entry, no new Slack app, no webhook URL change — fully reuses existing org Slack infra. Orange-colored message lands in the same channel as the red `Notification.failure` so team sees failure + diagnosis side-by-side.
15. **HTML report endpoint** — new `GET /rca/v1/report/<request_id>` route in FastAPI; renders the stored audit JSON as a polished HTML page. `audit.read_by_request_id(uuid)` added (with uuid regex validation as path-traversal defence). `build_url` now captured in the audit record so the report can link directly. Same URL appears in Jenkins console / sidebar / Slack / VictorOps `details` (key `rcaReportUrl`) — one canonical, shareable surface across all channels.
16. **Compact Jenkins console** — `renderRca()` no longer dumps the full RCA into Jenkins console (~40 lines). New output is ~10 lines: header box, class / stage / one-line summary, full report URL, request_id. Rationale: operators don't have to scroll through a wall of text; the HTML report is one click away. Sidebar build description got a rich 2-line card with `Open RCA →` link.
17. **Dark-theme HTML report** — Polished UI: navy `#0a0f1c` background with dot pattern, sticky frosted-glass topbar, hero card with gradient, per-class colored pills (translucent with matching borders), dark code blocks for commands with Copy button, colored ✓/✗/? evidence badges, two-column grid for root cause + suggested fix. Self-contained CSS (no Tailwind / Inter CDN) so it renders correctly in restricted corporate networks. Header has no bot emoji — clean professional brand. Footer: `Built by Hariharan G, DevOps`.
18. **Confidence + cost + tokens hidden from operator UI** — `confidence` was a self-reported LLM score with no automation gates, so it was cosmetic. Removed from console box, sidebar description, Slack message, VictorOps details, and HTML report header. Still stored in audit JSON for retro analysis. Cost / token usage similarly hidden from the report header (operators don't care; finance can see in audit JSON).
19. **BBCTL scoped to instance-access classes only** — earlier prompt iteration was suggesting `bbctl run i-... -- 'git fetch'` for compliance failures (wrong tool — compliance is fixed in Jira, not on an instance). Prompt now restricts `bbctl shell` / `bbctl run` to: `health_check` (always); `java_runtime` / `network` / `ssm` (when stack trace points at an instance). Forbidden for: `compliance`, `scm`, `aws_limit`, `parse_error`, `canary_*` — those are operator-action failures (Jira UI / GitHub / AWS console / config edits).
20. **Compliance split into 5 distinct modes** — `prompts/rca_system.md` now has explicit Mode 1-5 guidance reading directly from `jira.tickets[].custom_fields["Signed Off Commit ID"]`:
    - **Mode 1** — missing Signed Off Commit ID (most common; matches `ERROR: Compliance: ... has no Signed Off commit id`)
    - **Mode 2** — commit-mismatch (uses the existing Option A / Option B template)
    - **Mode 3** — Jira ticket status not in allowed list
    - **Mode 4** — clone-of-clone chain detected
    - **Mode 5** — merged PR title missing the Jira ticket ID
    Prevents the previous "clone detection failed" hallucinations on logs where clone-detection actually passed.
21. **Jira REST API curl suggestion banned** — operator edits the Signed Off Commit ID field in the Jira UI (custom-field PUT via REST often requires special perms; UI is org-standard path). Prompt explicitly forbids `curl -X PUT 'https://blackbuck.atlassian.net/rest/api/2/issue/...'` in `suggested_commands` or prose.
22. **`SSH` / `ssh` wording banned from prose** — earlier output mixed `ssh -i ...` into Action prose even when commands used `bbctl run`. Prompt rule now: NEVER use `SSH` / `ssh` in prose; write `Use bbctl shell <instance_id>` or `Run bbctl run <instance_id> -- '<cmd>'`. `ssh ...` allowed only as a one-line fallback clause if BBCTL unavailable.
23. **Real config field resolution** — `_SLIM_FIELDS` extended to surface this org's actual config.json field names (`target_port`, `filebeat_log_path`, `key_name`, `server_command`, `aws_region`, `service_identifier`, `service_type`). `llm.py` `health_check.service_config` block now resolves canonical → actual names (e.g. `port` ← `target_port`, `log_path` ← `filebeat_log_path`), parses `-Dlog.dir=` out of `server_command` as a fallback log-location hint, and derives `pem_path_hint` from `key_name`. Unresolvable fields shown as `NOT_IN_CONFIG` so LLM sees the gap rather than fabricating `<placeholder>` strings.
24. **Unknown-class deep dive** — when classifier returns `unknown`, expand context: wider `source.trace` sweep (10 queries × 16 hits), full `docs.catalog` block listing every docops/*.md with first heading + 250-char preview, plus a 4-step `unknown_class.guide` telling the LLM to self-classify from source evidence + runbook previews. Marks `needs_deeper: true` when no fit is found.
25. **Model bumped: gpt-4o-mini → gpt-4o** — `jenkins_rca/llm.py` `run_rca_openai` now uses `gpt-4o` (full model). Reasoning quality on multi-step compliance / canary cases is markedly better. Cost calc in `main.py` updated to `$2.50/1M input + $10.00/1M output` (gpt-4o pricing). Typical RCA: ~$0.04-0.06 vs $0.003 before. Daily spend cap in `cache.py::over_daily_cap` still enforces.
26. **Live verification (compliance class, build 35)** — re-RCA cleanly cites Mode 1 ("Jira ticket PEB-7 is missing the 'Signed Off Commit ID' custom field"), Action template tells operator to edit the field in Jira UI (not REST API), Evidence includes both the `ERROR: Compliance: ... has no Signed Off commit id` log line AND the `jira.tickets` block confirming the missing custom field. No BBCTL commands. No clone-detection hallucination. No `<placeholder>` strings.
27. **Repos + docops auto-sync** — `infra/scripts/sync-repos.sh` + `/etc/cron.d/jenkins-rca-sync` keep the on-disk copies fresh without manual `git pull`. Pulls `jenkins_pipeline` (`master`), `InfraComposer` (`main`) via `git fetch && git reset --hard origin/<branch>`, syncs `docops/` from `s3://docops-doc-storage/docs/` via `aws s3 sync --delete`, then restarts `jenkins-rca` so the in-process `_config` cache reloads. Originally every 2h; can be relaxed to every 6h now that Phase E does per-RCA freshness pulls.
28. **Sync script self-heal** — script preemptively `chown -R ubuntu:ubuntu` + `chmod -R u+w` on both repo directories before every `git reset`. Fixes the loop where a previous `chmod -R a-w` (locking) made `git reset --hard` fail with `unable to unlink old <file>: Permission denied`. Same self-heal copied into `jenkins_rca/git_fresh.py` for the per-RCA path.
29. **Phase E shipped — hybrid git freshness + agent-mode RCA** — three coordinated changes (full architecture in the "Agent mode (Phase E)" section above):
    - **`jenkins_rca/git_fresh.py`** (NEW) — at the top of every `_run_rca` call, runs `git fetch --depth 1 && git reset --hard origin/<branch>` on both repos in parallel. 3s timeout per repo, 60s in-memory dedup, falls back silently to whatever's on disk if GitHub is slow / offline. Self-heals permissions every run.
    - **`jenkins_rca/agent.py`** (NEW) — OpenAI function-calling loop. Tool palette: `get_jenkins_job_config`, `repo_read_file`, `repo_search`, `repo_list_dir`, `repo_find_function`, `repo_recent_commits`, `service_lookup`. Max 8 tool calls, $0.25 cost cap, 8 KB per-tool truncation. Dispatched for: `compliance`, `canary_fail`, `canary_script_error`, `health_check`, `parse_error`, `scm`, `unknown`. Other classes stay on the cheaper one-shot path.
    - **`prompts/rca_agent_system.md`** (NEW) — instructs the agent to start from `get_jenkins_job_config(job)`, read the entrypoint pipeline file, locate the failed stage block, `repo_find_function` each helper it calls, recurse one or two levels until it identifies the file:line that emitted the log error. Evidence citations now use real `<repo>/<file>:<line>` from the actual reads — grounded, not inferred.
30. **Tool palette extensions in `mcp_tools.py`** — three new helpers backing the agent loop:
    - `repo_list_dir(repo, path)` — list immediate children of a directory (trailing `/` on dirs).
    - `repo_find_function(repo, name)` — ripgrep tuned for `def <name>(` / `static def <name>(` / `<name> = { ... }` patterns across Groovy/Java/Python. Returns the definition site (not call sites).
    - `repo_recent_commits(repo, n)` — `git log -nN --pretty=...` so the agent can answer "what changed?". Often pinpoints a freshly-landed commit as the cause of a previously-green pipeline now failing.
    - `repo_read_file` tightened to return REAL (1-based) file line numbers regardless of the `start` offset — agents can paste those line numbers into `evidence[].source` verbatim.
31. **Jenkins job XML fetcher** — `jenkins_rca/jenkins.py::get_job_config(job)` GETs `/job/<name>/config.xml` and tolerantly regex-extracts `scm_url`, `scm_branch`, `scriptPath` (or `inline_script` if the pipeline isn't loaded from SCM). The raw XML is capped at 8 KB to keep prompts lean. This is almost always the agent's first tool call — it tells the agent which file in `jenkins_pipeline` is the actual entrypoint for the failing job (handles new jobs / non-standard pipelines automatically without naming-convention guessing).
32. **Cost / latency expectations under Phase E** — agent typically uses 4-6 tool calls per RCA. Token usage ~25-35K input + ~600-900 output → **~$0.07-0.10 per RCA** (vs ~$0.05 for the previous one-shot path). Worst case at the cap: $0.25. Latency: ~60-90s typical (small overhead vs one-shot since most tool calls are local disk reads). Daily cost cap (`cache.py::over_daily_cap`) still enforces an upper bound.
33. **HTML report route order fix** — earlier `GET /v1/report/{id}.json` 404'd silently because the catch-all HTML route `/v1/report/{request_id}` was registered first and greedily matched `<uuid>.json` (with `.json` baked into `request_id`), then failed the uuid regex inside `read_by_request_id`. Fix: register the more-specific `.json` route BEFORE the HTML route in `jenkins_rca/main.py`. FastAPI evaluates routes in declaration order. Comment added on the route block to prevent future re-breakage.
34. **HTML report — Metadata card dropped** — request_id / provider / redactions / log_window_chars / recorded_at no longer rendered in the HTML. Audit JSON still has them via `/v1/report/<uuid>.json` for debugging. Page now ends Evidence → Footer.
35. **Phase E tuning round 1 — cost trim attempt** — initial agent runs cost-capped at $0.30 because every iteration re-sent all prior tool results. Three knobs added in `jenkins_rca/agent.py`:
    - `MAX_TOOL_CALLS`: 8 → 6 (each iter still allows multiple parallel tool calls; 6 iters = typically 5–8 individual calls).
    - `PER_TOOL_RESULT_CAP`: 8 KB → 3 KB (tightens runaway grep / file reads).
    - `_elide_old_tool_results(messages, current_iter, keep_recent)` — new helper that walks backwards through `messages`, counts assistant turns, and replaces tool-result content from iterations older than `(current_iter - keep_recent)` with `[elided to save tokens — see earlier reasoning]`. Preserves tool_call_id chain so OpenAI still threads the conversation; just drops heavy bodies.
    - `TRIM_HISTORY_AFTER`: initially 2. Net: $0.30 → $0.27 (~10% cut, not enough).
36. **Phase E tuning round 2 — cost trim deeper + system-message prompt cache** — second iteration in `agent.py`:
    - `PER_TOOL_RESULT_CAP`: 3 KB → 1.5 KB.
    - `TRIM_HISTORY_AFTER`: 2 → 1 (only the last iteration's tool bodies kept full; older iters elided).
    - **Primer merged into the system message.** OpenAI auto-caches the longest static prefix across consecutive completions; the user message shrunk to a one-line kick-off so the system+primer prefix stays stable for cache hits. The primer itself was reshuffled — a new `## RESOLVED VALUES — substitute these VERBATIM` block appears at the TOP of the system message, with `health_check.target` + `health_check.service_config` JSON blocks pulled out of `_build_tool_context` via heuristic regex (see `_format_resolved_values` in `agent.py`). Goal: agent never has to hunt through the primer for instance_id / port / log_path.
    - Net: $0.27 → $0.21 (~20% cut from baseline).
37. **Log-path fallback** — `jenkins_rca/llm.py` `health_check.service_config` block now falls back to the org pattern `/var/log/blackbuck/<service>.log` when `filebeat_log_path` is empty string OR null. Adds `log_path_source` field (`filebeat_log_path/log_path` | `server_command -Dlog.dir hint` | `org default /var/log/blackbuck/<service>.log`) so the LLM can see WHERE the value came from. Previously empty string masqueraded as `NOT_IN_CONFIG` and agents emitted `/var/log/blackbuck/<log_path>` literal placeholders or hallucinated other services' log filenames (e.g. `gps.log` for `test-supply-wrapper-nonweb`).
38. **Hallucination guard in agent prompt** (`prompts/rca_agent_system.md`):
    - Explicit "HALLUCINATION GUARD — common wrong values to AVOID unless they literally appear in the primer" list: never default to `gps.log`, port `8080`, `/admin/version`. Always pull from the resolved-values block.
    - "If you find yourself writing a value that doesn't appear verbatim in the resolved-values block, STOP and re-read the primer."
39. **Wandering avoidance in agent prompt** — three new rules to stop the agent from burning iterations on `repo_list_dir` exploration:
    - Don't `repo_list_dir` unless you genuinely don't know where to look.
    - Don't call the same tool twice with identical args.
    - After `get_jenkins_job_config` + one entrypoint read, jump straight to `repo_find_function` → read helper. No directory listing.
40. **Mandatory repo-evidence rule** — agent prompt now contains a STRICT requirement: if `repo_read_file` was called at least once during the trace, the final `evidence` array MUST contain at least one repo-path source (`<repo>/<file>:<line>` that the agent actually read). Reading files and citing only `jenkins_log` wastes the trace and budget.
41. **Tolerant final-JSON parser + force-final schema injection** — observed gpt-4o emitting a markdown report (`### Summary\n### Failed Stage\n...`) instead of JSON when a prior tool call errored mid-trace, even with `response_format={"type":"json_object"}`. Two coordinated fixes:
    - New `_parse_final_json(text)` in `agent.py` handles three real-world output shapes: pure JSON, markdown-fenced JSON (` ```json … ``` `), and prose with an embedded `{...}` block (extracts via first-to-last brace).
    - New `_FORCE_FINAL_PROMPT` constant injected on both force-final and cost-cap paths. Contains the full RCA schema inline plus explicit rules: "NOT markdown, NOT ###headings — ONLY a JSON object", "If a tool errored earlier, that's fine — use the context you already have to compose the JSON", "Output the JSON object only — no prose before or after". Raw `final_text` is now logged to stderr on parse failure for debugging.
42. **Live verification (Phase E end-to-end on build 30)** — fresh repos pull (`jenkins_pipeline @ 33c2ead0`, `InfraComposer @ da00810`) confirmed via `repos_freshness` audit field. Agent traced through `prodPlusOne.groovy:15 → deployProdPlusOne.groovy:56 + :99`. Evidence array carries real repo-path citations. `agent_tool_calls` ranges 6–8 per RCA. Cost band $0.20–$0.25 typical, $0.25 cap rarely hit since round-2 trim landed.
43. **`compliance` removed from `AGENT_CLASSES`** — build #38 (`iam-authentication`, compliance class) hit the `"Agent failed to emit valid JSON"` fallback stub. Root cause: compliance failures are pure Jira-field-missing problems (no `Signed Off Commit ID` set). The agent loop had zero source code to trace — primer already carries `jira.tickets`, runbook, and mode 1-5 guidance — so the agent drifted and emitted a markdown `### Summary / ### Root Cause` report instead of JSON, even with `response_format={"type":"json_object"}`. Phase E had promoted compliance to the agent loop alongside `canary_*`, `health_check`, etc., but compliance gets nothing useful from tool calls. Fix in `jenkins_rca/main.py::_run_rca`: removed `"compliance"` from `AGENT_CLASSES` set so compliance routes back to the one-shot `run_rca` path (which uses the mode 1-5 prompt + tight JSON schema). Per-RCA git freshness still runs before class dispatch (`ensure_fresh_many` on line 215), so the one-shot path still benefits from fresh repos — though compliance doesn't actually need them. Verified on build #39: clean Mode 1 output citing `PEB-7 missing 'Signed Off Commit ID' (customfield_10973)`, Action template tells operator to edit the field in Jira UI and paste the real 40-char SHA from Jenkins log's `COMMIT_ID` env, Evidence array has both `jenkins_log` and `jira.tickets` sources, no `<placeholder>` strings, no markdown fallback.
44. **HTML report — drop `<strong>` on author name in footer** — minor UI tweak in `jenkins_rca/templates/rca_report.html` per operator preference. Footer now reads `Built by Hariharan G, DevOps` (plain).
45. **BB-AI Auto-RCA wired into `create-quick-infra` pipeline** — second pipeline gets the same post.failure RCA card + Slack notify as `main_stagger_prod_plus_one`. Differences from the stagger wiring:
    - VictorOps **intentionally skipped** — `create-quick-infra` is interactive / dev-triggered, not a page-worthy production deploy path.
    - RCA fires BEFORE the `input message: 'Destroy provisioned infra?'` prompt so the operator sees the diagnosis in console + Slack while deciding whether to roll back the EC2s.
    - Slack channel resolution prefers `effectiveParams.slack_channel` (loaded from `config.json` by the early Setup stage); falls back to `env["${SERVICE}:slack_channel"]` for parity.
    - Wrapped in `try/catch` — any RCA / Slack failure echoes but does not block the rollback prompt or `rollbackInstances` call.
46. **`unknown` removed from `AGENT_CLASSES`** — build #5 (`create-quick-infra-devops-test`) classified `unknown` and hit the agent loop, which then drifted (same failure mode as compliance: nothing concrete to trace) and emitted a markdown report instead of JSON. Fix: drop `"unknown"` from the set in `main.py::_run_rca` so it routes to the one-shot path. The one-shot path already has the `unknown_class.guide` + wide source.trace + `docs.catalog` previews + 4-step self-classify recipe, which is strictly better for catch-all cases. After this fix, the agent loop is reserved for classes that genuinely benefit from in-repo code-tracing: `canary_fail`, `canary_script_error`, `health_check`, `parse_error`, `scm`.
47. **Jenkins shared-lib `parseJson` deep-converts LazyMap → LinkedHashMap** — `vars/triggerRcaWebhook.groovy::parseJson` previously returned `groovy.json.internal.LazyMap` straight from `JsonSlurper`. LazyMap is **not Serializable**. In pipelines like `create-quick-infra` that pause at an `input` step after calling `triggerRcaWebhook()`, Jenkins checkpoints CPS program state to disk, and serialization of the still-in-scope `rca` variable crashes with `NotSerializableException`, taking down the entire post-condition block (no rollback, no destroy prompt, no exit). Fix: add `toSerializable()` helper (also `@NonCPS` so internal `Lazy*` types never touch the CPS heap) that recursively rebuilds Maps as `LinkedHashMap` and Lists as `ArrayList` — both are plain `Serializable` JDK types. Stagger pipeline doesn't hit this only because it has no `input` step between RCA call and end.
48. **Classifier extended to catch Groovy/pipeline-DSL exceptions** — `groovy.lang.\S+Exception`, `No signature of method:`, `unable to resolve class` added to `java_runtime` class in `classifier_rules.yml`. Reason: Groovy DSL exceptions raised by Jenkins shared libraries (e.g. `MissingMethodException` when a `vars/Foo.groovy` is called with the wrong arg count) are JVM-level method-resolution / type errors at the same layer as `java.lang.*` exceptions, and benefit from the same "read the stack trace, cite the file:line" handling. Before this, the classifier returned `unknown` for these failures and the LLM drifted into an `scm` narrative because of a `release/REQ-...` branch name in the log.
49. **`unknown_class.guide` rewritten — STRICT rules against runbook narrative borrowing** — observed regression on build #6 (`create-quick-infra`): pipeline failed with `groovy.lang.MissingMethodException: No signature of method: JiraDetails.call(String)` at `WorkflowScript:330` (the line was calling `JiraDetails(ticket)` with 1 arg; the var signature requires `(SERVICE, COMMIT_ID, ticket)`). Classifier returned `unknown` (because log window lacked the `groovy.lang.*` exception line — see item 50 for the timing reason). LLM in one-shot unknown mode followed the OLD guide's "Pick best-fit `error_class` from the enum", matched `Loading library staggered_plugins@release/REQ-463-staggerprodplusupdate-v2` to scm-style branch-access wording, picked `scm`, and templated suggested_fix as a PR-merge-access RCA — completely fictional. Fix in `jenkins_rca/llm.py::_build_tool_context`: dropped the forced-enum-pick step; added 5 STRICT rules:
    - **Stack trace is ground truth.** Cite the file:line (`WorkflowScript:330`, `vars/Foo.groovy:42`, `i-abc /var/log/.../bar.log:99`) from the LOG in `root_cause` + `evidence`.
    - **Interpret exception type literally.** `MissingMethodException: No signature of method: X.call(...) is applicable for argument types: (...)` = wrong arg count or types to a Groovy method, NOT network/auth/repo-access/compliance. Same for `MissingPropertyException`, `NullPointerException`, `ClassNotFoundException`.
    - **Do NOT borrow runbook narratives.** Even if `docs.catalog` lists a doc whose heading vaguely matches a keyword in the log (e.g. `REQ-` in a branch name → SCM access), DO NOT use that doc's remediation unless its FAILURE pattern matches the actual exception.
    - **Pick `error_class` from what actually happened**, not from runbook availability. `java_runtime` is correct for Groovy / JVM-level method-resolution / type errors in pipeline DSL or shared-library code.
    - **"Cannot determine from log window alone" + `needs_deeper:true` is INFINITELY better than a confident wrong answer.**
50. **Service-side: fetch FAILED stage `error.message` via Jenkins `wfapi/describe`** — generic fix for the timing gap. When `triggerRcaWebhook()` fires from inside a `post.failure` block, Jenkins has NOT yet emitted the trailing exception trace (`Also: hudson.remoting.ProxyException: groovy.lang.MissingMethodException ... at WorkflowScript:330` is appended AFTER the post block completes). So `consoleText` returned a log without the exception, the classifier fell through to `unknown`, and the LLM drifted (see item 49 for the downstream effect). Fix in `jenkins_rca/jenkins.py`: added `get_stage_errors(job, build)` that calls `/job/<job>/<build>/wfapi/describe` — Jenkins populates each stage's `error.message` as soon as the stage transitions to FAILED, independent of console-buffer flush timing. In `main.py::_run_rca`, the failed-stage error messages are now prepended to the log window before `classify()` + LLM call, so both see the real exception type regardless of console-buffer timing. Endpoint is tolerantly handled — if `wfapi` is unavailable (older Jenkins or non-Pipeline jobs), `get_stage_errors` returns an empty list and the flow falls through to the existing console-only path. **This is the generic fix that makes Auto-RCA work for the entire family of pipeline-DSL / library / shell-exit-code / SSM / health-check / canary failure modes, not just the build #6 MissingMethodException case.**
51. **Pipeline-specific runbooks staged for S3 docops bucket** — four new `.md` runbooks staged in `BBCTLLLM/s3_docs/docs/` to be uploaded to `s3://docops-doc-storage/docs/`. They give the LLM pre-digested guidance for the four other production pipelines beyond `main_stagger_prod_plus_one`:
    - **`CreateQuickInfra.md`** — onboarding / fresh-infra pipeline. Documents the 9-stage flow (Load Library → Jira Details → Resolve Parameters → Input Validation → Build → Build Frontend → Infra → Deploy → Deploy Frontend), the `IS_ONBOARDED=Yes/No` branching in `Resolve Parameters`, the post.failure `input message: 'Destroy provisioned infra?'` prompt pattern, and stage-by-stage failure patterns (config.json drift, JiraDetails arg-count, Terraform capacity errors, health check timeouts).
    - **`HotfixNoncanary.md`** — emergency-deploy pipeline with no canary. Explicitly calls out the **no Kayenta scoring, no traffic stagger, no Rolling Back safety net** caveat. Documents the 9-stage flow (… → Build Artifact → Pre-Deployment → Instance Provisioning → Artifact Deployment → Health Validation → Cutover & Cleanup), the `hotfix_rollback()` post-failure helper, and what happens if `Cutover & Cleanup` half-completes (manual ALB recovery).
    - **`StaggerProdPlusOneFrontend.md`** — frontend variant of the canary-gated stagger pipeline. Uses `staggered_plugins_fe@stagger-fe-temp` (not `staggered_plugins`), 8 stages including `prodPlusOneFrontend` + 3-arg `deploy(SERVICE, "prod", COMMIT_ID)`, and `frontendRollback` (CloudFront invalidation + S3 version revert) in post.failure. Cross-refs `StaggerProdPlusOneDeploy.md` for the shared canary mechanics.
    - **`StaggerNonweb.md`** — nonweb backend stagger deploy. Documents why it skips Prod+1 + Automation (no HTTP traffic to validate), 2-arg `deploy(SERVICE, "prod")`, and which canary checks are meaningful (`Other-*`) vs which return nodata (`Web-*`) when `service_type=nonweb`.
    - **Upload flow**: staged on dev workstation in `/Users/hariharan/cost_exp_aibot/BBCTLLLM/s3_docs/docs/`. Operator uploads via `aws s3 cp` (or `aws s3 sync`) using a non-`*-main` profile (org rule: never use admin/main creds for routine ops). Cron `/etc/cron.d/jenkins-rca-sync` runs `aws s3 sync s3://docops-doc-storage/docs/ /opt/jenkins-rca/docops/ --delete` then `systemctl restart jenkins-rca` so the in-process docs cache reloads. Max sync wait = cron interval (2h currently; can relax to 6h since per-RCA freshness covers repos but docops is sync-only). Force-fresh: `sudo /opt/jenkins-rca/infra/scripts/sync-repos.sh`.
    - **Note on completely new pipelines**: even without a runbook, the agent loop (`canary_fail`, `canary_script_error`, `health_check`, `parse_error`, `scm` classes) calls `get_jenkins_job_config(job)` → reads the entrypoint `.groovy` via `repo_read_file` → traces helpers via `repo_find_function`. Runbooks are pre-digested guidance, not a hard requirement — they make the agent faster + more certain but the code-trace path works without them.
52. **Wire BB-AI RCA on FAILURE + UNSTABLE + NOT_BUILT across all 5 pipelines** — original wiring fired RCA only from `post.failure`. Jenkins's `failure` block runs ONLY when `currentBuild.currentResult == 'FAILURE'`. A precheck `error "..."` that gets caught and converted to NOT_BUILT (e.g. `checkTerraformStateFile` or instance-count precheck flipping the build result) skipped `post.failure` entirely — no RCA, no Slack, no VictorOps. Same gap for UNSTABLE. Observed on `test-supply-wrapper-nonweb` prod-plus-one run: `ERROR: An error occurred during prechecks: instance count is not matching the number of instances healthy in green` → `Finished: NOT_BUILT`. Operator saw nothing in BB-AI. Jenkins has no `not_built` post block, so NOT_BUILT must be caught in `always { if (currentResult == 'NOT_BUILT') }`. Applied per-result wiring to all 5 production pipelines (main_stagger_prod_plus_one, stagger-nonweb, stagger-prod-plus-one-frontend, create-quick-infra, hotfix-noncanary): `failure` keeps existing rollback + RCA call; `unstable` adds new RCA-only call; `always` block adds NOT_BUILT-only RCA call. Service-side dedup (`is_duplicate(job, build)`) keeps double-fires safe. ABORTED is intentionally NOT covered (operator-cancelled). `hotfix-noncanary` RCA was never wired — now is.
53. **`java_runtime` added to `AGENT_CLASSES`** — stack-trace-bearing errors benefit from the agent's `repo_read_file` tool. Agent can locate `WorkflowScript:330` → resolve to `create-quick-infra.groovy:330` via `get_jenkins_job_config` → cite the wrong-arg call line directly. One-shot path lacked tool access, only paraphrased the stack trace text. Cost impact: +$0.10-0.15 per java_runtime RCA (~3-4 tool calls typical).
54. **`JENKINS_RCA_MODEL` env override + per-model pricing table** — operators can A/B-test models without code change (`sudo systemctl set-environment JENKINS_RCA_MODEL=gpt-5`). New `_MODEL_PRICING` dict in `jenkins_rca/agent.py` covers gpt-4o, gpt-4o-mini, gpt-4.1, gpt-4.1-mini, gpt-5, gpt-5-mini, o1, o1-mini, o3-mini. Unknown models fall through to gpt-4o pricing (conservative). Cost cap + audit `cost_usd` + new `model_used` field all honor the configured model. `llm.py` one-shot path + `main.py` cost calc + `agent.py` all read the same env var.
55. **Default model switched from `gpt-4o` to `gpt-4.1`** — gpt-4.1 (Apr 2025) provides better reasoning + 1M context window at a slightly cheaper price ($2/$8 vs $2.50/$10 per 1M tokens). Live A/B testing on create-quick-infra build #13 showed gpt-4.1 reading more files (2 vs 1) and emitting better-structured "needs_deeper" output than gpt-4o at lower cost ($0.11 vs $0.12 for same trace depth). Override via env if needed.
56. **Voluntary-bail rescue retry with JSON constraint** — when agent LLM stops emitting tool_calls mid-loop (it thinks it has enough info to finalize), the existing code path was NOT bound by `response_format=json_object` — that constraint only applied to the iter-6 force-final path. So LLM was free to emit a markdown report on natural completion → trips fallback stub. Fix in `agent.py`: when bailing voluntarily, validate the response. If `_parse_final_json` returns None, do ONE retry with `response_format=json_object` + `_FORCE_FINAL_PROMPT`. Worst case +$0.05 but rescues otherwise-wasted prior tool calls.
57. **Honesty rule + force-final operator-language ban** — observed agent output saying `summary: "...cause not determinable from 4 tool calls"` and `root_cause: "Agent budget exhausted before reaching implementation site"`. Internal jargon leaked to operator. Rewrote `_FORCE_FINAL_PROMPT` to: (1) bias toward `needs_deeper:true` over confident wrong answers when context is thin; (2) ban operator-unfriendly internal terms ("agent budget", "tool calls", "iterations", "implementation site"); (3) explicitly allow log-self-sufficient answers (e.g. `MissingMethodException` log line names the fix → cite that without claiming `needs_deeper`).
58. **Graceful handling for OpenAI / LLM call failures** — without this, an OpenAI 403 PermissionDenied (e.g. `JENKINS_RCA_MODEL=gpt-5` on a project that doesn't have access) propagated up as an unhandled exception → FastAPI HTTP 500 with HTML stack trace → Jenkins post.failure block got `parse error: Invalid numeric literal` from jq. Now `_run_rca` wraps both agent + one-shot in `try/except`. On any LLM-side failure builds a stub RCA with `_llm_error: true`, hint about model-access ("Model X is not available in this OpenAI project. Verify with..."), and `needs_deeper: true`. Stub still goes through audit + cache + render path so operator sees a clean BB-AI card in Jenkins console + HTML report URL with the diagnostic, not a 500 page.
59. **Dashboard view — browse all recent RCAs grouped by pipeline** — two new endpoints: `GET /rca/v1/dashboard` (landing — pipeline cards/table, last 2 days) and `GET /rca/v1/dashboard/<job>` (per-pipeline build list). Landing groups audit records by `job`, sorted by most-recent-failure DESC. Each row: job name + failure count + latest summary preview + class chips. Click → per-pipeline view → "Open RCA →" button → existing `/v1/report/<request_id>`. Data source: `audit.list_recent(days=2)` scans `/var/log/jenkins-rca/*.json`, filters by `recorded_at >= now - 48h`, dedups by `(job, build)` keeping latest (later commit). Time window configurable via `?days=N`. Header has logged-in-as pill + Sign out. Existing report header gets breadcrumb back to dashboard.
60. **Google OAuth SSO for dashboard (`@blackbuck.com` only)** — new module `jenkins_rca/auth.py` implements server-side Google OAuth web flow with starlette `SessionMiddleware` for cookie-signed sessions. Routes: `/v1/auth/login`, `/v1/auth/callback`, `/v1/auth/logout`. Dashboard routes (`/v1/dashboard*`) gated by `Depends(oauth.require_auth)` — on no/invalid session, raises 307 to `/v1/auth/login` with `next` query param. Domain restriction: `JENKINS_RCA_ALLOWED_DOMAIN` env (default `blackbuck.com`). Google `hd` param hints the consent UI to the workspace domain (UX); server-side post-callback verifies email suffix (security-binding). Per-build report (`/v1/report/<uuid>`) + webhook stay open (UUID-gated deep-link shareable in Slack/Jenkins, HMAC-gated webhook). Auth is OPT-IN: if `JENKINS_RCA_OIDC_CLIENT_ID` env unset, `require_auth` returns placeholder user and dashboard is open (dev mode). New dep: `itsdangerous`. GCP setup: Web Application OAuth client with redirect URI `https://jenkins-rca.jinka.in/rca/v1/auth/callback`. Cookie: 24h max-age, signed with HMAC, `https_only`, `same_site=lax`.
61. **Canonical host renamed `bbctl.blackbuck.com` → `jenkins-rca.jinka.in`** — matches the ALB rule (`host=jenkins-rca.jinka.in + path=/rca/*` → `jenkins-rca-tg`). Updated across `jenkins_rca/main.py` comments, `infra/jenkins/post_failure_rca.groovy` defaults (JENKINS_RCA_URL, JENKINS_RCA_REPORT_BASE_URL), `jenkins_pipeline/vars/triggerRcaWebhook.groovy`, `main_stagger_prod_plus_one.groovy`, `src/com/blackbuck/utils/Notification.groovy`, and `docs/rca/*.md`. bbctl CLI's separate `BackendURL` in `internal/config/` untouched.
62. **URL-decode job names at all entry points** — dashboard showed two rows for the same Jenkins job: `Stagger Prod Plus One` (real Jenkins-webhook-driven) and `Stagger%20Prod%20Plus%20One` (manual curl test with URL-encoded JSON). Clicking the encoded row triggered browser double-encode (`% → %25 → %2520`) → empty per-pipeline view. Fix in 3 places: `_run_rca` unquotes `job` at entry (cache key + audit file converge on decoded form); `/v1/dashboard/{job}` path unquoted; `audit.list_recent` unquotes legacy encoded records on read.
63. **Dedup audit records by `(job, build)` — keep latest only** — operator re-triggering same build with `deep:true` writes a fresh audit record per call. Dashboard was showing all of them as separate rows (e.g. `#5056` appeared 3 times). After sort-by-time-desc in `audit.list_recent`, keep only first occurrence per `(job, build)` tuple. Newer RCAs supersede older for the same build.
64. **Jira ticket deep-link button in report header** — extract first Jira ticket key (e.g. `MB-7545`) from `rca.summary` + `rca.root_cause` + `rca.evidence` snippets via `jira.extract_tickets` regex. Build URL `<JENKINS_RCA_JIRA_URL>/browse/<key>` (default `https://blackbuck.atlassian.net`). When key found, render extra pill-link in report hero alongside Jenkins build / Console log / Raw JSON. Opens in new tab.
65. **Light / dark theme toggle + modern formal UX refresh** — shared theme tokens in `jenkins_rca/static/theme.css` (CSS variables with `[data-theme="light"]` override) + `theme.js` toggle button. Static mount in main.py at `/static` and `/rca/static`. System preference detected via `prefers-color-scheme`; explicit user pick wins via localStorage (key `jenkins-rca-theme`). Inline bootstrap in every template `<head>` sets `data-theme` BEFORE paint to avoid flash-of-wrong-theme. All 3 templates (dashboard, pipeline_builds, rca_report) wired. Formal copy ("Sign out", banned emojis). Dashboard renamed `Jenkins Error RCA` → report header `Jenkins RCA` with BlackBuck logo.
66. **Compact "Open RCA" button** — button was wrapping to 2 lines when table column was narrow. Added `white-space: nowrap` + reduced padding (6/14 → 5/12). New `.col-actions` class pins column to content width (`width: 1%`).
67. **`JENKINS_RCA_DEBUG_PROMPT` env writes last prompt to `/tmp` file** — off by default. When set, every RCA writes the exact system + user message it sends to OpenAI to `/tmp/jenkins-rca-last-prompt.txt` for inspection. Operators: `sudo systemctl set-environment JENKINS_RCA_DEBUG_PROMPT=1 && sudo systemctl restart jenkins-rca`, trigger any RCA, then `cat /tmp/jenkins-rca-last-prompt.txt`. Disable when done: `unset-environment + restart`. Both `llm.py` (one-shot) and `agent.py` (agent) honor the env var. File has 4 sections: MODEL, MODE, SYSTEM MESSAGE (includes primer), USER MESSAGE (+ TOOLS SCHEMA for agent mode).
68. **MANDATORY source cross-check for all agent-mode classes** — agent was skipping `repo_read_file` when log appeared self-sufficient (e.g. `MissingMethodException` with `Possible solutions: call(...)` in error msg). Saved ~$0.05 per RCA but operator-facing evidence array had only `jenkins_log` entries — no permanent code citation. New section in `prompts/rca_agent_system.md` ("MANDATORY source cross-check") with per-class read plan: java_runtime → caller + helper file; health_check → `nonwebdeploy.groovy` + `healthy.sh`; canary_fail → `rollout.groovy` + `canary.py`; canary_script_error → `canary.py` deepest frame; scm → `triggerRcaWebhook.groovy`; terraform → `main.tf` + module; parse_error → `config.json`. `_FORCE_FINAL_PROMPT` updated — removed "log is self-sufficient → skip source read" shortcut, replaced with "MUST open caller + helper file even when error msg names the fix". Cost impact: +1-2 tool calls per agent RCA, +$0.03-0.05. Operator-facing payoff: evidence now includes verified code citations (e.g. `create-quick-infra.groovy:330` + `vars/JiraDetails.groovy:9`) operator can navigate to.

---

## Option C migration (branch `feature/jenkins-rca-agent-only`)

After item 68, manager review of build-15 (compliance) trace flagged that the LLM was receiving fully pre-fetched context (Jira tickets, runbook, GitHub commits) in the user message and emitting a verdict on the first turn — i.e. the LLM "decided nothing." Items 69–85 record the migration to **Option C — agent-only**: every RCA goes through the agent loop, the LLM receives only `log_window + build_meta + service.lookup`, then chooses which tools to call (Jira API, GitHub API, AWS API, repo file, runbook). All work lives on branch `feature/jenkins-rca-agent-only`; cutover plan in `docs/rca/agent_mode_migration_plan.md`, flow walkthrough in `docs/rca/flow_option_c.md`.

69. **Plan, flow, and IAM setup docs** — `docs/rca/agent_mode_migration_plan.md` (locked architecture, 8-phase implementation roadmap, cost/latency projections, acceptance criteria); `docs/rca/flow_option_c.md` (end-to-end 30,000-ft sequence diagram, boot-pack shape, per-class worked drill examples, stopping rules, output artifact list); `docs/rca/aws_iam_manual_setup.md` (step-by-step console walkthrough — host role + cross-account `BBCTLRcaReadOnly` in three target accounts); `docs/rca/policies/*.json` (three copy-paste IAM JSON files for the host role + target trust policy + SSM inline). Manual IAM (no Terraform module) — simpler one-time setup. Smoke-test command block in the IAM doc verifies STS AssumeRole + describe call works from EC2.

70. **AWS IAM cross-account roles created** — Account map locked: zinka=735317561518 (host, where jenkins-rca runs as `bbctl-backend-service`), bbfinserv=075903075452, divum=597070799581, tzf=476114138058. Host role gets `ReadOnlyAccess` managed policy + inline `jenkins-rca-ssm-send` (later removed for Option C) + inline `jenkins-rca-cross-account-assume` listing the three target ARNs. Each target account has `BBCTLRcaReadOnly` role with trust on the specific host role ARN (not whole account) + `ReadOnlyAccess` + same SSM inline (later removed). SSM policy uses `Resource: [arn:aws:ssm:*::document/AWS-RunShellScript, arn:aws:ec2:*:*:instance/*]` — IAM-layer restriction since SSM has no DocumentName condition key. Smoke test (`aws sts assume-role` for each target) returned valid creds for all three.

71. **Phase 1 — system prompt v2 + 10 runbook MDs + 19 tool schemas** — `prompts/rca_agent_system.md` rewritten from 500-line one-shot prompt to a 50-line generic method + schema + evidence rules. Per-class drill plans moved to `docops/runbooks/*.md` (compliance, parse_error, java_runtime, health_check, canary_fail, canary_script_error, terraform, scm, aws_limit, unknown). New `jenkins_rca/tool_schemas.py` holds the 19 OpenAI function-calling schemas in one place (domains: Jira×2, GitHub×4, local repo×4, jenkins×1, runbook×2, AWS×5, sanity×1). Validation: all 19 schemas conform to OpenAI's `{type: "function", function: {name, description, parameters}}` shape.

72. **Phase 2 — 8 NEW tool functions, folded into existing modules** — instead of a wrapper file, extended the natural homes. `jira.py`: + `search(jql, max)` for JQL search. `github.py`: + `find_pr_for_commit(repo, sha)`, + `read_file(repo, path, ref, start, end)` via Contents API + base64 decode, + `recent_commits(repo, branch, n)`. `mcp_tools.py`: + `list_runbooks()` (walks `DOCS_DIR/runbooks/*.md`, extracts summary from `## What this class means`), + `read_runbook(name)`. New `jenkins_rca/agent_dispatch.py` has a single `TOOL_DISPATCH` map: OpenAI tool name → callable, imported from natural modules. Same creds, same cache, no duplicate code.

73. **Phase 3 — agent loop wired to schemas + dispatch + bumped caps** — `jenkins_rca/agent.py`: replaced 200-line inline `TOOLS` list with `tool_schemas.TOOLS` import; rewrote `_dispatch_tool` to use generic `TOOL_DISPATCH` resolver with sync/async auto-detection (coroutines awaited via `inspect.iscoroutine`); kept two special cases that need per-RCA `ctx` (`get_jenkins_job_config` for Jenkins creds, `service_lookup` for output formatting). Caps bumped (env-overridable): `MAX_TOOL_CALLS` 6→25, `COST_CAP_USD` $0.25→$5.00 (panic killswitch only), new `WALL_CLOCK_SEC=180`. Wall-clock cap added at top of every iter so Jenkins post-block doesn't hang. `main.py`: opt-in env `JENKINS_RCA_FORCE_AGENT_MODE=1` routes EVERY error_class through agent (compliance, parse_error, aws_limit, etc.), drops the one-shot primer-stuffed path. Default off during soak.

74. **Phase 3 — minimal primer + revised user-kickoff** — `_build_primer()` gated on `JENKINS_RCA_FORCE_AGENT_MODE`. In Option C mode emits ONLY 3 boot-pack blocks: `build_meta` (job/build/service/result/url), `service.lookup` (target_port, aws_account, aws_region, rule_arn, log_path, …), `log_window` (sanitized). Drops `initial_tool_ctx` entirely (the previous primer's pre-fetched Jira/GitHub/runbook content), drops `RESOLVED VALUES` block, drops `error_class` hint, drops `detected_failed_stage` — LLM identifies the failed stage itself from `[Pipeline] { (X)` markers. User kickoff rewritten from "Don't re-fetch what's already there" to "The primer above contains ONLY log_window, build_meta, and service.lookup. You MUST call tools to fetch everything else." Build-15 retry: 6 tool calls, real `vars/JiraDetails.groovy:9` citation, no hallucinated paths.

75. **Phase 3 — system prompt cutover + 2d→7d dashboard window** — replaced active `prompts/rca_agent_system.md` content with v2 (drops "deep trace" header, "Tool budget: 6 tool calls" line, `source.trace` references, pre-fetched primer blocks). `jenkins_rca/main.py`: `rca_dashboard` + `rca_pipeline_builds` default `days` param 2→7. Operator now sees a week of pipeline failures by default.

76. **Phase 3 — fix text-tool-calls bug + escalating dedup** — Build 30 trace showed LLM imitating the system prompt's reasoning-narration example LITERALLY by writing `tool_calls: - functions.foo: ...` as TEXT inside `content` field instead of using the OpenAI function-calling API. Result: `msg.tool_calls=None`, server saw zero real tool calls, loop terminated with no evidence. Two fixes: (1) prompt rewrite — explicit "DO NOT write tool calls as text inside content; the content field is for natural-language reasoning ONLY"; (2) server-side rescue in `agent.py` — when `msg.tool_calls` is None AND content contains `tool_calls:`/`functions.`, retry with `tool_choice="required"` to force a structured call. Plus: tool-call dedup cache extended from `(prev_iter, prev_result)` to `(prev_iter, prev_result, hit_count)`. 1st repeat returns cached + soft warning; 2nd+ repeat returns ERROR ONLY (no data) — forces LLM to change strategy or finalize. Build 5177 dropped from 12 calls / 10 iters / $0.33 → 6-7 calls / 4-5 iters.

77. **Phase 3 — stage→helper convention table + chain-walk teaching** — system prompt step 2 now has an explicit table mapping every pipeline stage to its helper file: `Jira Details → vars/JiraDetails.groovy`, `Prod+1 → vars/prodPlusOne.groovy`, `Deploy Prod+1 → vars/deployProdPlusOne.groovy`, `Build → vars/buildService.groovy`, `Infra → vars/createGreenInfra.groovy`, `Deploy → vars/nonwebdeploy.groovy` (or `webdeploy.groovy`), `Rollout → vars/rollout.groovy`, `Build Frontend → vars/buildFrontend.groovy`, `Deploy Frontend → vars/deployFrontend.groovy`. Drill path explicit: log → `get_jenkins_job_config` → `repo_read_file(vars/<helper>.groovy)` directly (skip entrypoint header which has no stages). Plus Jenkins shared-lib rules: `libraryResource '<X>'` → `resources/<X>` on disk; `src/com/blackbuck/utils/<Class>.groovy` = utility class. STRICT do-not-waste list bans entrypoint header reads, redundant `repo_search` when stage name + table covers it, and re-reads of same helper with overlapping line ranges. Build 5177 trace went from "read 3 wrong paths" to direct hit on `vars/prodPlusOne.groovy` → `createRuleForProdPlusOne.groovy` → `InfraComposer/module/listener_rule_for_prod_plus_one/main.tf`.

78. **Phase 5 — AWS describe tools + SSM removed (per user decision)** — `jenkins_rca/aws_tools.py` (NEW, ~210 lines): cross-account describe tools backed by boto3 + STS `AssumeRole BBCTLRcaReadOnly`. Per-process STS cred cache keyed by account_id (15-min session); host account skips STS entirely (uses default `bbctl-backend-service` creds). Four functions: `describe_target_health(target_group_arn)`, `describe_target_group(target_group_arn)`, `describe_instance(instance_id, aws_account, aws_region)`, `describe_listener_rule(rule_arn)`. Account ARN-parsing → resolution map (zinka/bbfinserv/divum/tzf). `aws_run_ssm_command` explicitly NOT implemented — Option C decision: RCA never logs into instances; operator uses `bbctl shell <instance_id>` themselves when service-side detail is needed. `aws_run_ssm_command` schema removed from `tool_schemas.TOOLS` so the LLM can't even attempt it. `jenkins-rca-ssm-send` IAM inline policy left in place harmlessly.

79. **Phase 5 — sanitizer no longer masks AWS account IDs** — `sanitize_rules.yml`: removed the `aws_account_id` redaction rule (was replacing 735317561518 / 075903075452 / 597070799581 / 476114138058 with `<account>` placeholder). Account IDs are not secrets — they appear in every ARN, every CloudWatch URL, every IAM ARN. Masking them broke `aws_describe_target_health` etc. because the LLM was passing placeholder-bearing ARNs verbatim to boto3, which returned `ValidationError: '...<account>...' is not a valid target group ARN`. LLM then hallucinated "ARN missing in config.json" as the RCA. Real secrets (access keys, GitHub PATs, OAuth secrets, Anthropic keys, Gemini keys, bearer tokens, presigned signatures, SSH private keys, Jenkins tokens) stay redacted via the remaining rules. Defense in depth: `aws_tools.py:_check_arn_placeholder()` returns a helpful error if ANY placeholder text appears in an ARN, telling the LLM which account IDs are valid.

80. **Phase 5 — log_window TAIL (errors live at bottom)** — Build 5177 RCA kept landing on "terraform stale state" because the primer was slicing `log_window[:30000]` — keeping the FIRST 30K chars (pipeline startup chatter, dependency resolution, git checkout output). The real fatal `Error: TooManyUniqueTargetGroupsPerLoadBalancer` was on line 79995 of the 80071-line log = 76 lines from the end = past the 30K head cap. Changed both primer code paths in `agent.py` to `log_window[-30000:]` (TAIL, not head). Updated section header to "tail of build log, error lives at the bottom" so the LLM understands the orientation. Also added backwards-scan rule to system prompt method step 1: "Scan the log BACKWARDS from the end — the real fatal cause is almost always near the bottom of log_window, not the top. Walk from the LAST line upward; find the LAST line matching `^Error:` / `^ERROR:` / `^FATAL:` / `^Caused by:` — that's the fatal cause line." Worked example using build 5177's exact log fragment ("Stale state detected" → noise; "Error: TooManyUnique..." → real cause).

81. **Phase 5 — reclassification rule (terraform → aws_limit)** — `docops/runbooks/terraform.md` gains a "When to RECLASSIFY out of this runbook" table mapping terraform-surfaced error strings to their real class: `TooManyUniqueTargetGroupsPerLoadBalancer` → aws_limit; `LimitExceeded`/`Service quota exceeded` → aws_limit; `VcpuLimitExceeded`/`InstanceLimitExceeded` → aws_limit; `AccessDenied`/`UnauthorizedOperation` → aws_limit (perms mode); `Stale state detected — auto-destroying` alone (no Error:) → NOT a failure, keep scanning down. Terraform is often just the messenger — the cause is the AWS error inside the terraform Error: block. `docops/runbooks/aws_limit.md` gains a "Common mode — ALB target-group count hit (build 5177 case)" section with three-path fix template (cleanup orphan TGs / move to different ALB / Service Quotas request).

82. **Phase 5 — Option A — collapse 4 AWS tools → 1 generic `aws_describe`** — user feedback: 20 hand-rolled tools = spec spam in every iter's prompt. Compressed `aws_describe_target_health`, `aws_describe_target_group`, `aws_describe_instance`, `aws_describe_listener_rule` into one `aws_describe(service, operation, params, aws_account, aws_region)`. Server validates operation against `^(Describe|Get|List|Lookup|Search|Show|Estimate)\w+$` regex (rejects write actions), PascalCase converts to boto3 snake_case method, strips `ResponseMetadata`, returns slim JSON. Same security (regex + ReadOnlyAccess IAM); auto-extends to RDS, Lambda, CloudWatch Logs, AutoScaling, IAM Get*, etc. without new tool definitions per call site. Total tool count 20→16. Build 5177 retry: `aws_describe(elbv2, DescribeRules, ...)` → ALB ARN → `aws_describe(elbv2, DescribeTargetGroups, {'LoadBalancerArn': <alb>})` → TG count, exact aws_limit RCA emitted in 3 iters / 7 tools / $0.11 vs previous 14 iters / 12 tools / $0.41 wrong RCA.

83. **Phase 5 — value-provenance rule for hallucinated defaults** — gpt-4.1 kept emitting `curl http://localhost:8080/admin/version` and `sudo tail /var/log/blackbuck/gps.log` in `suggested_commands` even when service.lookup had `target_port=7005`, `filebeat_log_path=/var/log/blackbuck/test-supply-wrapper-nonweb.log`. Training-data prior overpowers prose bans. Three rounds of prompt tightening: (a) "every concrete value in final JSON MUST come from a tool result in this RCA's message history" + provenance table mapping value type → required source tool; (b) `DescribeTargetGroups` marked MANDATORY in iter-0 batch of `docops/runbooks/health_check.md` (so `Port` + `HealthCheckPath` are in context before LLM drafts fix); (c) STRICT do-not-write table listing the four most-common hallucinated defaults paired with their real sources. Build 46 trace afterward: LLM correctly fetched `DescribeTargetGroups` → got `Port: 80, HealthCheckPath: /admin/version` → cross-checked vs `service.lookup.target_port: 8080` → identified port-mismatch as the real RCA (not hallucinated default).

84. **Phase 6 — post-RCA value validator (mechanical, last-resort)** — even with the value-provenance rule, some hallucinated defaults still slipped through. New `jenkins_rca/value_validator.py` (~190 lines) runs server-side after the agent emits final JSON, walks `root_cause`, `suggested_fix.{Finding,Action,Verify}`, every `suggested_commands[].cmd` + `.rationale`. Three regex patterns: `(?<![\d])8080(?![\d])` → real `service.lookup.target_port`; `/admin/version` → real `service.lookup.health_check_path` (or a discovery hint if unknown); `/var/log/blackbuck/gps\.log` → real `service.lookup.filebeat_log_path` (only when service name doesn't contain "gps", so actual GPS RCAs untouched). Every substitution appended to `result["validator_notes"][]`. Evidence snippets NOT walked (verbatim quotes must stay pristine for audit). Idempotent. Wrapped in try/except so a validator bug never breaks the RCA response.

85. **`code_review` tool deferred — use Claude Code Review (external) instead** — Phase 6 originally planned a `code_review` tool using `gpt-4o-mini` / `gpt-4o` as a runtime second-opinion. Reverted before commit. Per user direction, we'll use Anthropic's GitHub-based **Claude Code Review** (https://code.claude.com/docs/en/code-review) as an external review surface for the PRs operators raise based on RCA suggestions, NOT as a runtime tool in the agent loop. Rationale: review-as-tool added cost to every RCA without clear payoff vs the value validator (item 84) for hallucination defense; review-as-PR-bot is the right shape for "is this fix sane" because PRs already exist as the integration point. The `code_review` schema slot in `tool_schemas.TOOLS` and the dispatch placeholder in `agent_dispatch.py` stay reserved for a future revival if needed.

86. **Phase 7 — clean trace logs + SUMMARY block** — old per-build trace was 1500+ lines of clumpsy noise: system prompt re-dumped every iter, raw `response.model_dump()` JSON duplicated alongside the structured `tool_calls` field, no top-of-file summary. Three changes in `agent.py`: (a) `_fmt_request_payload(..., hide_system=True)` from iter 1+ — placeholder line "[omitted — see top of trace]" instead of re-dumping the 50KB system prompt; tool-result messages capped to 200 chars in REQUEST blocks since the full body is already shown in the ITER N TOOL block; (b) drop raw `model_dump` unless `JENKINS_RCA_RAW_DUMP=1` env opt-in; (c) new `_prepend_summary_header()` that walks `_iter_log` (a new per-iter accounting list capturing tools chosen, reasoning narration, tokens, finish_reason) and splices a compact `=== RCA SUMMARY ===` block after the trace header line — iter-by-iter tool list, files_read / aws_apis / runbooks rollup, cost, result. Build 46 trace went from 1527 → 488 lines (-68%), cost from $0.22 → $0.08 (-63%, fewer redundant context replays).

87. **Phase 7 — outcome_log SQLite + failure-signal vocabulary + CLI** — Option A (measure-before-ship). New `jenkins_rca/outcome_log.py` writes one row per RCA to `/var/cache/jenkins-rca/outcomes.sqlite` capturing job/build/class/iters/tool_calls/tokens/cost + footprint arrays (files_read, aws_apis, runbooks) + `failure_signals[]` + `trace_path` + nullable `quality`/`notes` for later manual review. Twelve deterministic signals appended at occurrence by `agent.py`: `force_final_{wall_clock,cost_cap,iter_cap}`, `text_tool_calls_rescue`, `voluntary_bail_rescue` (later DEPRECATED in item 97), `dup_call_{warning,rejected}`, `file_not_found_in_chain`, `final_json_parse_failed`, `evidence_validator_dropped`, `evidence_snippet_hallucinated` (later DEPRECATED in item 94), `low_evidence_count`. New `jenkins_rca/cli_outcomes.py` queries: `recent N`, `signals D` (frequency over D days), `by-class D`, `cost D`, `show <id>`. Smoke-tested with two synthetic rows + all four CLI commands.

88. **Phase 7 — `PER_TOOL_RESULT_CAP` skip for authored/curated reads** — build 46 regression cause: 1500-char cap on every tool result silently truncated `read_runbook("health_check")` mid-body. The MANDATORY DescribeTargetGroups rule lives at ~3 KB into the runbook; LLM saw chars 0-1513 → never reached the rule → never called DescribeTargetGroups → vague RCA. Same cap truncated `repo_read_file(deployProdPlusOne.groovy)` past line ~32, hiding the actual healthy.sh call site. Fix in `agent.py`: skip the cap for `read_runbook`, `repo_read_file`, `github_read_file` — three tools where output is curated by us or already takes precise selectors (path + line range), so byte-capping is double-limiting + silent. AWS describe / grep / search outputs keep the cap (can be huge external responses). Build 46 v3 immediately recovered: aws_apis went from 2 (no DescribeTargetGroups) → 3 (full health-check evidence set), correct port-mismatch RCA cited.

89. **Phase 8 — job_flow .md schema + tools** — concrete bug from build 5177: the hardcoded stage→helper table from item 77 said `Infra → createGreenInfra.groovy`, but for `main_stagger_prod_plus_one` job the failed stage marker `(Infra Prod+1)` is NOT declared in the main pipeline — it lives inside the `vars/prodPlusOne.groovy` WRAPPER helper which calls `createRuleForProdPlusOne(service, 150)` for that nested stage. LLM trusted the table → cited `createGreenInfra.groovy` as evidence → wrong code path, misleading the operator. Fix is documentation-driven: new `docops/job_flows/` directory with one .md per Jenkins pipeline family (`main_stagger_prod_plus_one`, `stagger_nonweb`, `stagger_prod_plus_one_frontend`, `create_quick_infra`, `hotfix_noncanary`, `qa_automation`) plus an `index.md` describing match priority. Each doc has a `## Match` section (script_path stem + distinctive inline_script signature lines) and a stage-to-helper table derived from READING actual pipeline code (`jenkins_pipeline_master/*.groovy` + `vars/*.groovy`). Two new tools `list_job_flows()` and `read_job_flow(name)` in `jenkins_rca/mcp_tools.py` + schemas in `tool_schemas.py` + dispatch in `agent_dispatch.py`. LLM calls `list_job_flows()` in iter 0, picks the matching flow by script_path/inline_script content (NEVER by Jenkins display name — that's renameable), reads it, then drills into actual code.

90. **Phase 8 — strip misleading examples from tool_schemas.py + system prompt** — companion to item 89. `tool_schemas.py` scrubbed of all `e.g.` example values: removed `'MB-7545'`, `'i-0e8411d8d8817fe32'`, `'create-quick-infra-devops-test'`, `'alchemist'`, `'JiraDetails.groovy'`, `'zinka|bbfinserv|divum|tzf'` examples + the multi-line `aws_describe` example block with hardcoded ARNs. Replaced with directives like "Derive from service.lookup.x" or "Pass the value as it appears in the log/tool result". `prompts/rca_agent_system.md`: removed the 9-row stage→helper table from item 77 (the table that LIED for the prod+1 wrapper case), removed the toll-gold build-5177 worked example (real job/build names that could leak as defaults), replaced with a generic algorithmic procedure backed by job_flow docs. New step e: derive helper FROM the pipeline body you just read, not from a static table. Net effect: smaller, less misleading instruction surface; LLM verifies via live code instead of trusting hardcoded mappings.

91. **Phase 8 — expose `repo_list_dir` + tighten job-flow matching** — the Python implementation existed in `mcp_tools.py:repo_list_dir(repo, path)` but was never listed in `tool_schemas.py` or wired in `agent_dispatch.py`, so the LLM could not call it. New job families added to `jenkins_pipeline_master/` without a matching `docops/job_flows/*.md` had no graceful fallback. Schema + dispatch wired now. `docops/job_flows/index.md` rewritten with explicit match priority: (1) `script_path` filename stem, (2) `inline_script` signature lines, (3) `repo_list_dir("jenkins_pipeline", "")` discovery fallback for unknown families. All six flow docs' `## Match` sections restated as bullet criteria (no reliance on Jenkins display name — operator-renameable). System prompt step c2 added: "if no flow matches, list main pipelines via repo_list_dir, pick by content match, read directly. DO NOT pick an unrelated flow doc just to have something to read."

92. **Phase 9 — NESTED STAGE RULE for wrapper helpers** — even with job_flow docs (item 89), build 5177 + build 46 retries still picked the WRONG file: main pipeline body contains both `stage('Prod+1') { prodPlusOne(...) }` (wrapper) AND `stage('Infra') { createGreenInfra(...) }` / `stage('Deploy') { deploy(...) }` (leaf helpers for the non-wrapped flow). When the failed marker was `(Infra Prod+1)` or `(Deploy Prod+1)`, LLM matched on substring overlap with `stage('Infra')` / `stage('Deploy')` and read the leaf helper instead of drilling through `prodPlusOne.groovy`. Two-part fix: (a) deterministic rule in `prompts/rca_agent_system.md` step e2 — "if the failed stage marker is NOT literally a `stage('X')` declared in the main pipeline body, the marker is NESTED inside a wrapper helper; read the wrapper FIRST, do NOT read any leaf-stage helper from main pipeline first"; (b) the two prod+1 flow docs (`main_stagger_prod_plus_one.md` + `stagger_prod_plus_one_frontend.md`) restate the rule as a hard IF/THEN listing the four nested markers and naming the wrapper file for each (prodPlusOne.groovy for backend, prodPlusOneFrontend.groovy for frontend).

93. **Phase 9 — snippet-content validator (later removed in Phase 10)** — companion bug exposed by re-run traces: LLM was citing the RIGHT file but emitting a FAKE snippet for it. e.g. `"source": "createRuleForProdPlusOne.groovy:1", "snippet": "def call(Map params) { ... // helper invoked for listener rule creation"` — actual line 1 is `package vars`, actual signature on line 12 is `def call(String SERVICE, Number priority)`. The existing `_filter_fake_repo_evidence` only validated file PATH (the file was indeed read), not snippet CONTENT. New `_filter_hallucinated_snippets` in `agent.py`: extracts single/double-quoted string literals from each snippet (3-80 chars, skipping placeholders), requires each literal to appear in the cited file's content on disk; if no quoted literals, falls back to a 30-char sliding-window match against whitespace-normalised file body. File contents cached per RCA. Drops failing entries + records `evidence_snippet_hallucinated` signal. Smoke-tested against the exact build 5177 fabrications → both correctly dropped while real entries kept. (Superseded by item 94 — schema-side fix makes this validator unnecessary.)

94. **Phase 10 — schema-side anti-hallucination (coords-only repo evidence, server-filled snippets)** — user feedback after seeing items 84/93: validators that drop or substitute LLM output are band-aids — fix at the schema/prompt/model layer instead. New evidence schema in `prompts/rca_agent_system.md`: REPO-FILE entries emit `{source: "<repo>/<file>", line_start, line_end}` ONLY — no snippet field. Server reads the file from `$JENKINS_RCA_REPOS_DIR` (defaults to `/opt/jenkins-rca/repos`), slices `line_start..line_end`, prefixes line numbers (matching the `repo_read_file` output format), injects as `snippet`. NON-repo evidence (jenkins_log / build_meta / jira / github / aws / runbooks) still emits `{source, snippet}` verbatim. LLM physically cannot fabricate code it does not emit. New `_fill_repo_snippets` in `agent.py` runs as the only server-side transformation. Removed: `_filter_fake_repo_evidence` call, `_filter_hallucinated_snippets` call, `value_validator.validate_and_fix` call from `main.py`. Three signals (`evidence_validator_dropped`, `evidence_snippet_hallucinated`, `value_validator_substituted`) marked DEPRECATED in `outcome_log.py` vocabulary. Surfaces `_error` field on malformed coords / out-of-bounds / missing file — transparent failure beats silent correction. `_FORCE_FINAL_PROMPT` JSON schema reminder updated to mirror the new evidence shape so the force-final rescue path doesn't fall back to the old shape.

95. **Phase 10 — model default switch (gpt-5 → gpt-4o)** — attempted to default to `gpt-5` for stronger verbatim recall + reasoning, but the jenkins-rca OpenAI project lacked access (`403 PermissionDeniedError: model_not_found`). Fallback to `gpt-4o` ($2.50/$10 per 1M tokens vs gpt-4.1's $2/$8) — broadly available, better verbatim recall than gpt-4.1, especially needed now that response post-processing is gone. `JENKINS_RCA_MODEL` env var still overrides; once gpt-5 is granted on the project (OpenAI dashboard → Limits → Models), set `JENKINS_RCA_MODEL=gpt-5` via systemd drop-in to switch without code change.

96. **Phase 10 — main.py share `_DEFAULT_MODEL` with agent.py** — `jenkins_rca/main.py` had its OWN hardcoded `"gpt-4.1"` default in two spots (error-stub model hint + cost-rollup model lookup) separate from `agent.py:_DEFAULT_MODEL`. After item 95, the actual LLM call used gpt-4o correctly (agent.py path), but the `model_used` field in the response JSON reported `"gpt-4.1"` AND the cost was computed at gpt-4.1 pricing. Both sites now import `_DEFAULT_MODEL` from agent — single source of truth.

97. **Phase 10 — reclassify `voluntary_bail_rescue` as normal JSON FINALIZE step** — gpt-4o (more so than gpt-4.1) emits the final answer as markdown headings (`### Summary\n### Failed Stage\n...`) when `response_format=json_object` is not enforced. OpenAI rule: `response_format=json_object` and `tools=auto` are mutually exclusive on a single call — JSON mode prevents tool calls. So during tool-call iters we must omit json_object; when LLM signals "done" (`tool_calls=[]`) we re-issue with json_object to force parseable JSON. That second call was previously labeled `VOLUNTARY-BAIL RESCUE` + emitted as a failure_signal, framing it as an emergency. It is not — it is part of the normal agent contract for every RCA. Renamed trace labels to `JSON FINALIZE REQUEST` / `JSON FINALIZE RESPONSE` in `agent.py`, dropped the `_failure_signals.append("voluntary_bail_rescue")` line, marked the signal DEPRECATED in `outcome_log.py` vocab. Same cost, honest framing.

| What                              | Where                                                            |
| --------------------------------- | ---------------------------------------------------------------- |
| FastAPI entrypoint                | `jenkins_rca/main.py`                                              |
| LLM dispatch & tool-context build | `jenkins_rca/llm.py` (`build_initial_tool_ctx` is the public alias used by the agent) |
| Agent loop (Phase E)              | `jenkins_rca/agent.py`                                             |
| Per-RCA freshness pull            | `jenkins_rca/git_fresh.py`                                         |
| Repo tool palette (`repo_*`)      | `jenkins_rca/mcp_tools.py`                                         |
| Jenkins REST helpers              | `jenkins_rca/jenkins.py` (`get_job_config` added for the agent)    |
| Error classifier (ordered rules)  | `jenkins_rca/classifier.py` + `classifier_rules.yml`               |
| Log window extraction             | `jenkins_rca/window.py`                                            |
| Per-canary-stage pass/fail        | `jenkins_rca/canary_analyzer.py`                                   |
| Jira fetch (incl. `customfield_10973` Signed Off Commit ID) | `jenkins_rca/jira.py` |
| GitHub commit lookup              | `jenkins_rca/github.py`                                            |
| NewRelic slow-txn query           | `jenkins_rca/newrelic.py`                                          |
| Runbook section extractor         | `jenkins_rca/runbook.py`                                           |
| 24h diskcache + daily cap         | `jenkins_rca/cache.py`                                             |
| Audit log writer                  | `jenkins_rca/audit.py` (+ `read_by_request_id` for HTML report)    |
| HTML report endpoint              | `jenkins_rca/main.py::rca_report` + `jenkins_rca/templates/rca_report.html` |
| systemd start script              | `infra/scripts/jenkins-rca-start.sh`                               |
| Repos + docops auto-sync          | `infra/scripts/sync-repos.sh` + `/etc/cron.d/jenkins-rca-sync`     |
| Jenkins post-failure groovy lib   | `infra/jenkins/post_failure_rca.groovy` (mirrored to `vars/triggerRcaWebhook.groovy` in jenkins_pipeline) |
| Pipeline wiring                   | `jenkins_pipeline_master/main_stagger_prod_plus_one.groovy` (post.failure block) |
| Slack RCA helper                  | `jenkins_pipeline_master/src/com/blackbuck/utils/Notification.groovy::rcaAlert` |
| LLM prompts (one-shot)            | `prompts/rca_system.md`, `prompts/rca_examples.md`               |
| LLM prompt (agent)                | `prompts/rca_agent_system.md`                                    |
| Per-class runbooks                | `docops/StaggerProdPlusOneDeploy.md`, `docops/JiraDetailsCompliance.md`, `docops/HealthCheckFailure.md`, … |
| Per-error-class runbooks (agent)  | `docops/runbooks/*.md` (compliance, health_check, aws_limit, terraform, canary_fail, canary_script_error, java_runtime, scm, parse_error, unknown) |
| Per-pipeline-family job_flow docs | `docops/job_flows/*.md` (index, main_stagger_prod_plus_one, stagger_nonweb, stagger_prod_plus_one_frontend, create_quick_infra, hotfix_noncanary, qa_automation) |
| Agent tool schemas (OpenAI fn-call) | `jenkins_rca/tool_schemas.py` |
| Agent dispatch (tool name → callable) | `jenkins_rca/agent_dispatch.py` |
| Generic AWS describe (STS assume-role) | `jenkins_rca/aws_tools.py` |
| Per-RCA outcome log (SQLite) | `jenkins_rca/outcome_log.py` → `/var/cache/jenkins-rca/outcomes.sqlite` |
| Outcome query CLI | `jenkins_rca/cli_outcomes.py` (`python3 -m jenkins_rca.cli_outcomes {recent,signals,by-class,cost,show}`) |
| Server-side snippet filler (phase 10) | `jenkins_rca/agent.py:_fill_repo_snippets` — reads files from disk for repo evidence |
| Per-build trace files | `/tmp/jenkins-rca-trace-<job>-<build>.txt` (latest-only mirror at `/tmp/jenkins-rca-last-trace.txt`) |
| Full request dump (latest run only) | `/tmp/jenkins-rca-last-prompt.txt` |

---

## Recent improvements (May–June 2026, branch `feature/jenkins-rca-agent-only` continued)

98. **Chain-walk self-verification injected before finalize** — when the agent stops voluntarily (tool_calls=[]) and `_parse_final_json` returns None, a `_CHAIN_VERIFY_PROMPT` is injected ONCE (guarded by `_chain_verify_done` flag). The prompt asks the LLM to review every function call and `libraryResource 'scripts/...'` reference it saw in files it read, and call `repo_read_file` for any unread ones before finalizing. Universal — works for all job types without hardcoding file lists. Skipped when the LLM already emits valid JSON at voluntary stop (normal flow). Second stop always finalizes.

99. **Service migrated from `/opt/jenkins-rca/` to `/home/ubuntu/project/Jenkins-rca/`** — `/opt/jenkins-rca/` was a stale flat copy with no git tracking; docops/runbooks and code changes deployed there never reflected git state. Migration: (1) all hardcoded `/opt/jenkins-rca/` paths in `mcp_tools.py`, `git_fresh.py`, `evidence.py`, `source_trace.py` replaced with `Path(__file__).resolve().parent.parent` relative paths + `os.environ.get("JENKINS_RCA_REPOS_DIR/JENKINS_RCA_DOCS_DIR", ...)` overrides; (2) `infra/scripts/jenkins-rca-start.sh` updated: `APP_DIR=/home/ubuntu/project/Jenkins-rca`, `VENV=/home/ubuntu/project/Jenkins-rca/.venv`, `JENKINS_RCA_REPOS_DIR=/var/cache/jenkins-rca/repos`; (3) systemd `ExecStart` updated to new script path; `ReadWritePaths` kept as `/var/cache/jenkins-rca /var/log/jenkins-rca /tmp` (repos go to cache dir, satisfying `ProtectSystem=strict`); (4) new venv created at `~/project/bbctl/.venv` with all deps from `jenkins_rca/requirements.txt`; (5) cron `jenkins-rca-sync` updated to `~/project/bbctl/infra/scripts/sync-repos.sh`. `git pull` in `~/project/bbctl/` is now the complete deploy step.

100. **Evidence output schema hardened — 5 rules** — system prompt evidence section tightened: (a) `line_start/line_end` for repo evidence MUST be specific 1-5 lines of interest, NOT the full read window (read wide, cite narrow); (b) `main_*.groovy` dispatch pipeline file MUST NOT appear in `evidence[]` — dispatch only, no implementation; (c) `jenkins_log` source ALWAYS required — must contain exact fatal error line verbatim; (d) port in `suggested_commands` MUST come from `DescribeTargetHealth.TargetHealthDescriptions[0].Target.Port` (NOT `DescribeTargetGroups.Port`); (e) health script name derived from `libraryResource 'scripts/...'` line in deploy helper — never assumed.

101. **health_check.md trimmed (11.7K → 5.5K chars)** — verbose chain-walk diagrams, redundant port-source explanations, and multi-line action template removed. All rules preserved. Saves ~15K tokens per RCA across 6 iters (~$0.04/RCA).

102. **Skip main pipeline read for `*Prod+1*` stage markers** — for any failed stage marker containing "Prod+1" (`Infra Prod+1`, `Deploy Prod+1`), system prompt step d now says: skip reading `main_stagger_prod_plus_one.groovy` entirely — go directly to `vars/prodPlusOne.groovy`. Main pipeline just calls `prodPlusOne(...)` which the job_flow doc already documents; reading 200 lines of dispatch adds nothing and wastes one iteration. Also reinforced in `main_stagger_prod_plus_one.md` job_flow doc.

103. **Chain-walk follows `scripts/` only, not config/data files** — `_CHAIN_VERIFY_PROMPT` updated: "follow `libraryResource 'scripts/...'` references only — skip `config.json`, `*.yml`, `*.conf`, `fluent-bit.conf` etc. (data files, not scripts)." Previously LLM was following `libraryResource 'config.json'` from `createRuleForProdPlusOne.groovy` → reading `resources/config.json` → adding irrelevant service config to evidence and costing an extra iter.

104. **Wrong filename derivation fix** — system prompt step e: "When you see function call `foo(...)`, file is EXACTLY `vars/foo.groovy` — the token before `(`, verbatim. Do NOT append stage name words. Example: `createRuleForProdPlusOne(service, 150)` → `vars/createRuleForProdPlusOne.groovy`, NOT `vars/createRuleForProdPlusOneInfra.groovy` (stage name 'Infra Prod+1' does not append to the function name)." Fixed a recurring regression where LLM appended the nested stage's leading word to the function name.

105. **`_runbooks_dir()` fallback for empty primary dir** — `mcp_tools._runbooks_dir()` previously returned the primary dir (`DOCS_DIR/runbooks/`) if it existed as a directory, even if empty. An empty `/opt/jenkins-rca/docops/runbooks/` silently shadowed the git-tracked fallback, causing `read_runbook("health_check")` to return "not found" and skip all MANDATORY AWS describes. Fixed: primary is used only if `RUNBOOKS_DIR.is_dir() AND any(RUNBOOKS_DIR.glob("*.md"))`. Empty primary → fallback. Now superseded by item 99 (single path, no two-dir ambiguity).

106. **Chain-walk evidence line precision enforcement** — `_CHAIN_VERIFY_PROMPT` (injected before LLM finalizes) extended with step 5: "For each repo evidence entry, if `(line_end - line_start) > 15`, that is your READ WINDOW not evidence — re-identify the 1-5 specific lines that directly show the failure and narrow." Previously chain-walk only checked for unread files; it now also forces LLM to narrow oversized line ranges before emitting final JSON. Result on build 5177: `createRuleForProdPlusOne.groovy` evidence narrowed from `lines 1-80` to `line_start=19, line_end=19` (the exact `createInfra(...)` call).

107. **Evidence snippet HTML rendering — code block with line numbers** — `rca_report.html` evidence snippet changed from plain `<div class="ev-snippet">{{ e.snippet }}</div>` to `<code class="ev-snippet">{{ e.snippet }}</code>` with CSS: `white-space: pre; font-family: monospace; background: #0d1117; border-radius: 4px; padding: 8px; max-height: 300px; overflow: auto`. Previously all evidence lines were squashed into one paragraph (no newlines visible). Now each line number is on its own line, scrollable, readable. No-snippet entries skip the block entirely.

108. **Evidence precision WRONG/CORRECT examples in system prompt** — added explicit anti-pattern to evidence rules: "WRONG: `line_start=1, line_end=80`" plus concrete correct examples for the files most often cited: `createRuleForProdPlusOne.groovy` → `line_start=19, line_end=19`; `InfraComposer/config/<svc>/main.tf` → `line_start=23, line_end=43`. Plus absolute rule: "If your line_start=1 and line_end matches your read window exactly, you are WRONG — narrow it." Combined with item 106's chain-walk enforcement, evidence precision improved significantly.

109. **`aws_limit.md` runbook — ALB ARN derivation formula added** — `suggested_commands` was emitting `--load-balancer-arn <alb_arn>` placeholder (unresolvable) because agent never derived ALB ARN. Root cause: `service.lookup.rule_arn` contains ALB ARN embedded in it but agent didn't know how to extract it. Added to `aws_limit.md` a mandatory ALB ARN derivation rule: `rule_arn` format is `arn:...listener-rule/app/<alb-name>/<alb-id>/<listener-id>/<rule-id>` → strip last two slash-segments to get `arn:...loadbalancer/app/<alb-name>/<alb-id>`. LLM must use this formula and NEVER emit `<alb_arn>` placeholder. Also added correct quota code `L-417A185B` for `Target groups per Application Load Balancer` service quota.

110. **Runbooks periodically deleted by cron sync** — root cause discovered: `infra/scripts/sync-repos.sh` runs `aws s3 sync s3://docops-doc-storage/docs/ ~/project/bbctl/docops/ --delete` every 2h. The `--delete` flag removes any local file NOT present in S3. Since `runbooks/` and `job_flows/` subdirectories were committed to git but never uploaded to S3, every cron cycle wiped them. Pattern: `git checkout HEAD -- docops/runbooks/` restored files → 2h later cron deleted them again. Fix: upload runbooks + job_flows to S3 as canonical source: `aws s3 sync docops/runbooks/ s3://docops-doc-storage/docs/runbooks/ --profile zinkareadonly`. S3 is the authoritative store; git is the edit-and-push workflow. Deploy new/updated runbook = edit locally → `git commit` + `aws s3 cp` → cron picks it up.

111. **MCP-based RCA experiment (`claude_test/`)** — proof-of-concept replacing OpenAI function-calling agent with Claude Code + MCP servers. Created `claude_test/mcp_server.py` — Python MCP server (FastMCP) exposing: `repo_read_file`, `repo_list_dir`, `repo_search`, `repo_find_function`, `list_runbooks`, `read_runbook`, `list_job_flows`, `read_job_flow`, `service_lookup`, `jenkins_get_build_log`, `jenkins_get_job_config`, `aws_describe`. Registered in `~/.claude.json` as `jenkins-rca-tools` MCP server. On startup loads credentials from AWS Secrets Manager (`jenkins-rca/prod`) using `zinkareadonly` profile — no separate API key needed. AWS describe uses `zinkareadonly` boto3 profile directly (not STS cross-account). Ran full RCA for build 46 using only MCP tools: `read_runbook(health_check)` → `repo_read_file(vars/deployProdPlusOne.groovy)` → `repo_read_file(resources/scripts/healthy.sh)` → `aws_describe(DescribeTargetGroups)` → `aws_describe(DescribeTargetHealth)`. Correct RCA emitted. Port 8080 from real `DescribeTargetHealth.Target.Port` (not hardcoded). Zero separate API cost — uses Claude Code session. Jenkins unreachable from laptop (private IP `10.34.x.x`); log pasted manually for test. Target architecture: Claude Code as agent, MCP servers for all data sources, no custom agent.py loop.

112. **`create-quick-infra.groovy` renamed to `Jenkinsfile_create_quick_infra`** — master branch merged into `release/REQ-463-staggerprodplusupdate-v2`. The main Jenkinsfile for the create-quick-infra pipeline was renamed in master. Updated in `docops/job_flows/create_quick_infra.md`, `docops/runbooks/java_runtime.md`, and `docops/runbooks/compliance.md` — all references changed from `create-quick-infra.groovy` to `Jenkinsfile_create_quick_infra`.

113. **MCP architecture proof-of-concept tested end-to-end** — confirmed Claude Code (CLI) can run a full RCA using ONLY MCP tools, no Python agent loop, no separate API key. Ran build 46 (health_check) and build 29 (Deploy stage healthy.sh failure) entirely via `jenkins-rca-tools` MCP server. Tool calls: `read_runbook(health_check)` → `repo_read_file(vars/nonwebdeploy.groovy:199)` → `repo_read_file(resources/scripts/healthy.sh:21-23)` → `aws_describe(DescribeTargetGroups)` → `aws_describe(DescribeTargetHealth)` → `aws_describe(DescribeInstances)`. All evidence verified live. Port 8080 sourced from real `Target.Port`, HealthCheckPath `/admin/version` from real `DescribeTargetGroups.HealthCheckPath`. Build 46 RCA cost: $0 (Claude Code session tokens already paid). Architecture validated for migration: keep jenkins-rca as production webhook service, MCP version as developer workflow.

114. **Build 5177 (aws_limit) — clean run after `terraform.md` ALB ARN derivation rule deployed** — runbook reclassify table now includes ALB ARN derivation formula. LLM derives ALB ARN from `service.lookup.rule_arn` by stripping last two slash-segments (rule → loadbalancer). Latest trace: 5 iters, $0.272 cost (down from $0.46), 7 tool calls. Real ALB ARN in suggested commands (no `<alb_arn>` placeholder). AWS account correctly resolved as `divum` for toll-gold service. Evidence cites `createRuleForProdPlusOne.groovy:19-19` and `InfraComposer/config/toll-gold/prodplusone/main.tf:23-43` with specific line ranges. Two trace walkthrough docs written: `docs/rca/trace_walkthrough_build46.md` and `docs/rca/trace_walkthrough_build5177.md` — iter-by-iter flow + code file→function map.

115. **Runbooks restored via S3 (cron `--delete` race fixed)** — root cause of repeated runbook disappearance: `sync-repos.sh` runs `aws s3 sync s3://docops-doc-storage/docs/ ~/project/bbctl/docops/ --delete` every 2h, wiping any local file not in S3. Runbooks were committed to git only, never uploaded to S3, so cron deleted them. Fix: uploaded `docops/runbooks/*.md` and `docops/job_flows/*.md` to `s3://docops-doc-storage/docs/runbooks/` and `s3://docops-doc-storage/docs/job_flows/`. S3 now canonical store, git for edit-and-push workflow. New deploy procedure: edit locally → `git commit` + `aws s3 cp <file> s3://docops-doc-storage/docs/runbooks/<file> --profile zinkareadonly` → cron picks up in 2h or manual `sudo sync-repos.sh`.

116. **Unknown-error RCA quality — research + plan for RAG layer** — current agent works well for classified errors. Falls short on `unknown` class. Three additional failure modes captured during session:
    - `terraformstate_error.txt` — precheck `checkTerraformStateFile` finds non-empty S3 tfstate, throws `Unexpected Error: Terraform state contains resources. Total resources: 4`. Should be own error class (`stale_tf_state` or extension of `terraform`).
    - `test-supply-wrapper-nonweb_unhealthy.txt` — Deploy stage health_check correctly detected but `filebeat_log_path` empty in config = log path placeholder in output.
    - `test-supply-wrapper-nonweb_failure_rcanotcaptured.txt` — `Finished: NOT_BUILT` due to precheck `error()` triggering rollout precheck failure. Item 52 fix needs production verification across all 5 pipelines.

    Research findings (2026 best practices, LogSage @ ASE 2025, arxiv 2506.03691): RAG layer recommended as ADDITION to existing agent (not replacement). Architecture:
    - Embed cleaned error window (~last 50-200 lines) + metadata using **BGE-M3** (self-hosted) or `text-embedding-3-small` (quick start)
    - Vector store: `(error_embedding, RCA_text, fix_description, build_id, verified_flag)` in pgvector/Qdrant
    - Top-K retrieval (K=3-5, similarity >0.75 threshold) → inject as few-shot examples in agent's system prompt
    - Hybrid retrieval (BM25 + dense) beats dense-only on log corpora
    - Gating: needs ≥200 resolved builds with operator-written fixes before RAG pays off

    Implementation priority order:
    1. **`/rca/feedback` endpoint** (1-2 days) — operator tags final fix (PR URL, "what fixed it"). Without labelled fixes, RAG has nothing useful.
    2. **Error-fingerprint pipeline** (2-3 days) — normalize logs, extract error window, embed, store in pgvector.
    3. **Hybrid retrieval + few-shot injection** (1 day) — top-K=3 (dense+BM25 reranked) into agent system prompt when similarity >0.75.
    4. **Specifically fix `unknown_class`** (2 days) — replace generic guide with retrieved examples when ≥1 hit clears threshold.
    5. **Eval harness** (ongoing) — hold-out 50 resolved builds, replay with/without RAG, measure operator-rated accuracy + MTTR delta.

    Fine-tuning verdict: skip for now (need >10k labelled examples, vocabulary not alien enough). LangGraph migration: defer (custom loop fine for single-agent; reconsider for multi-agent with checkpointing).

    Sources cited: LogSage (arxiv 2506.03691), RAGLog (arxiv 2311.05261), Functionality-Aware RAG (arxiv 2509.20552), Seven Failure Points of RAG (arxiv 2401.05856), Medium n8n+AI article by Ankit Saxena.

    Next concrete tasks:
    - Add `tf_state_check` regex to `classifier_rules.yml` for `Terraform state contains resources` pattern → new runbook `docops/runbooks/stale_tf_state.md`
    - Verify NOT_BUILT post.always wiring lives in all 5 production pipelines (item 52)
    - Build feedback capture endpoint before any RAG work
