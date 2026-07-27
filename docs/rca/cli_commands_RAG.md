# jenkins-rca — CLI Operations Cheat Sheet

One-stop reference for every command an operator runs to set up,
update, observe, and debug jenkins-rca + its RAG layer on EC2.

Pair with:
- `docs/rca/bbctlrca.md` — service design + RCA agent overview
- `docs/rca/RAGflow.md` — RAG architecture + roadmap
- `docops/jenkins_pipelines_golden.md` — pipeline map for RCAs

All commands assume you're on the EC2 host (`ubuntu@jenkins-rca-ec2`)
unless prefixed `LOCAL:`. Paths assume default install at
`/home/ubuntu/project/Jenkins-rca/`.

---

## 1. First-time setup (one-shot, NEVER again)

```bash
# 1a. Clone bbctl onto the host (operator action via deploy script or manual)
cd /home/ubuntu/project
git clone https://github.com/BLACKBUCK-LABS/bbctl.git
cd bbctl
git checkout feature/jenkins-rca-agent-RAG   # or master once merged

# 1b. Install Postgres 16 + pgvector + schema + DB role
sudo AWS_PROFILE=<write-profile> bash infra/scripts/rag-postgres-install.sh
# - apt installs postgresql-16 from PGDG
# - builds pgvector v0.8 from source
# - creates jenkins_rca DB + role with a generated password
# - applies jenkins_rca/rag_schema.sql
# - stashes pg_password + pg_host + pg_db + pg_user + pg_port into the
#   jenkins-rca/prod secret in AWS Secrets Manager (needs WRITE profile)

# 1c. Install Python deps inside the service venv
source /home/ubuntu/project/Jenkins-rca/.venv/bin/activate
pip install -r jenkins_rca/requirements.txt

# 1d. (re-)start service so it picks up new JENKINS_RCA_PG_* env from secret
sudo systemctl restart jenkins-rca

# 1e. Smoke embed (confirms PG + OpenAI key + module all wired)
python -m jenkins_rca.rag embed "hello world"
# expect: dim=1536 first5=[...]
```

If the secret stash in `rag-postgres-install.sh` step 5 fails (e.g. no
write-profile available), the script prints the generated `pg_password`
to stderr. Recovery: paste it into a systemd override (see §6 below)
OR run the manual rotation in §7.

---

## 2. RAG CLI — `python -m jenkins_rca.rag <cmd>`

All commands run from `/home/ubuntu/project/Jenkins-rca/` with the service
venv active. Env required: `JENKINS_RCA_LLM_API_KEY`, `JENKINS_RCA_PG_*` (the
service has these via systemd; for ad-hoc CLI, source them via
`eval "$(python -m jenkins_rca.secrets)"`).

| Command | What it does |
|---|---|
| `python -m jenkins_rca.rag embed "<text>"` | Embed text via OpenAI text-embedding-3-small; prints dim + first 5 floats. Uses query_emb_cache. Smoke check. |
| `python -m jenkins_rca.rag search "<query>" [k]` | Semantic search across rca_chunks. Default k=5. Prints top-k hits with similarity scores + source_id + first 120 chars. |
| `python -m jenkins_rca.rag index-docops` | Walks `docops/*.md` (runbooks, job_flows, org docs), chunks markdown on H2 + 2000-char ceiling, embeds, upserts into `rca_chunks` with `source_type` ∈ {runbook, doc, job_flow}. Idempotent via `content_hash`. |
| `python -m jenkins_rca.rag index-audits [dir]` | Walks `<dir>/*.json` audit files (default `/var/log/jenkins-rca/audit`, also accepts `/var/log/jenkins-rca` on EC2). Embeds rationale + suggested_commands as one chunk per audit. Idempotent. |
| `python -m jenkins_rca.rag reset --yes` | TRUNCATE rca_chunks + query_emb_cache + retrieval_cache. **DESTRUCTIVE** — requires `--yes`. Use only when re-indexing from scratch. |

### When to re-index

| Trigger | Command |
|---|---|
| Docops content changed (runbook edit, new job_flow, golden doc update) | `python -m jenkins_rca.rag index-docops` |
| New RCA audits accumulated (~weekly cron once R4 lands) | `python -m jenkins_rca.rag index-audits /var/log/jenkins-rca` |
| Embedding model swap | `... reset --yes` then `index-docops` + `index-audits` |
| PG schema migration | `... reset --yes` then both indexers |

`index-docops` typical run: ~37 files, ~150-400 chunks, ~30s. Cost ≈ $0.001.

---

## 3. Service lifecycle (systemd)

```bash
# Start (if stopped)
sudo systemctl start jenkins-rca

# Stop
sudo systemctl stop jenkins-rca

# Restart (preserves env from secret + override.conf)
sudo systemctl restart jenkins-rca

# Status — confirm running + when started
sudo systemctl status jenkins-rca --no-pager | head -15

# Enable on boot (one-time)
sudo systemctl enable jenkins-rca

# Disable on boot
sudo systemctl disable jenkins-rca
```

### Tail live logs

```bash
# Last 100 lines + follow
sudo journalctl -u jenkins-rca -n 100 -f

# Last 30 lines, no follow
sudo journalctl -u jenkins-rca -n 30 --no-pager

# Since a specific time
sudo journalctl -u jenkins-rca --since "10 minutes ago" --no-pager
sudo journalctl -u jenkins-rca --since "today" --no-pager

# Grep for specific things
sudo journalctl -u jenkins-rca -n 200 --no-pager | grep -iE 'rag|psycopg|error|skipped'
```

### Verify env the running uvicorn process has

```bash
# Sanity-check service has JENKINS_RCA_PG_PASSWORD + JENKINS_RCA_LLM_API_KEY etc.
sudo cat /proc/$(pgrep -f 'uvicorn jenkins_rca' | head -1)/environ \
  | tr '\0' '\n' | grep JENKINS_RCA_ | awk -F= '{print $1"=<"length($2)"chars>"}'
# expect: JENKINS_RCA_PG_PASSWORD=<48chars>, etc. (lengths only, never values)
```

---

## 4. Sync — pull latest jenkins_pipeline + InfraComposer + docops

`infra/scripts/bbctl-sync.sh` is the one and only sync entrypoint.
A `/etc/cron.d/jenkins-rca-sync` cron runs it every 2 hours; you can
also run it manually.

```bash
# Run sync with current defaults (master for jenkins_pipeline, main for InfraComposer)
sudo bash /home/ubuntu/project/Jenkins-rca/infra/scripts/bbctl-sync.sh

# Override the jenkins_pipeline branch for a historical-build RCA
JP_BRANCH=release/REQ-463-staggerprodplusupdate-v2 \
  sudo -E bash /home/ubuntu/project/Jenkins-rca/infra/scripts/bbctl-sync.sh

# What sync does (in order):
#   1. self-heal repo permissions (chown ubuntu:ubuntu)
#   2. jenkins_pipeline: fetch + reset --hard origin/<JP_BRANCH>
#   3. InfraComposer:    fetch + reset --hard origin/<IC_BRANCH>
#   4. aws s3 sync docops/ from s3://docops-doc-storage/docs/
#   5. restart jenkins-rca (so service picks up new docs / config.json)

# Sync log
sudo tail -50 /var/log/jenkins-rca/sync.log
```

The service ALSO does a per-request `git fetch + reset --hard` via
`jenkins_rca/git_fresh.py` whenever an RCA call comes in. That's what
keeps the local clone fresh between cron ticks. The branch tracked
there is the same `master` default unless `JENKINS_RCA_JP_BRANCH` /
`JENKINS_RCA_IC_BRANCH` are set in the service env.

### Force-switch the local clone branch

```bash
# Switch jenkins_pipeline clone to a specific branch
sudo -u ubuntu git -C /home/ubuntu/project/Jenkins-rca/repos/jenkins_pipeline \
  fetch origin <branch>
sudo -u ubuntu git -C /home/ubuntu/project/Jenkins-rca/repos/jenkins_pipeline \
  reset --hard origin/<branch>

# Or via sync with an override:
JP_BRANCH=<branch> sudo -E bash /home/ubuntu/project/Jenkins-rca/infra/scripts/bbctl-sync.sh
```

After this, future RCA calls hit the new branch's content. The git_fresh.py
per-request reset will keep it there if `JENKINS_RCA_JP_BRANCH` matches.

---

## 5. RCA API — curl examples

The service listens on `:7070`. Endpoint: `POST /v1/rca`.

```bash
# Standard RCA call
curl -sX POST http://localhost:7070/v1/rca \
  -H 'Content-Type: application/json' \
  -d '{"job":"<job-name>","build":<build-number>,"deep":true}' \
  | jq '{error_class, summary, suggested_fix, files_read, agent_tool_calls, cost_usd}'

# Examples that exercise the four main pipelines
curl ... -d '{"job":"Stagger Prod Plus One","build":5177,"deep":true}'           # aws_limit
curl ... -d '{"job":"HotFix-NonCanary","build":61,"deep":true}'                  # config_validation
curl ... -d '{"job":"create-quick-infra","build":42,"deep":true}'                # compliance
curl ... -d '{"job":"Stagger Scaling","build":15,"deep":true}'                   # java_runtime (JsonSlurperClassic)

# Common pitfalls:
#   - DO put a space between -H ... and -d ...   (no space => -d glued onto -H value)
#   - DO NOT put newlines inside the JSON string for job names
#   - Job name is the Jenkins display name (with spaces), not the URL slug
```

### Inspect the trace of a recent RCA

```bash
# Per-build trace file (last 50 kept on disk)
ls -ltrh /tmp/jenkins-rca-trace-*.txt | tail -5
less /tmp/jenkins-rca-trace-<Job>-<Build>.txt

# Always-overwritten "last" trace
less /tmp/jenkins-rca-last-trace.txt

# Stored audit JSONs (input to index-audits)
ls -ltrh /var/log/jenkins-rca/*.json | tail -10
```

### Enable full-prompt dump (debug)

```bash
# Set env on systemd unit
sudo systemctl set-environment JENKINS_RCA_DEBUG_PROMPT=1
sudo systemctl restart jenkins-rca

# Next RCA call writes the full initial system + user prompt to:
ls -ltrh /tmp/jenkins-rca-last-prompt.txt

# Inspect what blocks landed
grep -E '^## ' /tmp/jenkins-rca-last-prompt.txt
grep -c "retrieved.rag" /tmp/jenkins-rca-last-prompt.txt   # confirm RAG inject fired

# Unset when done (so prompts don't pile up in /tmp)
sudo systemctl unset-environment JENKINS_RCA_DEBUG_PROMPT
sudo systemctl restart jenkins-rca
```

---

## 6. systemd override — env var overrides without code changes

jenkins-rca reads secrets from AWS Secrets Manager at boot (via
`jenkins-rca-start.sh` calling `python -m jenkins_rca.secrets`). To override
any of those env values without touching the secret (e.g. point at a
different branch for one build's RCA), use a systemd drop-in:

```bash
# Edit override (creates /etc/systemd/system/jenkins-rca.service.d/override.conf)
sudo systemctl edit jenkins-rca

# Paste between the marker lines (example — branch override + debug):
[Service]
Environment="JENKINS_RCA_JP_BRANCH=release/REQ-463-staggerprodplusupdate-v2"
Environment="JENKINS_RCA_DEBUG_PROMPT=1"

# Apply
sudo systemctl daemon-reload
sudo systemctl restart jenkins-rca

# View the active override
sudo cat /etc/systemd/system/jenkins-rca.service.d/override.conf

# Drop the override entirely
sudo rm /etc/systemd/system/jenkins-rca.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart jenkins-rca
```

If `sudo systemctl edit` opens an empty editor that won't save, write
the file directly:

```bash
sudo mkdir -p /etc/systemd/system/jenkins-rca.service.d
sudo tee /etc/systemd/system/jenkins-rca.service.d/override.conf > /dev/null <<EOF
[Service]
Environment="JENKINS_RCA_PG_PASSWORD=<hex>"
Environment="JENKINS_RCA_PG_HOST=127.0.0.1"
Environment="JENKINS_RCA_PG_DB=jenkins_rca"
Environment="JENKINS_RCA_PG_USER=jenkins_rca"
Environment="JENKINS_RCA_PG_PORT=5432"
EOF
sudo systemctl daemon-reload && sudo systemctl restart jenkins-rca
```

---

## 7. Postgres ops

```bash
# Sanity — extensions + tables present
sudo -u postgres psql -d jenkins_rca -c '\dx'    # expect: vector, pg_trgm, plpgsql
sudo -u postgres psql -d jenkins_rca -c '\dt'    # expect: query_emb_cache, rca_chunks, retrieval_cache

# Row counts per source_type
sudo -u postgres psql -d jenkins_rca -c "
  SELECT source_type, count(*)
  FROM rca_chunks
  GROUP BY 1 ORDER BY 1;"

# Cache hit-rate (after some traffic)
sudo -u postgres psql -d jenkins_rca -c "
  SELECT 'query_emb' AS layer, sum(hits) AS hits, count(*) AS rows
  FROM query_emb_cache
  UNION ALL
  SELECT 'retrieval', sum(hits), count(*)
  FROM retrieval_cache;"

# Reset role password if forgotten / leaked
NEW_PG_PASS=$(openssl rand -hex 24)
sudo -u postgres psql -c "ALTER ROLE jenkins_rca WITH PASSWORD '$NEW_PG_PASS';"
echo "NEW pg_password: $NEW_PG_PASS"
# Then push to secret (write profile) OR set as systemd override (see §6).

# Connect with the service role (smoke)
PGPASSWORD="$JENKINS_RCA_PG_PASSWORD" psql -h 127.0.0.1 -U jenkins_rca -d jenkins_rca -c '\dt'
```

### Wipe and re-index from scratch

```bash
python -m jenkins_rca.rag reset --yes
python -m jenkins_rca.rag index-docops
python -m jenkins_rca.rag index-audits /var/log/jenkins-rca
```

---

## 8. Secrets — read / update Secrets Manager

`jenkins-rca/prod` is the secret. Keys: `jenkins_url`, `jenkins_user`,
`jenkins_token`, `webhook_secret`, `llm_provider`, `llm_api_key`,
`github_pat`, `jira_url`, `jira_user`, `jira_api_token`,
`pg_password`, `pg_host`, `pg_db`, `pg_user`, `pg_port`.

```bash
# Read (any profile with secretsmanager:GetSecretValue works)
aws secretsmanager get-secret-value \
  --secret-id jenkins-rca/prod \
  --region ap-south-1 \
  --profile <readonly-profile> \
  --query SecretString --output text | jq 'keys'

# View one key only (don't print in shared chat)
aws secretsmanager get-secret-value \
  --secret-id jenkins-rca/prod --region ap-south-1 --profile <readonly-profile> \
  --query SecretString --output text | jq -r '.pg_password' | wc -c
# expect length around 48 + newline

# Update one key (needs WRITE profile)
CURRENT=$(aws secretsmanager get-secret-value \
  --secret-id jenkins-rca/prod --region ap-south-1 --profile <write-profile> \
  --query SecretString --output text)
MERGED=$(echo "$CURRENT" | jq --arg p "<new-value>" '.<key-name> = $p')
aws secretsmanager put-secret-value \
  --secret-id jenkins-rca/prod --region ap-south-1 --profile <write-profile> \
  --secret-string "$MERGED" > /dev/null && echo "updated"

# Pull all keys into current shell as JENKINS_RCA_<KEY> env vars (for ad-hoc CLI)
eval "$(aws secretsmanager get-secret-value \
  --secret-id jenkins-rca/prod --region ap-south-1 \
  --query SecretString --output text \
  | jq -r 'to_entries[] | "export JENKINS_RCA_\(.key | ascii_upcase)=\"\(.value)\""')"
```

⚠️ **Don't paste secret values into chat, slack, tickets, or commit
messages.** If a value lands somewhere visible, rotate it.

---

## 9. End-to-end: redeploy after a code change

Typical sequence after you push a new commit to a bbctl branch:

```bash
# On EC2
cd /home/ubuntu/project/Jenkins-rca
git pull
sudo find jenkins_rca -name '__pycache__' -exec rm -rf {} + 2>/dev/null
sudo systemctl restart jenkins-rca
sleep 3
sudo systemctl status jenkins-rca --no-pager | head -5

# If docops/ content changed, re-index
source .venv/bin/activate
eval "$(aws secretsmanager get-secret-value \
  --secret-id jenkins-rca/prod --region ap-south-1 \
  --query SecretString --output text \
  | jq -r 'to_entries[] | "export JENKINS_RCA_\(.key | ascii_upcase)=\"\(.value)\""')"
python -m jenkins_rca.rag index-docops

# Smoke test
curl -sX POST http://localhost:7070/v1/rca \
  -H 'Content-Type: application/json' \
  -d '{"job":"Stagger Prod Plus One","build":5177,"deep":true}' \
  | jq '{error_class, cost_usd, agent_tool_calls}'
```

---

## 10. Health checks + quick diagnostics

```bash
# Is the service up?
curl -sI http://localhost:7070/health  # if /health exists; else:
sudo systemctl is-active jenkins-rca

# Process inspection
ps -eo pid,lstart,cmd | grep uvicorn | grep -v grep
sudo cat /proc/$(pgrep -f 'uvicorn jenkins_rca' | head -1)/environ \
  | tr '\0' '\n' | grep JENKINS_RCA_ | wc -l   # expect ≥10 JENKINS_RCA_ env vars

# Recent failures in service log
sudo journalctl -u jenkins-rca --since "1 hour ago" --no-pager \
  | grep -iE 'error|exception|traceback|skipped' | tail -20

# What branch is the local jenkins_pipeline clone on?
cd /home/ubuntu/project/Jenkins-rca/repos/jenkins_pipeline
git branch --show-current
git log --oneline -3

# What branch is bbctl itself on?
cd /home/ubuntu/project/Jenkins-rca
git branch --show-current
git log --oneline -3

# Is jenkins-rca-sync cron firing?
sudo tail -30 /var/log/jenkins-rca/sync.log
cat /etc/cron.d/jenkins-rca-sync
```

---

## 11. Common operations — quick recipes

### Re-RCA a build with full prompt dump

```bash
sudo systemctl set-environment JENKINS_RCA_DEBUG_PROMPT=1
sudo systemctl restart jenkins-rca
sleep 3
curl -sX POST http://localhost:7070/v1/rca \
  -H 'Content-Type: application/json' \
  -d '{"job":"<Job Name>","build":<N>,"deep":true}' > /tmp/rca_out.json
jq '{error_class, summary, files_read, cost_usd}' /tmp/rca_out.json
grep -E '^## ' /tmp/jenkins-rca-last-prompt.txt
sudo systemctl unset-environment JENKINS_RCA_DEBUG_PROMPT
sudo systemctl restart jenkins-rca
```

### Force-fresh docs end-to-end (clean slate, no cache)

```bash
sudo bash /home/ubuntu/project/Jenkins-rca/infra/scripts/bbctl-sync.sh
source /home/ubuntu/project/Jenkins-rca/.venv/bin/activate
cd /home/ubuntu/project/Jenkins-rca
python -m jenkins_rca.rag reset --yes
python -m jenkins_rca.rag index-docops
python -m jenkins_rca.rag index-audits /var/log/jenkins-rca
sudo systemctl restart jenkins-rca
```

### Validate a config.json change is live in the service

```bash
# After committing config.json on the jenkins_pipeline tracked branch:
sudo bash /home/ubuntu/project/Jenkins-rca/infra/scripts/bbctl-sync.sh
# Service restart is in the sync trap. Verify content:
grep -A3 '"my-service"' /home/ubuntu/project/Jenkins-rca/repos/jenkins_pipeline/resources/config.json
```

### Operator decides "wrong RCA" — what to do

1. Capture trace: `cp /tmp/jenkins-rca-trace-<Job>-<N>.txt ~/`
2. Capture RCA output JSON
3. (Future R6) `POST /v1/rca/feedback` to mark verdict
4. For now: file an issue or DM with the trace + the actual fix.

---

## 12. Reference

- Service entry: `/home/ubuntu/project/Jenkins-rca/infra/scripts/jenkins-rca-start.sh`
- systemd unit: `/etc/systemd/system/jenkins-rca.service`
- Sync cron: `/etc/cron.d/jenkins-rca-sync`
- App code: `/home/ubuntu/project/Jenkins-rca/jenkins_rca/`
- Repos clone root: `/home/ubuntu/project/Jenkins-rca/repos/`
- Docops source: `/home/ubuntu/project/Jenkins-rca/docops/`
- Audit JSONs: `/var/log/jenkins-rca/<request-id>.json`
- Trace files: `/tmp/jenkins-rca-trace-<Job>-<N>.txt` + `/tmp/jenkins-rca-last-trace.txt`
- Prompt dump (debug): `/tmp/jenkins-rca-last-prompt.txt`
- Sync log: `/var/log/jenkins-rca/sync.log`
- Postgres data dir: `/var/lib/postgresql/16/main/`
- Secrets: AWS Secrets Manager → `jenkins-rca/prod` in `ap-south-1`

Companion docs:
- `bbctl/docs/rca/bbctlrca.md` — service design
- `bbctl/docs/rca/RAGflow.md` — RAG architecture
- `bbctl/docops/jenkins_pipelines_golden.md` — pipeline reference
