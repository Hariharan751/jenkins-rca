# Jenkins RCA

Auto-RCA service for failing Jenkins pipelines. Webhook → log classifier → RAG-augmented LLM agent loop → LangGraph quality gates → validators → audit JSON.

Target: < 60s end-to-end, ~$0.30 per RCA.

Dashboard: <https://jenkins-rca.jinka.in/rca/v1/dashboard>

## Architecture (one-shot)

```
Jenkins build fails
        ↓ POST /v1/rca (HMAC)
    main.py
        ↓ fetch console log
   classifier.py (regex YAML rules)
        ↓ error_class
   agent.py — _build_primer (4 parallel fetches)
        ↓
   agent loop — OpenAI gpt-4o function-calling
        ↓
   LangGraph ULTIMATUM gates (gates.py)
        ↓
   validators (drop hallucinated evidence, substitute slave IDs)
        ↓
   audit.py → /var/log/jenkins-rca/<request_id>.json
        ↓
   JSON response → Jenkins console + Slack rcaAlert
```

See `docs/rca/architecture.html` for the full picture and `docs/rca/jenkins-rca.md` for the development log.

> **Note — knowledge graph is per-ORG and per-flow.** The RAG store / knowledge graph is scoped per organization (account) and per pipeline flow. Each ORG and each job flow indexes into its own knowledge base; graphs are not shared or merged across accounts or flows. Runbooks and job-flow docs (`docops/`) are indexed per-flow so retrieval stays isolated to the failing pipeline's context.

## Layout

| Path | Owner |
|---|---|
| `jenkins_rca/` | Python service (FastAPI on port 7070, 2 uvicorn workers) |
| `jenkins_rca/agent.py` | OpenAI function-calling agent loop + primer + validators |
| `jenkins_rca/gates.py` | LangGraph StateGraph + 4 quality gates |
| `jenkins_rca/rag.py` | Postgres + pgvector RAG (text-embedding-3-small) |
| `prompts/` | LLM system prompts (rca_common · rca_agent_system · rca_system) |
| `docops/runbooks/` | Per-error-class drill plans (18 classes) |
| `docops/job_flows/` | Per-pipeline reference docs |
| `infra/scripts/` | Deploy + sync + secrets bootstrap scripts |
| `infra/systemd/jenkins-rca.service` | systemd unit |
| `classifier_rules.yml` | Regex first-match-wins (18 classes) |

## Deploy (EC2)

Single source of truth: `/home/ubuntu/project/Jenkins-rca/` on the service host. `git pull` is the deploy step.

```bash
# bootstrap
ssh ubuntu@<jenkins-rca-host>
git clone https://github.com/Hariharan751/jenkins-rca.git ~/project/Jenkins-rca
cd ~/project/Jenkins-rca
bash infra/scripts/jenkins-rca-deploy.sh

# subsequent deploys
cd ~/project/Jenkins-rca
git pull
sudo systemctl restart jenkins-rca
```

Cron-driven sync every 2h via `infra/scripts/jenkins-rca-sync.sh` (pulls reference repos, indexes RAG, restarts service when docops changes).

## Secrets

AWS Secrets Manager `jenkins-rca/prod` in `ap-south-1`. Bootstrap with:

```bash
bash infra/scripts/jenkins-rca-secrets-setup.sh
```

## Env vars (set by start script, all sourced from Secrets Manager)

- `JENKINS_RCA_LLM_API_KEY` · OpenAI key
- `JENKINS_RCA_JENKINS_URL` · `JENKINS_USER` · `JENKINS_TOKEN`
- `JENKINS_RCA_JIRA_URL` · `JIRA_USER` · `JIRA_API_TOKEN`
- `JENKINS_RCA_GITHUB_PAT`
- `JENKINS_RCA_WEBHOOK_SECRET` · HMAC for `/v1/rca/webhook`
- `JENKINS_RCA_PG_*` · Postgres connection for RAG store
- `JENKINS_RCA_SLACK_WEBHOOK_URL`
- `JENKINS_RCA_REPOS_DIR` · external git checkouts (default `/var/cache/jenkins-rca/repos`)

## Branches

| Branch | RAG | LangGraph | Use case |
|---|---|---|---|
| `feature/jenkins-rca-agent-only` | no | no | baseline / no-deps fallback |
| `feature/jenkins-rca-agent-RAG` | yes | no | rollback target |
| `feature/jenkins-rca-agent-RAG-LANG` | yes | yes | **production** |

## Migration history

Migrated out of public `bbctl` repo on 2026-05-26 because the service held internal prompts, pipeline knowledge, and infrastructure references that should not live in a public repo. See `docs/rca/jenkins-rca.md` for the full development log including the pre-migration phase work.
