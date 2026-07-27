# Runbook: terraform

## What this class means
Terraform (run from the `Infra` stage) failed to plan/apply. Could be
state drift, syntax error in `.tf`, AWS API quota/permission issue, or
resource already exists.

## Detect signals
- `Error: aws_<resource>.<name> already exists` (already-exists conflict)
- `Error: <provider> error:` (Terraform provider error)
- `terraform plan` exit != 0
- `Error: Reference to undeclared input variable`
- Failed stage = "Infra"

## When to RECLASSIFY out of this runbook

Terraform is often just the messenger. The actual cause may be an AWS
quota / IAM / config issue surfaced THROUGH terraform. Reclassify when:

| If the Error line contains ...                                | error_class    | Use runbook |
|---------------------------------------------------------------|---------------|-------------|
| `TooManyUniqueTargetGroupsPerLoadBalancer`                    | aws_limit     | aws_limit.md|

**For `TooManyUniqueTargetGroupsPerLoadBalancer` — ALB ARN derivation (use immediately, do NOT emit placeholder):**
Derive ALB ARN from `service.lookup.rule_arn`:
- `rule_arn` format: `arn:aws:elasticloadbalancing:<region>:<acct>:listener-rule/app/<alb-name>/<alb-id>/<listener-id>/<rule-id>`
- ALB ARN = `arn:aws:elasticloadbalancing:<region>:<acct>:loadbalancer/app/<alb-name>/<alb-id>`
- Example: `rule_arn` contains `listener-rule/app/prod-private-internal-alb/fdbcf4c344dbed6d/...` → `alb_arn` = `arn:...loadbalancer/app/prod-private-internal-alb/fdbcf4c344dbed6d`
- Quota code for "Target groups per ALB": `L-417A185B`
| `LimitExceeded` / `Service quota exceeded`                    | aws_limit     | aws_limit.md|
| `VcpuLimitExceeded` / `InstanceLimitExceeded`                 | aws_limit     | aws_limit.md|
| `AccessDenied` / `UnauthorizedOperation`                      | aws_limit (perms sub-mode) | aws_limit.md |
| `Stale state detected — auto-destroying` (alone, no Error:)   | NOT a failure — keep scanning down for the real Error: | — |
| `Executing precheck: checkTerraformStateFile` + `Stopping pipeline execution due to non-empty Terraform state` | stale_tf_state | stale_tf_state.md |

The "Stale state detected" line is informational chatter emitted by
`precheck.groovy` — it's a NORMAL recovery step that runs BEFORE the
actual terraform apply. If the actual apply later succeeds, the stale
state cleanup is fine. If the apply fails, the Error: line below has
the real cause; do NOT stop at the stale-state line.

## Pipeline source to cross-check (MANDATORY)

Both infra helpers call an `infraComposer()` function that clones the
InfraComposer repo at runtime and runs terraform from within it:

- `(Infra)` stage → `jenkins_pipeline/vars/createGreenInfra.groovy`
  reads `infraComposer(service)` → clones InfraComposer →
  `cd config/<service>/prod/` → terraform init/plan/apply
- `(Infra Prod+1)` stage → `jenkins_pipeline/vars/createRuleForProdPlusOne.groovy`
  reads `infraComposer(service)` → clones InfraComposer →
  `cd config/<service>/prodplusone/` → terraform init/plan/apply

InfraComposer repo layout (use `repo_read_file("InfraComposer", ...)` to read):
- `config/<service>/<env>/main.tf` — root module; declares which
  module from `module/<name>/` to call, plus input variables
- `config/<service>/<env>/variable.tf` — variable declarations for this env
- `module/<name>/main.tf` — the module; typically composes sub-modules
  (e.g. ec2, target-group, tg-attachment, listener-rule sub-modules)

Available env dirs in `config/<service>/`: `prod`, `prodplusone`,
`prod-scale`, `quick-deploy`. Derive the env from the failed stage name.

## Drill plan
1. `get_jenkins_job_config(job)` → confirm scriptPath
2. From log, identify the failing helper: `createGreenInfra.groovy`
   (Infra stage) or `createRuleForProdPlusOne.groovy` (Infra Prod+1);
   read it to find the `infraComposer()` call and the terraform vars passed
3. From log, extract the offending resource address and terraform error
4. Derive service name + env dir from build params or failed stage
5. `repo_read_file("InfraComposer", "config/<service>/<env>/main.tf", 1, 60)`
   → see which module is invoked and what input vars are wired
6. `repo_read_file("InfraComposer", "module/<module>/main.tf", 1, 60)`
   → inspect the module; follow any sub-module `source` refs if error
   points deeper
7. If resource-exists / state-drift conflict: call appropriate
   `aws_describe` for the resource type to confirm current AWS state
8. `repo_recent_commits("InfraComposer", 10)` — check for recent module
   changes that may have introduced the regression

## Action template
```
Finding: Terraform error during <action> on <resource>:
         "<exact error message>".
         <If 'already exists'>: AWS has resource <id>/<name> but
         Terraform state doesn't track it. Either someone created it
         outside Terraform, or state was lost.
         <If syntax/variable error>: <module-or-config>:<line> has
         <description>.
         <If quota>: AWS service quota <name> exceeded; see aws_limit runbook.

Action:
  <If 'already exists'>:
    Option A (RECOMMENDED — preserves resource history + audit trail):
      `terraform import` brings the existing AWS resource into state
      WITHOUT touching the resource itself. No traffic disruption, no
      lost tags/attachments. ALWAYS try this BEFORE Option B.
      Step 1 — Derive the existing-id:
        # For ELBv2 Target Group:
        aws elbv2 describe-target-groups --names <tg-name> \
          --region <region> --profile <acct> \
          --query 'TargetGroups[0].TargetGroupArn' --output text
        # For EC2 instance: aws ec2 describe-instances ... --query ...InstanceId
        # For ALB: aws elbv2 describe-load-balancers --names <name> ...
      Step 2 — Import into state:
        cd <InfraComposer>/config/<service>/<env>/
        terraform import <resource-address-from-error> <existing-id>
        # resource-address is the dotted path from the error message,
        # e.g. module.createProdPlusOneInfra.module.createNewTg.aws_lb_target_group.tg_v1
      Step 3 — Verify state now tracks it:
        terraform plan   # should show no changes (or only safe drift)
      Step 4 — Re-run pipeline.

    Option B (FALLBACK — only when Option A fails or resource is genuinely orphan):
      If `terraform import` returns "already managed" or the resource
      is confirmed orphan (no live traffic, no upstream refs), delete
      AWS resource then re-run pipeline so Terraform creates fresh:
        # Always derive ARN inline, never emit <arn> placeholder:
        TG_ARN=$(aws elbv2 describe-target-groups --names <tg-name> \
          --region <region> --profile <acct> \
          --query 'TargetGroups[0].TargetGroupArn' --output text)
        # Pre-check: confirm not attached to any active listener rule
        aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
          --region <region> --profile <acct>
        # If healthy targets present, STOP — this is live, not orphan.
        # If empty / draining only:
        aws elbv2 delete-target-group --target-group-arn "$TG_ARN" \
          --region <region> --profile <acct>
      Then re-run pipeline.
  <If syntax/variable error>:
    Edit <file>:<line> in InfraComposer repo, fix the syntax, commit,
    push, re-run pipeline.
  <If quota>:
    See aws_limit runbook — request quota increase in AWS console.
Verify:
  Re-run pipeline; expect Infra stage to complete past the error.
```

## Output schema notes
- `error_class: "terraform"`
- `failed_stage: "Infra"`
- `evidence[]` must include:
  - `jenkins_log` with terraform error
  - `jenkins_pipeline/vars/createGreenInfra.groovy:<line>` or
    `jenkins_pipeline/vars/createRuleForProdPlusOne.groovy:<line>` — the caller
  - `InfraComposer/config/<service>/<env>/main.tf:<line>` — root config
  - `InfraComposer/module/<name>/main.tf:<line>` — module with failing resource
  - For 'already exists': `aws:<resource>(<id>)` confirming AWS state

## Common pitfalls
- DO NOT suggest `terraform destroy` as a fix — destructive and rarely correct.
- DO NOT cite a TF file you didn't open via `repo_read_file`.
- DO NOT emit `<arn>` / `<tg_arn>` / `<existing-id>` placeholders in
  `suggested_commands`. Always derive via `aws elbv2 describe-*
  --query ... --output text` chained into the delete/import command.
  Placeholder commands are unusable to the operator.
- DO NOT skip Option A. `terraform import` is ALWAYS safer than delete
  + recreate. Only fall back to Option B if import fails OR you have
  positive confirmation the resource is orphan (zero healthy targets,
  no listener refs).

## Deeper reading

For full state-surgery procedures (state lock recovery, lineage repair,
manual state edits, cross-env state migration), ALSO call
`read_doc("TerraformTroubleshoot")` — covers scenarios beyond this
runbook's drill plan.
