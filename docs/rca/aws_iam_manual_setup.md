# AWS IAM Manual Setup — jenkins-rca cross-account read

**Goal:** Give the jenkins-rca service describe/list/get on EC2, ELB, SSM
across 4 AWS accounts so the LLM's AWS tools can run live infra checks.

**Host role:** `arn:aws:iam::735317561518:role/bbctl-backend-service`
(the EC2 instance role jenkins-rca runs as).

**Account map (locked):**

| Account name | Account ID | Role |
|---|---|---|
| zinka     | 735317561518 | host — jenkins-rca runs here. Policies attached directly to `bbctl-backend-service`. |
| bbfinserv | 075903075452 | target — create `BBCTLRcaReadOnly` role. |
| divum     | 597070799581 | target — create `BBCTLRcaReadOnly` role. |
| tzf       | 476114138058 | target — create `BBCTLRcaReadOnly` role. |

Setup is one-time, applied via AWS console (no Terraform).

---

## Step 1 — Host account (735317561518 / zinka)

jenkins-rca already runs here. No STS hop for AWS calls in this account —
just attach policies directly to the host role.

### 1a. Attach managed `ReadOnlyAccess`

1. AWS Console → IAM → Roles → `bbctl-backend-service`
2. Permissions tab → Add permissions → Attach policies
3. Search `ReadOnlyAccess` → check the AWS-managed policy
4. Attach

### 1b. Add inline policy: `jenkins-rca-ssm-send`

`ReadOnlyAccess` doesn't include SSM SendCommand. Add narrow inline:

1. Roles → `bbctl-backend-service` → Permissions tab
2. Add permissions → Create inline policy → JSON tab
3. Paste content of `policies/jenkins-rca-ssm-send.json` (this repo)
4. Name: `jenkins-rca-ssm-send` → Create policy

### 1c. Add inline policy: `jenkins-rca-cross-account-assume`

Lets the host role STS-assume into the other 3 target accounts.

1. Add permissions → Create inline policy → JSON tab
2. Paste content of `policies/jenkins-rca-cross-account-assume.json`
3. Replace the 3 placeholder account IDs with the real ones
4. Name: `jenkins-rca-cross-account-assume` → Create policy

---

## Step 2 — Each target account (bbfinserv, divum, tzf)

Repeat 3 times, once per account.

### 2a. Create role `BBCTLRcaReadOnly`

1. Switch to target account → IAM → Roles → Create role
2. Trusted entity type: **Another AWS account**
3. Account ID: `735317561518` (the host account)
4. Skip MFA / external ID
5. Permissions: search `ReadOnlyAccess` → check → Next
6. Role name: `BBCTLRcaReadOnly`
7. Create role

### 2b. Tighten trust policy

The default trust opens to the whole 735317561518 account. Narrow to the
specific host role:

1. Roles → `BBCTLRcaReadOnly` → Trust relationships tab
2. Edit trust policy → paste content of `policies/BBCTLRcaReadOnly-trust.json`
3. Update policy

### 2c. Add inline SSM policy

1. Permissions tab → Add permissions → Create inline policy → JSON
2. Paste content of `policies/jenkins-rca-ssm-send.json` (same file as host)
3. Name: `jenkins-rca-ssm-send` → Create policy

---

## Step 3 — Smoke test from EC2

After all 4 setups complete, on the jenkins-rca host:

```bash
# A) Local account — direct describe (no STS)
aws sts get-caller-identity
# Expect: Account=735317561518, Arn=...:role/bbctl-backend-service
aws ec2 describe-instances --max-results 5 --region ap-south-1 \
  --query 'Reservations[*].Instances[*].InstanceId' --output table

# B) Cross-account — assume + describe (run for each of 3 target accounts)
for ACCT in 075903075452 597070799581 476114138058; do  # bbfinserv, divum, tzf
  echo "=== Account $ACCT ==="
  CREDS=$(aws sts assume-role \
    --role-arn "arn:aws:iam::$ACCT:role/BBCTLRcaReadOnly" \
    --role-session-name jenkins-rca-test \
    --duration-seconds 900 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text)
  read -r AK SK ST <<<"$CREDS"
  AWS_ACCESS_KEY_ID=$AK AWS_SECRET_ACCESS_KEY=$SK AWS_SESSION_TOKEN=$ST \
    aws sts get-caller-identity
  AWS_ACCESS_KEY_ID=$AK AWS_SECRET_ACCESS_KEY=$SK AWS_SESSION_TOKEN=$ST \
    aws ec2 describe-instances --max-results 3 --region ap-south-1 \
    --query 'Reservations[*].Instances[*].InstanceId' --output text
done
```

Expected: each block prints the target account ID + a non-empty instance list.

Failures:
- `AccessDenied` on AssumeRole → trust policy in target account doesn't list
  bbctl-backend-service ARN. Fix `BBCTLRcaReadOnly-trust.json`.
- `UnauthorizedOperation` on describe-instances → ReadOnlyAccess not attached
  in target account. Re-attach in step 2a.

---

## What this enables

After this setup the jenkins-rca agent loop can call these tools without
extra IAM work:

| Tool | API used | Permission source |
|---|---|---|
| aws_describe_target_health | elasticloadbalancing:DescribeTargetHealth | ReadOnlyAccess |
| aws_describe_target_group | elasticloadbalancing:DescribeTargetGroups | ReadOnlyAccess |
| aws_describe_instance | ec2:DescribeInstances | ReadOnlyAccess |
| aws_describe_listener_rule | elasticloadbalancing:DescribeRules | ReadOnlyAccess |
| aws_run_ssm_command | ssm:SendCommand + GetCommandInvocation | inline (this doc) |

Future read APIs (CloudWatch, Logs, RDS, etc.) auto-covered by
ReadOnlyAccess — no IAM change required when adding new tools.

---

## Files in this repo

- `bbctl/docs/rca/policies/jenkins-rca-ssm-send.json` — inline policy (used in both host + target accounts)
- `bbctl/docs/rca/policies/jenkins-rca-cross-account-assume.json` — host-account inline policy listing 3 target role ARNs
- `bbctl/docs/rca/policies/BBCTLRcaReadOnly-trust.json` — target-account role trust policy
