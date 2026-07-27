# Runbook: config_validation

## What this class means

A pipeline sub-stage that validates AWS resource ids from `config.json`
against the live AWS account found one (or more) ids that no longer
exist. The fix is data drift between `config.json` and the AWS account
— either the AWS resource was deleted/replaced and `config.json` was
not updated, OR `config.json` references a value from a different
account / region than the one the pipeline is running in.

This is NOT a `health_check` class failure. Even when the log message
contains words like "Validate", "Health", or shows a skipped
`Health Validation` stage downstream, the actual failure is the
**Validate Config Resources** sub-stage which runs BEFORE any
instance is provisioned and BEFORE any ALB target group probe.

## Detect signals

- `Config resource validation failed`
- `Key pair '<name>' not found in AWS`
- `Subnet '<id>' not found`
- `AMI '<id>' not found`
- `Security group <id> not found`
- `IAM profile '<name>' not found`
- Failed sub-stage marker `[Pipeline] { (1.3 Validate Config Resources)`
  (currently only emitted by `hotfix-noncanary`'s `pre_deployment` helper)
- `error_class` should be `config_validation`

## Pipeline source to cross-check (MANDATORY)

The check that fires this error class is:

- `jenkins_pipeline/vars/pre_deployment.groovy` — runs `aws ec2 describe-images`,
  `aws ec2 describe-subnets`, `aws ec2 describe-security-groups`,
  `aws ec2 describe-key-pairs`, `aws iam get-instance-profile` against the
  ids it reads from `jenkins_pipeline/resources/config.json` for the
  given `SERVICE`. Any NotFound aborts the stage.

NOT in scope:

- `vars/deployProdPlusOne.groovy`, `vars/createGreenInfra.groovy`,
  `vars/health_validation.groovy`, `vars/canary.groovy` — these run
  LATER in the pipeline (Deploy / Health Validation / Rollout stages)
  and are not the source of this failure. Do not drill into them for
  a config_validation RCA.

## Drill plan

1. From the log, identify which resource id is missing. The error line
   names the resource type AND the offending id (e.g.
   `Key pair 'blackbuck_production' not found in AWS`).
2. `service_lookup(<service>)` → confirm what `config.json` lists for
   that resource type for this service. Fields:
   - `ami` → AMI
   - `subnet_id` / `subnet_ids` → subnet(s)
   - `security_groups` → SGs
   - `key_name` → key pair
   - `instance_profile` → IAM instance profile
3. `aws_describe` the resource type at the service's
   `aws_account` + `aws_region` to confirm the id is genuinely absent
   (rule out region/account drift).
4. `repo_recent_commits("jenkins_pipeline", 10)` looking for recent
   `config.json` edits to this service — was the id changed recently?

## Action template

```
Finding: Config resource validation in (1.3 Validate Config Resources)
         on <pipeline> for service <SVC> failed. config.json declares
         <resource-type> = <id-from-config> for this service, but
         aws_describe(<resource-type> @ <account>/<region>) returns
         NotFound. The pipeline aborts before instance provisioning.

Action: Resolve the drift between config.json and AWS. Pick ONE:

  Option A (RECOMMENDED — fix config.json):
    Identify a real, current id for this resource in
    <account>/<region>. Update config.json[<SVC>].<key> to that id.
    Commit + push to the jenkins_pipeline master branch. Re-run the
    pipeline.

  Option B (create the missing AWS resource):
    Only if the resource SHOULD exist with the id config.json names.
    Recreate it in <account>/<region> matching the expected id. Verify
    with aws_describe. Re-run the pipeline.

Verify: After the fix, the (1.3 Validate Config Resources) sub-stage
        passes; the pipeline proceeds to (Instance Provisioning).
```

## Output schema notes

- `error_class: "config_validation"`
- `failed_stage: "Pre-Deployment"` (or `"1.3 Validate Config Resources"`
  if your output schema accepts sub-stages)
- `evidence[]` must include:
  - `jenkins_log` with the `Config resource validation failed` line
    and the specific NotFound resource id
  - `jenkins_pipeline/vars/pre_deployment.groovy:<line>` — the
    describe-* call that fired
  - `jenkins_pipeline/resources/config.json` — the offending field
    for this service (cite the resource type, not the full record)
  - `aws:<resource-type>(<id>)` — the NotFound response

## Common pitfalls

- **DO NOT classify as `health_check`.** Even when the log shows
  `Stage 'Health Validation' skipped due to earlier failure(s)` —
  that line is the AUTOMATIC skip-chain emitted by Jenkins for EVERY
  downstream stage of a failed build. The Health Validation stage
  was never reached.
- **DO NOT recommend recreating target groups, ALB rules, or running
  `terraform destroy`.** This failure is a config.json drift, not an
  infrastructure problem.
- **DO NOT recommend `aws elbv2 describe-*` calls** as part of the
  drill — they are unrelated to the offending resource type.
- **DO NOT cite `vars/deployProdPlusOne.groovy`, `vars/canary.groovy`,
  or `vars/health_validation.groovy`** in `evidence[]`. Those helpers
  are not on the call path for this failure.
- The fix is a `config.json` PR in `jenkins_pipeline`, NOT a code
  change in `vars/*.groovy`. The helpers correctly aborted the
  pipeline; the data they read was stale.
