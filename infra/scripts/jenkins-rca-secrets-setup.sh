#!/usr/bin/env bash
# One-shot: create AWS Secrets Manager entry for jenkins-rca.
# Run from operator workstation with AWS creds that can create secrets in ap-south-1.
#
# Usage:
#   export JENKINS_URL=...
#   export JENKINS_USER=...
#   export JENKINS_TOKEN=...
#   export WEBHOOK_SECRET=...
#   export LLM_PROVIDER=openai
#   export LLM_API_KEY=sk-...
#   export GITHUB_PAT=ghp_...
#   bash jenkins-rca-secrets-setup.sh
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
SECRET_ID="${JENKINS_RCA_SECRET_ID:-jenkins-rca/prod}"

: "${JENKINS_URL:?set JENKINS_URL}"
: "${JENKINS_USER:?set JENKINS_USER}"
: "${JENKINS_TOKEN:?set JENKINS_TOKEN}"
: "${WEBHOOK_SECRET:?set WEBHOOK_SECRET}"
: "${LLM_PROVIDER:?set LLM_PROVIDER (openai|gemini)}"
: "${LLM_API_KEY:?set LLM_API_KEY}"
: "${GITHUB_PAT:?set GITHUB_PAT}"

PAYLOAD=$(python3 -c '
import json, os
out = {
    "jenkins_url": os.environ["JENKINS_URL"],
    "jenkins_user": os.environ["JENKINS_USER"],
    "jenkins_token": os.environ["JENKINS_TOKEN"],
    "webhook_secret": os.environ["WEBHOOK_SECRET"],
    "llm_provider": os.environ["LLM_PROVIDER"],
    "llm_api_key": os.environ["LLM_API_KEY"],
    "github_pat": os.environ["GITHUB_PAT"],
}
# Optional fields — only include if set
for k in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN", "SLACK_WEBHOOK_URL"):
    if os.environ.get(k):
        out[k.lower()] = os.environ[k]
print(json.dumps(out))')

# Create or update
if aws secretsmanager describe-secret --secret-id "$SECRET_ID" --region "$REGION" >/dev/null 2>&1; then
  echo "==> updating existing secret $SECRET_ID"
  aws secretsmanager update-secret \
    --secret-id "$SECRET_ID" \
    --secret-string "$PAYLOAD" \
    --region "$REGION"
else
  echo "==> creating secret $SECRET_ID"
  aws secretsmanager create-secret \
    --name "$SECRET_ID" \
    --description "jenkins-rca runtime config" \
    --secret-string "$PAYLOAD" \
    --region "$REGION"
fi

echo ""
echo "==> done."
aws secretsmanager describe-secret --secret-id "$SECRET_ID" --region "$REGION" \
  --query "{Name:Name,ARN:ARN,LastChangedDate:LastChangedDate}" --output table
