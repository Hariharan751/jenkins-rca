# jenkins-rca secrets management

All runtime secrets live in **AWS Secrets Manager** in `ap-south-1`.

## Secret structure

Single secret `jenkins-rca/prod` (JSON):
```json
{
  "jenkins_url": "http://10.34.42.254:8080",
  "jenkins_user": "g.hariharan@blackbuck.com",
  "jenkins_token": "...",
  "webhook_secret": "...",
  "llm_provider": "openai",
  "llm_api_key": "sk-...",
  "github_pat": "ghp_..."
}
```

## First-time setup

### 1. Create the secret

```bash
export JENKINS_URL="http://10.34.42.254:8080"
export JENKINS_USER="g.hariharan@blackbuck.com"
export JENKINS_TOKEN="..."
export WEBHOOK_SECRET="..."
export LLM_PROVIDER="openai"
export LLM_API_KEY="sk-..."
export GITHUB_PAT="ghp_..."

bash infra/scripts/bbctl-secrets-setup.sh
```

### 2. Attach IAM policy to bbctl-ec2 instance role

Find the instance role:
```bash
aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=bbctl-backend-service" \
  --query "Reservations[].Instances[].IamInstanceProfile.Arn" \
  --region ap-south-1
```

Create policy:
```bash
aws iam create-policy \
  --policy-name jenkins-rca-secrets-read \
  --policy-document file://infra/iam/jenkins-rca-secrets-policy.json
```

Attach to role:
```bash
aws iam attach-role-policy \
  --role-name <bbctl-ec2-role-name> \
  --policy-arn arn:aws:iam::<acct>:policy/jenkins-rca-secrets-read
```

### 3. Verify from bbctl-ec2

```bash
aws secretsmanager get-secret-value \
  --secret-id jenkins-rca/prod \
  --region ap-south-1 \
  --query SecretString --output text | jq .
```

Should return the JSON.

## Rotation

Update secret in place — no code change needed:
```bash
# update single field via aws CLI
NEW_KEY="sk-..."
CURRENT=$(aws secretsmanager get-secret-value \
  --secret-id jenkins-rca/prod --region ap-south-1 \
  --query SecretString --output text)
UPDATED=$(echo "$CURRENT" | jq --arg k "$NEW_KEY" '.llm_api_key = $k')
aws secretsmanager update-secret \
  --secret-id jenkins-rca/prod \
  --secret-string "$UPDATED" \
  --region ap-south-1
sudo systemctl restart jenkins-rca
```

Or re-run `bbctl-secrets-setup.sh` with new env vars.

## How service reads secrets

On startup, `infra/scripts/jenkins-rca-start.sh`:
1. Calls `python -m jenkins_rca.secrets` (uses boto3 → Secrets Manager)
2. Prints `export JENKINS_RCA_<KEY>=<value>` lines
3. `eval` exports them into env
4. uvicorn inherits env, main.py reads via `os.environ`

Single network call at boot. No periodic polling. Restart service to pick up rotation.

## Removed: SOPS

Old SOPS-based flow (`/etc/jenkins-rca/keys.enc.yaml` + age key) is deprecated. Safe to delete after migration verified:
```bash
sudo rm -rf /etc/jenkins-rca
```
