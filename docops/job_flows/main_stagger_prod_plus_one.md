# Job flow: main_stagger_prod_plus_one

## Identity

- **Script path:** `jenkins_pipeline/main_stagger_prod_plus_one.groovy`
- **Likely Jenkins job names:** `Stagger-Prod-Plus-One`, `stagger-deploy`, `main-prod-deploy`
- **Shared library:** `staggered_plugins@release/REQ-463-staggerprodplusupdate-v2`
- **Agent / options:** `agent any`; `tools { maven 'Maven' }`; `ansiColor('xterm')`
- **Environment:** `SUBMITTER_EMAILS = "thejasvi.bhat@..., rahul.aggarwal@..., vivekanand.matta@..."`

## Match

- `script_path` ends with `main_stagger_prod_plus_one.groovy`, OR
- `inline_script` contains stage bodies calling `buildJob(...)`,
  `prodPlusOne(...)`, `createGreenInfra(...)`, `deploy(...)`,
  `rollout(...)`, `destroyBlueInfra(...)` in that order, with
  `rollbackMain("Single Job Rollback", ...)` in `post { failure { ... } }`.

## Parameters

| Param | Type | Default |
|---|---|---|
| `COMMIT_ID` | string | `commit_id` |
| `SERVICE` | choice | hardcoded ~60 web services |
| `Jira-Ticket` | string | `''` |

## Stages

| # | Stage marker | Helper |
|---|---|---|
| 1 | `(Load Library)` | sets buildName + library |
| 2 | `(Jira Details)` | `JiraDetails(SERVICE, COMMIT_ID, Jira-Ticket)` |
| 3 | `(Build)` | `buildJob(SERVICE, COMMIT_ID)` |
| 4 | `(Prod+1)` | `prodPlusOne(SERVICE)` — on success sets `env.PROD_PLUS_ONE_COMPLETED = "true"` (this gates VictorOps paging in `post.failure`) |
| 5 | `(Infra)` | `createGreenInfra(SERVICE)` |
| 6 | `(Deploy)` | `deploy(SERVICE, "prod")` (web or non-web routing inside) |
| 7 | `(Rollout)` | `rollout(SERVICE)` (canary traffic shift) |
| 8 | `(Destroy)` | `destroyBlueInfra(SERVICE)` (post-cutover terminate old blue) |

## Helper chain

```
buildJob(SERVICE, COMMIT_ID)
  ├─ reads config.json (libraryResource)
  ├─ env[service+':branch_deploy'], env[service+':slack_channel']
  ├─ precheck.executePrechecks(SERVICE, ['Build'])    ← checkGitRepo
  ├─ JAVA_HOME by config.java_version (no account differentiation)
  └─ Notification.build → build JAR from git
prodPlusOne(SERVICE)
  ├─ precheck['prodPlusOne']                          ← checkTerraformStateFile
  ├─ stage("Infra Prod+1") {
  │     createRuleForProdPlusOne(service, 150)
  │       └─ validates rule_arn, lb_listener_arn, ami, aws_region,
  │          aws_account; INSTANCE_COUNT=1 hardcoded;
  │          createInfra(data, SERVICE, env['BUILD_ID'], priority=150)
  │  }
  ├─ stage("Deploy Prod+1") {
  │     deployProdPlusOne(service, "preprod")
  │       (catch → approvalProd1Failure())
  │  }
  ├─ stage("Automation") {
  │     triggerAutomation(service)
  │     Notification.qaSanityApproval
  │     approval()
  │  }
  └─ stage("Destroy Prod+1") { destroyPP1Infra(service) }
createGreenInfra(SERVICE)
  ├─ precheck['Infra']                                ← validateAmiExists +
  │                                                     getInstanceCountInGreenTargetGroup
  ├─ validates rule_arn, lb_listener_arn, ami, aws_region, aws_account
  ├─ defaults: DISK_SIZE=50, INSTANCE_CLASS=t3a.small
  └─ terraform creates green TG + N EC2s via
     createInfra(data, SERVICE, env['BUILD_ID'])
deploy(SERVICE, "prod")
  ├─ reads service_type from config
  ├─ if non-web → nonWebDeploy(SERVICE, "prod", serviceType)
  └─ else SSM-based parallel deploy with libraryResource scripts:
     app_restart, deploy_code, download_jar, healthy.sh,
     post_deploy_cleanup, prepare_jenkins, prepare_server, unhealthy.sh;
     templates filebeat.yml, supervisor.conf; optional fluent-bit.conf
rollout(SERVICE)
  ├─ precheck['Rollout']                              ← checkroutingeligibility
  ├─ if non-web → nonwebRollout(service)
  └─ else web canary loop:
     reads env[service+":blue_target_arn"] and env[service+":green_target_arn"]
     extracts hostnames via SSM (with SSH fallback) per TG
     canary(app_name, blue_tg_arn, region, account) once per
     traffic_value from config
canary(app_name, blue_tg_arn, region, account)
  ├─ SSM-with-SSH-fallback → extract blue instance hostnames
  ├─ writes canary.py from libraryResource
  └─ python3 ./canary.py --percent_rollout 10
     → exit 0 = pass, non-zero = rollback trigger
destroyBlueInfra(SERVICE)
  └─ terraform destroy on blue TG + old EC2s
```

## Post

| Result | Action |
|---|---|
| `always` | `NOT_BUILT` → `triggerRcaWebhook()`; `deleteDir()` |
| `success` | `UpdateJiraStatus(params['Jira-Ticket'])` |
| `unstable` | `triggerRcaWebhook()` |
| `failure` | (a) `rollbackMain("Single Job Rollback", params.SERVICE)`; (b) `triggerRcaWebhook()` capturing parsed RCA object; (c) post RCA to per-service Slack via `Notification.rcaAlert`; (d) IF `env.PROD_PLUS_ONE_COMPLETED=="true"` AND failure is NOT a canary failure → POST to VictorOps at `http://fms-vyakhya.alb.jinka.in/vyakhya/api/incident/create` with `{service_name:"devops", message_type:"CRITICAL", incidentKey:"jenkins_${SERVICE}_${BUILD_NUMBER}"}`. Canary-induced rollbacks (log contains `"Rollout back as Canary failed"` or `"Rolling Back as Result !=0"`) are EXCLUDED from VictorOps paging. |
| `aborted` | `rollbackMain("Single Job Rollback", params.SERVICE)` |

## Stage → likely failure modes

| Stage marker | Error class | Drill |
|---|---|---|
| `(Load Library)` | scm, dependency | library branch / ref not resolvable |
| `(Jira Details)` | compliance | Modes 1-5 in `docops/runbooks/compliance.md` (Mode 6 is NOT for this job) |
| `(Build)` | scm, dependency, java_runtime | git fetch / maven dep / JAR build error |
| `(Prod+1)` / `(Infra Prod+1)` | terraform, stale_tf_state, aws_limit | terraform apply errors; "already exists"; ALB rule limit |
| `(Prod+1)` / `(Deploy Prod+1)` | ssm, java_runtime, health_check | preprod SSM exec fail; app crash |
| `(Prod+1)` / `(Automation)` | timeout, dependency | QA-automation trigger fail; approval timeout |
| `(Prod+1)` / `(Destroy Prod+1)` | terraform | targeted destroy on prod+1 modules failed |
| `(Infra)` | terraform, stale_tf_state, aws_limit | terraform apply errors; "already exists"; TooMany TGs per ALB |
| `(Deploy)` | ssm, java_runtime, health_check | SSM exec fail; app launch crash; healthy.sh poll fail |
| `(Rollout)` | canary_fail, canary_script_error | NewRelic canary score < 80; canary.py crashed |
| `(Destroy)` | terraform, aws_limit | destroy fail; orphaned TGs |

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

- `env.PROD_PLUS_ONE_COMPLETED` gates VictorOps — pre-Prod+1 failures don't page on-call.
- Canary-induced rollbacks page neither VictorOps nor Slack-RCA-Phase-C (detected via log substring match).
- VictorOps payload uses fixed `service_name:"devops"` per API contract; `entityId = BUILD_NUMBER`.
- `triggerRcaWebhook.buildAlertMessage(rca)` is a nested call on the var — used to format the RCA summary line for the Slack post.
- Web pipeline only — non-web services go through `stagger-nonweb` (different pipeline).
