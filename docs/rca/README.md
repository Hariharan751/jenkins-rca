# bbctl RCA — documentation index

| File | Purpose |
|---|---|
| [`workflow.md`](workflow.md) | End-to-end request flow with parallel-fetch + LangGraph routing diagrams |
| [`RAG_and_LangGraph.md`](RAG_and_LangGraph.md) | RAG (pgvector) + LangGraph (gates) design + ops |
| [`bbctlrca.md`](bbctlrca.md) | Operational doc — Jenkins integration, EC2 layout, secrets, deploy |
| [`cli_commands_RAG.md`](cli_commands_RAG.md) | Operator cheat sheet for `python -m jenkins_rca.rag *` |
| [`jenkins-webhook.md`](jenkins-webhook.md) | Webhook signature + payload spec |
| [`secrets.md`](secrets.md) | AWS Secrets Manager schema + rotation |
| [`aws_iam_manual_setup.md`](aws_iam_manual_setup.md) | One-time cross-account IAM setup (BBCTLRcaReadOnly + trust policies) |
| [`policies/`](policies/) | IAM trust + assume + SSM policy JSONs |

## Reading order for a new engineer

1. `bbctlrca.md` — what the service does, where it runs (5 min)
2. `workflow.md` — single-page mental model of every step (10 min)
3. `RAG_and_LangGraph.md` — the two engines in detail (20 min)
4. `cli_commands_RAG.md` — bookmark for operations
5. Then read code in order: `main.py → classifier.py → agent.py → gates.py → rag.py → tool_schemas.py → agent_dispatch.py`

## What it does (one paragraph)

Jenkins build fails → webhook to jenkins-rca on EC2 → service fetches
console log + classifies via regex → primer pre-fetches Jira ticket,
Jenkins node, RAG semantic memory → OpenAI gpt-4o function-calling
loop drills the failure via repo reads + AWS describe + runbook
fetch → LangGraph ULTIMATUM gates enforce runbook compliance →
validator hard gates drop hallucinated evidence → server-side
slave-ID substitution kills placeholder commands → final RCA JSON
returns to operator + persists as audit/<request_id>.json on disk
+ row in outcomes.sqlite. Audit JSONs feed back into RAG as
past-incident memory. Total: ~$0.30 per RCA, typical end-to-end
30-60 seconds.

## Status (May 2026)

Production branch: `feature/jenkins-rca-agent-RAG-LANG`.
17/18 classifier classes route through the LangGraph gate path.
Three branches kept for A/B:

| Branch | RAG | LangGraph | Use |
|---|---|---|---|
| `feature/jenkins-rca-agent-only`     | ✗ | ✗ | Baseline / no-deps env |
| `feature/jenkins-rca-agent-RAG`      | ✓ | ✗ | Rollback target (imperative gates) |
| `feature/jenkins-rca-agent-RAG-LANG` | ✓ | ✓ | **Production** |

Switch which branch the EC2 service tracks via the `JENKINS_RCA_BRANCH`
env override on `infra/scripts/bbctl-sync.sh`.

## Quick-reference: which file to edit for...

| To change | Edit |
|---|---|
| Drill plan for a class | `docops/runbooks/<class>.md` |
| Per-pipeline routing / stage table | `docops/job_flows/<pipeline>.md` |
| Cross-pipeline universal stage rules | `docops/jenkins_pipelines_golden.md` |
| Shared prompt rules (override / placeholder / evidence) | `prompts/rca_common.md` |
| Agent method (drill / narration / iter) | `prompts/rca_agent_system.md` |
| One-shot method | `prompts/rca_system.md` |
| Classifier regex | `classifier_rules.yml` |
| Add a new error class | classifier + runbook + AGENT_CLASSES + MANDATORY_RUNBOOK + re-index |
| Add a new ULTIMATUM gate | `jenkins_rca/gates.py:_check_<name>` + add_node + add_conditional_edges |
| New tool exposed to LLM | `jenkins_rca/tool_schemas.py` + `jenkins_rca/agent_dispatch.py` |
| Sync script cadence | `infra/scripts/bbctl-sync.sh` (every 2h via cron) |
