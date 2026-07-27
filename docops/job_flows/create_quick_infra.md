# Job flow: create-quick-infra

> **🚨 CRITICAL RULE — DO NOT recommend "add the service to config.json"
> for any failure on this job.** create-quick-infra is the BOOTSTRAP
> pipeline that CREATES the infra for a brand-new service. The service
> entry in `config.json` does NOT exist yet — that is the DESIGN, not
> the bug. Population of `config.json` happens LATER, after this job
> succeeds, when an operator runs the **`Stagger-Onboarding`** job
> (which writes the `config.json` entry referencing the infra this job
> created).
>
> If the agent's RCA for this job says "service X not found in
> config.json — add it to config.json", the RCA is WRONG. The real
> fix is one of:
>   1. **Compliance gate-logic regression** — the build-param fallback
>      in `vars/JiraDetails.groovy` was reverted/broken. Read recent
>      `jenkins_pipeline/` commits + fix the gate (see
>      `docops/runbooks/compliance.md` Mode 6).
>   2. **Service not onboarded to `team-board-mapping`** — the routing
>      check at `JiraDetails.groovy:46` returned no match. Register the
>      service in `team-board-mapping` first (Jira/GitHub PR depending
>      on repo conventions), then re-run.
>   3. **Jira ticket / build-param values** wrong — `Jira-Ticket`
>      empty, `SERVICE` typo, etc. Fix the param + re-trigger.
>
> Order of operations: `create-quick-infra` → `Stagger-Onboarding`
> → `Stagger Prod Plus One`. `config.json` enrichment lives between
> step 1 and step 3, owned by step 2. Step 1 cannot depend on the
> output of step 2.

## Identity

- **Script path:** `jenkins_pipeline/Jenkinsfile_create_quick_infra`
- **Likely Jenkins job names:** `create-quick-infra`, `quick-infra`, `create-quick-infra-onboarding`
- **Shared library:** `staggered_plugins@${libraryBranch}` — dynamic but currently hardcoded to `release/REQ-463-staggerprodplusupdate-v2`
- **Agent / options:** `agent any`; `tools { maven 'Maven' }`; `ansiColor('xterm')`

## Match

- `script_path` ends with `Jenkinsfile_create_quick_infra`, OR
- `inline_script` contains stage bodies calling `QuickBuildJob(...)`
  AND `QuickDeploy(...)` (the QuickBuildJob / QuickDeploy pair is
  distinctive — no other pipeline uses these).

## Parameters (reactive Active Choices)

| Param | Type | Notes |
|---|---|---|
| `IS_ONBOARDED` | choice [`No`, `Yes`] | gates show/hide of all auto-resolvable params |
| `Jira-Ticket` | string | mandatory; pipeline errors if empty |
| `SERVICE` | DynamicReferenceParameter | dropdown of ~135 services if onboarded=Yes; free-text input if No |
| `INSTANCE_COUNT` | choice [1..7] | frontends must be 1 |
| `COMMIT_ID` | ValidatingStringParameterDefinition | regex `^([0-9a-fA-F]{7,40})?$` |
| `JFROG_BUILD` | reactive | `jf rt s` jar listing; hidden when IS_ONBOARDED=No |
| `service_type` | hiddenChoice | `Java` / `Docker` |
| `jar_path`, `project_name`, `IAM_ROLE`, `SUBNET_IDS`, `SECURITY_GROUP_IDS`, `HEALTH_CHECK_URL` | hiddenString | hidden when IS_ONBOARDED=Yes |
| `APP_PORT`, `dockerfile_path` | visibleForFrontendOnly | Docker/frontend + IS_ONBOARDED=No |
| `git_repo`, `AWS_REGION`, `AMI_ID`, `INSTANCE_CLASS`, `slack_channel`, `business`, `team_name` | hiddenString | |
| `ACCOUNT` | hiddenChoice | `zinka` / `divum` / `finserv` / `tzf` |
| `SERVER_CMD`, `java_version`, `build_command`, `LOG_FILE_PATTERN`, `NEWRELIC_FILE_PATH`, `NEWRELIC_JAR` | hideForFrontend | hidden for Docker/frontend + IS_ONBOARDED=Yes |

## Script-scope state

- `effectiveParams` — mutable `Map` copy of `params` (Jenkins `params` is immutable)
- `instanceIds` — list of EC2 IDs returned by `CreateQuickInfra`
- `ALLOW_AWS_INFRA_DISCOVERY = false` — gate; currently only `health_check_url` is auto-discovered
- Helper `rollbackInstances(ids, region, account)` — terminates EC2s
- Helper `keyNameForAccount(account)` — `finserv → prod-finserv-key`, `tzf → production_tzf`, default → `blackbuck_production`

## Stages

| # | Stage marker | Helper / inline |
|---|---|---|
| 1 | `(Load Library)` | `buildName "${SERVICE}_${COMMIT_ID}"`; loads dynamic-branch library |
| 2 | `(Jira Details)` | `JiraDetails(params.SERVICE, params.COMMIT_ID, params['Jira-Ticket'])` — errors if Jira-Ticket empty |
| 3 | `(Resolve Parameters)` | large inline script: if `IS_ONBOARDED==Yes` parses libraryResource `config.json` and populates `effectiveParams`; falls back to `discoverInfraFromRuleArn` for `health_check_url` only. If `IS_ONBOARDED==No` validates broader mandatory list, derives `KEY_NAME` from ACCOUNT, sets `DISK_SIZE='50'`, derives `multi_project` from `jar_path` + `project_name`. Frontend / Docker → enforces `INSTANCE_COUNT==1`. |
| 4 | `(Input Validation)` | inline xor: `JFROG_BUILD` vs `COMMIT_ID` — **when** `service_type in ['web','non-web','non-web-cron','non-web-consumer','Java']` |
| 5 | `(Build)` | `QuickBuildJob(SERVICE, COMMIT_ID, effectiveParams)` — **when** Java service types + `JFROG_BUILD=='Select jar'` + `COMMIT_ID` set |
| 6 | `(Build Frontend)` | `buildJobFrontend(SERVICE, COMMIT_ID, effectiveParams)` — **when** `service_type in ['Docker','frontend']` |
| 7 | `(Infra)` | `instanceIds = CreateQuickInfra(SERVICE, effectiveParams)` |
| 8 | `(Deploy)` | `QuickDeploy(SERVICE, "prod", effectiveParams + [INSTANCE_IDS: instanceIds])` — **when** Java service types |
| 9 | `(Deploy Frontend)` | `QuickDeployFrontend(...)` — **when** Docker / frontend |

## Helper chain

```
QuickBuildJob(service, COMMIT_ID, params)
  ├─ if IS_ONBOARDED=='Yes' → loads config.json libraryResource
  ├─ precheck.executePrechecks('Build')                  ← onboarded path only
  ├─ JAVA_HOME by params.java_version + params.ACCOUNT
  │     (zinka / divum → amazon-corretto java21; others → openjdk)
  └─ Notification.build
buildJobFrontend(service, COMMIT_ID, params)
  └─ frontend artifact build (Docker / nodejs)
CreateQuickInfra(service, params)
  ├─ aws ec2 run-instances (count = INSTANCE_COUNT)
  └─ returns instance ID list
QuickDeploy(service, "prod", params)
  ├─ resolves JAR (params.JFROG_BUILD with 'staggered/' prefix stripped,
  │   else env[service+":jar_identifier"])
  ├─ S3 bucket by params.ACCOUNT (finserv → finserv-deployment, tzf →
  │   tzf-deployments, default → blackbuck-deployments)
  ├─ prepareLocalFiles() — writes deploy scripts
  └─ parallel SSM deploy to each instance in params.INSTANCE_IDS
QuickDeployFrontend(service, "prod", params)
  └─ frontend deploy variant
rollbackInstances(ids, region, account)  ← script-scope, defined in pipeline
  └─ aws ec2 terminate-instances (called from post.failure with input prompt)
```

## Post

| Result | Action |
|---|---|
| `always` | `NOT_BUILT` → `triggerRcaWebhook()`; then `deleteDir()` |
| `success` | `UpdateJiraStatus(params['Jira-Ticket'])` |
| `unstable` | `triggerRcaWebhook()` |
| `failure` | `triggerRcaWebhook()` → Slack RCA via `Notification.rcaAlert` → interactive `input message: 'Pipeline failed. Destroy provisioned infra?'` → `rollbackInstances(instanceIds, ...)`. **NO VictorOps page.** |
| `aborted` | `input` prompt → `rollbackInstances(...)` |

## Stage → likely failure modes

| Stage marker | Error class | Drill |
|---|---|---|
| `(Load Library)` | scm, dependency | library branch / ref not resolvable |
| `(Jira Details)` | **compliance Mode 6** (GATE BUG, NOT registration) | See `docops/runbooks/compliance.md` Mode 6. This job is the bootstrap path; missing `config.json` entry is the design (the service is new). The gate was patched to source SERVICE from build params — a failure here means the patch regressed. Do NOT recommend `vim config.json`. |
| `(Resolve Parameters)` | config_validation, parse_error | jq parse fail; mandatory params missing |
| `(Input Validation)` | (pipeline-level) | JFROG_BUILD vs COMMIT_ID xor violated |
| `(Build)` | scm, dependency, java_runtime | git fetch / maven dep / JAR build error |
| `(Build Frontend)` | dependency, java_runtime | npm / Docker build error |
| `(Infra)` | aws_limit, config_validation | `RunInstances` quota; AMI / subnet / SG NotFound |
| `(Deploy)` | ssm, java_runtime, health_check | SSM exec fail; app launch crash |
| `(Deploy Frontend)` | ssm, dependency | frontend artifact deploy fail |

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

- **Bootstrap job** — used to spin up infra for a NEW service that does NOT yet exist in `config.json`.
- The compliance gate in `vars/JiraDetails.groovy` has a build-param fallback for this job — `SERVICE` is sourced from the git build params and `config.json` is enrichment only. A `Compliance: SERVICE '<svc>' not found in config.json` failure on this job is a **gate-logic regression**, NOT a missing-entry bug. Verify the fallback is still on HEAD via `repo_recent_commits("jenkins_pipeline", 5)` before recommending a fix. See `docops/runbooks/compliance.md` Mode 6.
- No Prod+1, no canary, no Rollout stage. Single-shot provisioning + deploy.
- Frontend / Docker services bypass `QuickBuildJob` + `QuickDeploy`; use `buildJobFrontend` + `QuickDeployFrontend`.
- `discoverInfraFromRuleArn` is gated behind `ALLOW_AWS_INFRA_DISCOVERY=false`; today only `health_check_url` is auto-discovered.
- Rollback path has an interactive `input` prompt before destroy — pipeline can hang waiting for operator approval.
