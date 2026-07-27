# Job flow: hotfix-noncanary

## Identity

- **Script path:** `jenkins_pipeline/hotfix-noncanary.groovy`
- **Likely Jenkins job names:** `Hotfix-Deploy`, `hotfix-noncanary`, `hotfix`
- **Shared library:** `staggered_plugins@master` (loaded INSIDE the `Load Library` stage, not via `@Library` annotation)
- **Agent / options:** `agent any`; `AWS_REGION='ap-south-1'`; `ansiColor('xterm')`

## Match

- `script_path` ends with `hotfix-noncanary.groovy`, OR
- `inline_script` contains stage bodies calling `pre_deployment(...)`,
  `instance_provisioning(...)`, `artifact_deployment(...)`,
  `health_validation(...)`, `cutover_cleanup(...)`, with `hotfix_rollback()`
  in `post { failure { ... } }`.

## Parameters

| Param | Type | Default | Notes |
|---|---|---|---|
| `SERVICE` | active-choice single-select | — | hardcoded ~100 services |
| `JFROG_BUILD` | reactive single-select | `'Select jar'` | runs `jf rt s 'Blackbuck/java/${SERVICE}/sha/staggered/*/*.zip'` to populate latest 10 |
| `backup` | boolean | false | "Create AMI Backup of healthy OLD instance?" |
| `COMMIT_ID` | string | `''` | only used when building a new jar |
| `Jira-Ticket` | string | `''` | HOTFIX ticket id |

## Stages

| # | Stage marker in console | Helper / inline |
|---|---|---|
| 1 | `(Load Library)` | `buildName "${params.SERVICE}"`; `library "staggered_plugins@master"` |
| 2 | `(Jira Details)` | `JiraDetails(params.SERVICE, params.COMMIT_ID, params['Jira-Ticket'])` |
| 3 | `(Input Validation)` | inline: `JFROG_BUILD=='Select jar' && COMMIT_ID==''` → error; both set → error (xor) |
| 4 | `(Build Artifact)` | `buildJob.call(params.SERVICE, params.COMMIT_ID)` — **only when** `JFROG_BUILD=='Select jar' && COMMIT_ID?.trim()` |
| 5 | `(Pre-Deployment)` | `pre_deployment.call(SERVICE, JFROG_BUILD, COMMIT_ID, backup)` — sub-stages `1.1 Validate Parameters` + `1.2 Load Service Configuration` + `1.3 Validate Config Resources` |
| 6 | `(Instance Provisioning)` | `instance_provisioning.call(params.SERVICE)` |
| 7 | `(Artifact Deployment)` | `artifact_deployment.call(SERVICE, JFROG_BUILD=='Select jar' ? env.JFROG_BUILD : params.JFROG_BUILD)` |
| 8 | `(Health Validation)` | `health_validation.call(SERVICE)` |
| 9 | `(Cutover & Cleanup)` | `cutover_cleanup.call(SERVICE, backup)` |

## Helper chain

```
pre_deployment
  ├─ libraryResource('config.json') + jq → env vars (SERVICE_NAME, AWS_REGION,
  │   CLUSTER, RULE_ARN, INSTANCE_TYPE, AMI_ID_BASE, SECURITY_GROUP_IDS, etc.)
  └─ sub-stages "1.1 Validate Parameters", "1.2 Load Service Configuration",
     "1.3 Validate Config Resources"
       └─ 1.3 runs `aws ec2 describe-images`, `aws ec2 describe-subnets`,
          `aws ec2 describe-security-groups`, `aws ec2 describe-key-pairs`,
          `aws iam get-instance-profile`. NotFound on any →
          "ERROR: Config resource validation failed" + lists the missing
          resource(s). This is NOT a health_check class failure even
          though the log message can look like one — the stage is config-
          validation, the helper is `pre_deployment`, NOT `health_validation`.
instance_provisioning
  ├─ validates env vars
  ├─ fallback AMI from config.json if env.AMI_ID_BASE invalid →
  │   `aws ec2 describe-images`
  └─ `aws ec2 run-instances` → registers in BLUE_TG_ARN →
     sets env.NEW_INSTANCE_IDS, env.OLD_INSTANCE_IDS
artifact_deployment
  ├─ `jf rt dl Blackbuck/java/${SERVICE}/sha/${JFROG_BUILD}` to
  │   /var/www/hotfix/${SERVICE}
  ├─ unzip → S3 upload (`blackbuck-deployments` bucket)
  └─ SSM execution on NEW_INSTANCE_IDS
health_validation
  └─ `aws elbv2 describe-target-health` against BLUE_TG_ARN within
     `timeout(10, MINUTES)` `waitUntil` loop
cutover_cleanup
  ├─ deregister OLD_INSTANCE_IDS from BLUE_TG (pre-checks they're
  │   registered; skips `unused` / `None` / `null`)
  ├─ optional AMI backup of healthy OLD instance (gated by `backup` param)
  └─ terminate OLD instances
hotfix_rollback (only fires from post.failure / post.aborted)
  ├─ deregister env.NEW_INSTANCE_IDS from env.BLUE_TG_ARN
  ├─ wait for drain (state=='unused', up to 30 attempts × 10s)
  └─ terminate NEW_INSTANCE_IDS — skips entirely if NEW_INSTANCE_IDS
     was never set (clean failure pre-provisioning)
```

## Post

| Result | Action |
|---|---|
| `always` | if `currentResult=='NOT_BUILT'` → `triggerRcaWebhook()` (BB-AI RCA) |
| `success` | `UpdateJiraStatus(params['Jira-Ticket'])` |
| `unstable` | `triggerRcaWebhook()` |
| `failure` | `hotfix_rollback()` then `triggerRcaWebhook()` |
| `aborted` | `hotfix_rollback()` |

## Stage → likely failure modes

| Stage marker | Error class | Drill |
|---|---|---|
| `(Load Library)` | scm, dependency | library branch / ref not resolvable |
| `(Jira Details)` | compliance | use `docops/runbooks/compliance.md` Modes 1-5 |
| `(Input Validation)` | (pipeline-level) | JFROG_BUILD vs COMMIT_ID xor violated |
| `(Build Artifact)` | scm, dependency, java_runtime | git fetch / maven dep / JAR build error |
| `(Pre-Deployment)` / sub-stage `1.1 Validate Parameters` | (pipeline-level) | xor + mandatory-param errors |
| `(Pre-Deployment)` / sub-stage `1.2 Load Service Configuration` | parse_error, config_validation | jq parse fail; missing config.json keys |
| `(Pre-Deployment)` / sub-stage `1.3 Validate Config Resources` | **config_validation** (NOT health_check) | AMI / subnet / SG / key-pair / IAM-profile NotFound. Drill `vars/pre_deployment.groovy`. Fix = update `config.json` to a real resource ID, OR create the missing AWS resource. Do NOT recommend "recreate target group" — that path is unrelated. |
| `(Instance Provisioning)` | aws_limit, config_validation | `RunInstances` quota; AMI / subnet / SG NotFound from `instance_provisioning` |
| `(Artifact Deployment)` | scm, ssm, network | JFrog 401 / 404, SSM SendCommand fail, S3 upload denial |
| `(Health Validation)` | health_check | TG never healthy; service crash; healthz 4xx/5xx |
| `(Cutover & Cleanup)` | aws_limit, ssm | deregister race; terminate denials |

## Before drilling — check recent commits

For any failure in this pipeline that traces back to code in
`jenkins_pipeline/` or `InfraComposer/`, call
`repo_recent_commits("jenkins_pipeline", 5)` (and
`repo_recent_commits("InfraComposer", 5)` when terraform / Infra /
Destroy stages are involved) BEFORE recommending a fix. If a recent
commit touched the file you would otherwise cite as the cause, open
the diff via `github_get_commit(<repo>, <sha>)` and read it — the code
may have moved underneath this doc. See
`docops/jenkins_pipelines_golden.md` §3 ("Universal rule") for detail.

## Gotchas (operator-relevant)

- Single-target-group flow (BLUE only — no green, no canary).
- `Build Artifact` is SKIPPED when a pre-built jar is selected — most hotfix runs.
- **Stage `1.3 Validate Config Resources` must NOT be classified as `health_check`.** The "Health Validation" stage is stage 8, not stage 5. Match the stage marker, not the word "health" in adjacent log lines.
- `triggerRcaWebhook()` is always wrapped in try/catch — RCA failures don't fail the pipeline.
- `hotfix_rollback` skips quietly if `NEW_INSTANCE_IDS` was never set — pre-provisioning failures leave no cleanup work.
