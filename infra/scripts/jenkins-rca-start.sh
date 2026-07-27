#!/usr/bin/env bash
# Fetch secrets from AWS Secrets Manager, export as env vars, start uvicorn.
# Requires IAM role on bbctl-ec2 with secretsmanager:GetSecretValue on
# arn:aws:secretsmanager:ap-south-1:<acct>:secret:jenkins-rca/prod-*
set -euo pipefail

APP_DIR="/home/ubuntu/project/Jenkins-rca"
VENV="$APP_DIR/.venv"
export AWS_REGION="${AWS_REGION:-ap-south-1}"
export JENKINS_RCA_SECRET_ID="${JENKINS_RCA_SECRET_ID:-jenkins-rca/prod}"

cd "$APP_DIR"

# Fetch secrets via boto3 and export as env vars. Fail loud if fetch breaks.
SECRETS_OUTPUT=$("$VENV/bin/python" -m jenkins_rca.secrets) || {
  echo "ERROR: failed to fetch secrets from AWS Secrets Manager" >&2
  exit 1
}
eval "$SECRETS_OUTPUT"

exec "$VENV/bin/uvicorn" jenkins_rca.main:app --host 0.0.0.0 --port 7070 --workers 2 --log-level info --no-access-log
