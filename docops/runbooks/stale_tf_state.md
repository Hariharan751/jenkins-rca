# Runbook: stale_tf_state

## What this class means

A `prodplusone` (or other "fresh-env") run was aborted **before any
terraform action** because the precheck `checkTerraformStateFile` found
that the env's S3 tfstate file is non-empty — it still contains rows
from a previous run that should have been destroyed.

This is NOT a terraform apply / plan / lock error. The pipeline never
called terraform. It refused to start.

Distinct from generic `terraform` class:
- `terraform` = apply/plan/lock failed (terraform ran and errored)
- `stale_tf_state` = precheck refused to run terraform at all

## Detect signals

- `Executing precheck: checkTerraformStateFile`
- `Stopping pipeline execution due to non-empty Terraform state`
- `Terraform state contains resources. Total resources: <N>`
- Followed by `Finished: NOT_BUILT` (precheck error uses
  `Pipeline error`, not stage failure → Jenkins records NOT_BUILT)
- Failed stage hint: `prodPlusOne` (precheck stage), or `Infra Prod+1`

## Why this happens

`prodplusone` (and `prod-scale`, `quick-deploy`) are **ephemeral envs**.
Each run is expected to:
1. Start with an empty S3 tfstate
2. Terraform creates new resources (ALB rule, TG, EC2)
3. After deploy/canary, `Destroy` stage tears them down + clears state

If the previous run failed mid-flight (canary fail, deploy timeout,
manual abort, JenkinsMaster crash), the `Destroy` stage may not have
run, leaving state rows behind. Next prodplusone run trips the
precheck guard.

## Pipeline source to cross-check (MANDATORY)

The precheck lives in the staggered_plugins shared library:
- `jenkins_pipeline/vars/precheck.groovy` — dispatcher for precheck
  functions; `checkTerraformStateFile` is one entry
- `jenkins_pipeline/vars/checkTerraformStateFile.groovy` (or inline in
  precheck.groovy) — does `aws s3 ls` then `aws s3 cp - | jq` of
  `s3://staggered-terraform/<service>/<env>/terrafrom.tfstate` and
  counts `.resources | length`

Confirm S3 path format with `repo_read_file("jenkins_pipeline",
"vars/precheck.groovy", 1, 200)` (or `repo_search jenkins_pipeline
"checkTerraformStateFile"`).

Note: the actual S3 key has the typo `terrafrom.tfstate` (not
`terraform.tfstate`). Do NOT correct the typo in commands — it must
match what the pipeline uses.

## Drill plan

1. From log, extract `service`, `env` (usually `prodplusone`),
   `aws_account` (from config dump), `Total resources: <N>`
2. `service_lookup(<service>)` → get `aws_account`, `aws_region`,
   correct AWS profile name
3. `repo_search("jenkins_pipeline", "checkTerraformStateFile")` →
   confirm exact S3 bucket + key format used
4. `aws_describe(s3, ListObjectsV2, {Bucket: "staggered-terraform",
   Prefix: "<service>/<env>/"})` → confirm tfstate exists + size
5. Inspect what resources are stuck: download + jq:
   `aws s3 cp s3://staggered-terraform/<svc>/<env>/terrafrom.tfstate -
   --profile <acct> | jq '.resources[] | {type, name}'`
6. Check for previous run history to understand why Destroy didn't run
   (canary fail? manual abort? — look at last completed build of same
   job for `Destroy` stage outcome)

## Action template

```
Finding: prodplusone precheck `checkTerraformStateFile` aborted for
         service <svc>. S3 tfstate
         `s3://staggered-terraform/<svc>/<env>/terrafrom.tfstate`
         contains <N> resource(s) left over from a prior incomplete
         run (likely the `Destroy` stage was skipped due to an earlier
         failure or manual abort).

Action — pick ONE based on what resources are stuck:

  Option A (RECOMMENDED — full cleanup, safe when resources orphaned):
    1. Confirm no live traffic depends on the orphan resources:
       aws s3 cp s3://staggered-terraform/<svc>/<env>/terrafrom.tfstate - \
         --profile <acct> | jq '.resources[] | {type, name, instances}'
    2. For each AWS resource shown (ALB rule, target group, EC2, etc.)
       confirm it can be deleted — cross-check it is NOT referenced by
       prod ALB listener or any active traffic config.
    3. Delete AWS resources first (ALB rule → target group → EC2 → ENI):
         aws elbv2 delete-rule --rule-arn <arn> --profile <acct>
         aws elbv2 delete-target-group --target-group-arn <arn> --profile <acct>
         aws ec2 terminate-instances --instance-ids <id> --profile <acct>
    4. Then clear the S3 state object:
         aws s3 rm s3://staggered-terraform/<svc>/<env>/terrafrom.tfstate \
           --profile <acct>
    5. Re-trigger the Jenkins job.

  Option B (state-only cleanup — when AWS resources already gone but
            state still references them):
    1. Download state, copy out the entries you want to keep (usually
       none for a fresh prodplusone), reconstruct empty state, upload:
         echo '{"version":4,"terraform_version":"1.x.x","serial":0,
                "lineage":"<keep-existing>","outputs":{},"resources":[]}' \
           | aws s3 cp - s3://staggered-terraform/<svc>/<env>/terrafrom.tfstate \
                 --profile <acct>
       (Preserve `lineage` from the original state file.)
    2. Re-trigger.

  Option C (re-run the Destroy stage of the prior failed build, if the
            job supports rebuild-from-stage):
    Jenkins UI → previous failed build → "Restart from Stage" → Destroy.
    If Destroy succeeds, state will be cleared correctly. Then re-trigger
    the new prodplusone build.

Verify:
  Re-run the pipeline; the precheck should print
  "Terraform state is empty or missing — proceeding" (or equivalent)
  and the Infra Prod+1 stage should begin terraform apply.
```

## Output schema notes

- `error_class: "stale_tf_state"`
- `failed_stage: "prodPlusOne"` (the precheck stage; actual Infra stage
  shows `skipped due to earlier failure(s)`)
- `evidence[]` must include:
  - `jenkins_log:<line>` with `Terraform state contains resources. Total
    resources: <N>` and the `Stopping pipeline execution...` line
  - `jenkins_pipeline/vars/precheck.groovy:<line>` — the
    checkTerraformStateFile dispatch
  - `jenkins_log:<line>` with the resolved S3 path (from the
    `aws s3 ls` precheck output)
  - `aws:s3(<bucket>/<key>)` confirming object size > 0

## Common pitfalls

- DO NOT clear state without first auditing the orphan resources —
  Option A step 1 is mandatory. Wiping state for resources that still
  exist in AWS leaves AWS-side orphans that future prodplusone runs
  cannot manage.
- DO NOT correct the `terrafrom.tfstate` typo in commands. The pipeline
  uses the misspelled key.
- DO NOT propose `terraform destroy` from the operator's laptop —
  state lineage must match; use the pipeline's Destroy stage rebuild,
  or the explicit AWS+S3 path above.
- DO NOT confuse with `terraform` class — that one's apply/plan errors,
  this one's a pre-apply guard.
- The pipeline finishes with `Finished: NOT_BUILT` (not FAILURE) because
  the precheck raises `error` instead of throwing — this means
  `post.failure` will NOT fire. Ensure `post.always` NOT_BUILT trap is
  wired (see item 52 in jenkins-rca.md) so this RCA actually runs.
