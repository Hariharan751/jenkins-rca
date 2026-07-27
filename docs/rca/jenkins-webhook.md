# Wiring Jenkins → jenkins-rca webhook

Auto-trigger RCA on every failed build, with HMAC-signed payload.

## 1. Add Jenkins credential

In Jenkins:
1. Manage Jenkins → Credentials → System → Global → Add Credentials
2. Kind: **Secret text**
3. ID: **`bbctl-webhook-secret`** (exact)
4. Secret: the `webhook_secret` value from AWS Secrets Manager `jenkins-rca/prod`
   ```bash
   aws secretsmanager get-secret-value --secret-id jenkins-rca/prod \
     --region ap-south-1 --query SecretString --output text | jq -r .webhook_secret
   ```

## 2. Install httpRequest plugin (if missing)

Manage Jenkins → Plugins → Available → search "HTTP Request" → install.

## 3. Drop the shared lib

Copy `infra/jenkins/post_failure_rca.groovy` into `jenkins_pipeline/vars/`
as `triggerRcaWebhook.groovy`. Commit + push (existing nightly sync picks it up,
or push to master directly).

## 4. Hook one pilot pipeline

In **ONE** pipeline groovy file (e.g. `vars/createGreenInfra.groovy`), add to
the `post.failure` block:

```groovy
post {
    failure {
        script { triggerRcaWebhook() }
    }
}
```

Existing post.failure blocks? Append the `script { triggerRcaWebhook() }` line.

## 5. Verify

1. Trigger a failure on the pilot pipeline (or wait for one)
2. Check Jenkins console output for: `[jenkins-rca] webhook status=200`
3. Check Slack channel for RCA message
4. Check audit log on bbctl-ec2: `ls -t /var/log/jenkins-rca/ | head -5`

## 6. Rollout to remaining pipelines

After 5+ pilot runs prove stable, add the snippet to:
- `vars/createGreenInfra.groovy`
- `vars/deploy.groovy`
- `vars/canary.groovy`
- `vars/rollout.groovy`
- `vars/nonwebdeploy.groovy`
- `vars/destroyBlueInfra.groovy`
- `vars/rollback.groovy`

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Console: `webhook status=401` | HMAC mismatch | Secret in Jenkins ≠ secret in AWS. Re-copy from Secrets Manager. |
| `webhook status=429` | Daily cost cap | `add_spend` saturated `DAILY_COST_CAP=20.0`. Bump in `cache.py` or wait until UTC midnight. |
| `webhook status=500` | Service crash | `sudo journalctl -u jenkins-rca -n 50` on bbctl-ec2 |
| No Slack message | `slack_webhook_url` not set in secret | `aws secretsmanager update-secret ...` add field |
| `webhook error` only | bbctl-ec2 unreachable from Jenkins agent | Check SG: agent → port 7070 |
