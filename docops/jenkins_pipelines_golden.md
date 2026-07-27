# Jenkins Pipelines — Golden Index

Authoritative MAP across all production Jenkins pipelines at BlackBuck.
Use this doc to:

1. Identify which pipeline a failed build belongs to (§2).
2. Map the failing stage to the most-likely error class + drill plan (§3).
3. Look up a helper's signature, purpose, and callers (§4).
4. Jump into the per-pipeline detail doc for the full chain (links in §1).

This doc is the INDEX. Per-pipeline full content is split into
`docops/job_flows/<name>.md`. The agent should:

- read this doc first (cheap, ~200 lines),
- then `read_job_flow(<name>)` for the specific pipeline,
- then `repo_read_file("jenkins_pipeline", ...)` for the actual code.

QA-Automation is intentionally omitted — not in production use.

---

## 1. Pipeline catalogue (read the linked doc for full content)

| Pipeline | Script | Per-pipeline doc | One-line description |
|---|---|---|---|
| **hotfix-noncanary** | `hotfix-noncanary.groovy` | [`job_flows/hotfix_noncanary.md`](job_flows/hotfix_noncanary.md) | BLUE-only single-target-group hotfix; no canary; no Prod+1 |
| **create-quick-infra** | `Jenkinsfile_create_quick_infra` | [`job_flows/create_quick_infra.md`](job_flows/create_quick_infra.md) | Bootstrap job for NEW services; single-shot provisioning + deploy |
| **main_stagger_prod_plus_one** | `main_stagger_prod_plus_one.groovy` | [`job_flows/main_stagger_prod_plus_one.md`](job_flows/main_stagger_prod_plus_one.md) | Web canary deploy; Prod+1; VictorOps + Slack RCA on failure |
| **stagger-nonweb** | `stagger-nonweb.groovy` | [`job_flows/stagger_nonweb.md`](job_flows/stagger_nonweb.md) | Non-web (consumers / crons / kafka); time-based stagger; no on-call paging |
| **stagger-prod-plus-one-frontend** | `stagger-prod-plus-one-frontend.groovy` | [`job_flows/stagger_prod_plus_one_frontend.md`](job_flows/stagger_prod_plus_one_frontend.md) | Frontend deploy; DIFFERENT shared library (`staggered_plugins_fe@stagger-fe-temp`) |
| **stagger-onboarding** | `config/OnBoardingJenkinFile` | [`job_flows/stagger_onboarding.md`](job_flows/stagger_onboarding.md) | Pure-bash onboarding; config.json append + InfraComposer scaffolding |
| **stagger-scaling** | `scaling.groovy` | [`job_flows/stagger_scaling.md`](job_flows/stagger_scaling.md) | Scale-out (additive): adds N new EC2s to existing BLUE TG; no replace, no canary, no cutover. Library on `feature/scale-job` branch. |

Shared library:
- 5 main pipelines: `staggered_plugins@master` or `staggered_plugins@release/REQ-463-staggerprodplusupdate-v2`.
- **Frontend uses a DIFFERENT library:** `staggered_plugins_fe@stagger-fe-temp`. All same-named helpers (`buildJob`, `deploy`, `createGreenInfra`, `rollout`, `destroyBlueInfra`) resolve to **different** implementations there. Do not assume frontend behavior matches the main library.
- Onboarding is pure bash — no shared lib.

---

## 2. Cross-pipeline reference table

| Pipeline | Build | Prod+1 | Infra | Deploy | Rollout | Cleanup | Failure path |
|---|---|---|---|---|---|---|---|
| **hotfix-noncanary** | `buildJob` (conditional) | — | `instance_provisioning` | `artifact_deployment` + `health_validation` | — | `cutover_cleanup` | `hotfix_rollback` |
| **create-quick-infra** | `QuickBuildJob` / `buildJobFrontend` | — | `CreateQuickInfra` | `QuickDeploy` / `QuickDeployFrontend` | — | — | `rollbackInstances` (with `input` prompt) |
| **main_stagger_prod_plus_one** | `buildJob` | `prodPlusOne` → `createRuleForProdPlusOne` → `deployProdPlusOne` → `triggerAutomation` → `destroyPP1Infra` | `createGreenInfra` | `deploy` | `rollout` → `canary` | `destroyBlueInfra` | `rollbackMain("Single Job Rollback")` + VictorOps + Slack RCA |
| **stagger-nonweb** | `buildJob` | — | `createGreenInfra` | `deploy` → `nonWebDeploy` | `rollout` → `nonwebRollout` | `destroyBlueInfra` | `rollbackMain("non_web_rollback")` |
| **stagger-prod-plus-one-frontend** | `buildJob` (FE lib) | `prodPlusOneFrontend` | `createGreenInfra` (FE lib) | `deploy(..., COMMIT_ID)` (FE lib) | `rollout` (FE lib) | `destroyBlueInfra` (FE lib) | `frontendRollback(SERVICE, "prod", COMMIT_ID)` |
| **stagger-onboarding** | bash: jq validation → config.json append → InfraComposer scaffolding → git push → python enrichment | — | — | — | — | — | `set -e` exit (no Jenkins `post`; manual rollback) |
| **stagger-scaling** | `buildJob` (conditional) | — | `instance_provisioning` (additive — joins existing BLUE_TG) | `artifact_deployment` + `health_validation` | — | — (no cutover, old EC2s stay) | `hotfix_rollback` |

---

## 3. Stage → likely failure modes (universal index)

The most useful section for an RCA agent. When the `[Pipeline] { (StageName)`
marker tells you which stage failed, this table gives the most-likely
error classes and where to drill.

If the stage marker is pipeline-specific (e.g. `(Prod+1)` only exists on
`main_stagger_prod_plus_one` and `stagger-prod-plus-one-frontend`), see
the per-pipeline doc for the variant.

| Stage marker | Most-likely error classes | Drill |
|---|---|---|
| `(Load Library)` | scm, dependency | library branch / ref not resolvable; check `@Library` line; for frontend pipeline, the lib is `staggered_plugins_fe@stagger-fe-temp` |
| `(Jira Details)` | **compliance** | `docops/runbooks/compliance.md`. Modes 1-5 for most jobs. **Mode 6** for `create-quick-infra` ONLY (gate-bug case). |
| `(Resolve Parameters)` | config_validation, parse_error | `create-quick-infra` only; jq parse fail or missing mandatory params |
| `(Input Validation)` | (pipeline-level) | JFROG_BUILD vs COMMIT_ID xor violated |
| `(Build)` / `(Build Artifact)` / `(Build Frontend)` | scm, dependency, java_runtime | git fetch / maven dep / JAR build error / frontend npm fail |
| `(Pre-Deployment)` → sub-stage `1.3 Validate Config Resources` | **config_validation** (NOT health_check) | `hotfix-noncanary` and `stagger-scaling`. AWS describe-* for AMI / subnet / SG / key-pair / IAM-profile returns NotFound. Drill `vars/pre_deployment.groovy`. Fix = update `config.json` to a real resource ID, OR create the missing AWS resource. **Do NOT recommend "recreate target group" — that path is unrelated.** |
| `(Pre-Deployment)` → sub-stage `1.4 Discover BLUE Target Group` | **java_runtime** (CPS serialization) OR `aws_describe` | `stagger-scaling` only. Reads `rule_arn` from config.json and runs `aws elbv2 describe-rules` to resolve BLUE_TG_ARN. Build 15 case: slave bounce mid-`sh` → Jenkins tried to checkpoint pipeline state → `JsonSlurperClassic` retained in `pre_deployment` scope was NOT Serializable → `Caused: java.io.NotSerializableException`. **This is a pipeline-code bug, NOT an AWS / profile / region issue.** Don't be misled by slave-bouncing chatter. Fix is to refactor `vars/pre_deployment.groovy` to discard the `JsonSlurperClassic` instance after extracting primitive map values. If the failure IS actually `aws elbv2 describe-rules` NotFound on the rule_arn, that's a stale value in config.json. |
| `(Instance Provisioning)` | aws_limit, config_validation | `hotfix-noncanary` only. `RunInstances` quota; AMI / subnet / SG NotFound |
| `(Artifact Deployment)` | scm, ssm, network | `hotfix-noncanary` only. JFrog 401/404; SSM SendCommand fail; S3 upload denial |
| `(Health Validation)` | health_check | `hotfix-noncanary` only. TG never healthy; service crash; healthz 4xx/5xx |
| `(Cutover & Cleanup)` | aws_limit, ssm | `hotfix-noncanary` only. Deregister race; terminate denials |
| `(Infra)` / `(Infra Prod+1)` | terraform, stale_tf_state, aws_limit | terraform apply errors; "already exists" conflicts; ALB rule / target-group limit |
| `(Deploy)` / `(Deploy Prod+1)` | ssm, java_runtime, health_check | SSM exec fail; app launch crash; healthy.sh poll fail |
| `(Rollout)` | **canary_fail** (web only) / **canary_script_error** (web) / **timeout** (non-web) | Web: NewRelic canary score < 80 OR canary.py crashed. Non-web: nonwebRollout time-based stagger timeout — there is no canary score, so don't classify as `canary_fail`. |
| `(Destroy)` / `(Destroy Prod+1)` | terraform, aws_limit | destroy fail; orphaned target groups |
| `(Automation)` | timeout, dependency | `main_stagger_prod_plus_one` only. QA-automation trigger fail; approval timeout |

If the stage marker doesn't match this table, the failure is likely
**pre-stage** (Load Library / parameter parsing) or a custom inline
script — read the pipeline file directly via `repo_read_file`.

### Universal rule — when the failure touches infra code, check recent commits FIRST

The `jenkins_pipeline` shared library and the `InfraComposer` terraform
modules are both iterated on continuously. Many wrong-RCA cases trace
back to a recent code change that the runbook content didn't yet
anticipate — the runbook tells you to do X, but X stopped being the
right answer two days ago.

Before recommending an action for ANY failure that involves either
repo, do this once:

1. `repo_recent_commits("jenkins_pipeline", 5)` — the last 5 commits.
2. If the failure involves a terraform / Infra / Destroy stage, ALSO
   `repo_recent_commits("InfraComposer", 5)`.
3. For each commit that touches a file you would otherwise cite in
   your evidence (e.g. `vars/JiraDetails.groovy`, `vars/canary.groovy`,
   `vars/createGreenInfra.groovy`, `config/<svc>/<env>/main.tf`),
   open the diff via `github_get_commit(<repo>, <sha>)` and read it.
4. If the diff modified the code path you are about to recommend a
   fix for, treat that as the prime suspect. Recent code change >
   stale runbook recipe.
5. Widen to 10 or 20 commits only if the last 5 show nothing relevant.

This is the rule that prevents "follow the runbook into the wrong
fix" when the codebase moved underneath the runbook. It applies to
all pipelines and is the FIRST step before drilling helpers when the
failure mode looks unusual or contradicts what the runbook expects.
Cost is one tool call — cheap insurance against a wrong-fix RCA.

---

## 4. Helper summary table

| Helper | Signature | Purpose (1-line) | Called by |
|---|---|---|---|
| `JiraDetails` | `(String service, String commitId, String jiraTicket, String expectedStatus = "READY FOR RELEASE")` | Compliance gate — team-board-mapping, Jira status, signed-off SHA, clone detection. Falls back to `JiraDetails_oldflow(jiraTicket)` if service not onboarded. | all 6 pipelines |
| `buildJob` | `(String service, String COMMIT_ID)` | Reads config.json, runs `precheck['Build']`, builds JAR from git | main, nonweb, frontend, hotfix |
| `QuickBuildJob` | `(String service, String COMMIT_ID, Map params = [:])` | Conditional precheck (only if `IS_ONBOARDED=Yes`), sets JAVA_HOME by `java_version + ACCOUNT` | create-quick-infra (Build) |
| `buildJobFrontend` | `(String service, String COMMIT_ID, Map params)` | Frontend artifact build (Docker / nodejs) | create-quick-infra (Build Frontend) |
| `CreateQuickInfra` | `(String service, Map params)` | Runs N EC2s; returns instance-id list | create-quick-infra (Infra) |
| `QuickDeploy` | `(def SERVICE, def ENVIRONMENT, params)` | Reads `params.INSTANCE_IDS`, resolves JAR, parallel SSM deploy | create-quick-infra (Deploy) |
| `QuickDeployFrontend` | similar to QuickDeploy | Frontend variant | create-quick-infra (Deploy Frontend) |
| `prodPlusOne` | `(def service)` | Orchestrates 4 sub-stages: Infra Prod+1, Deploy Prod+1, Automation, Destroy Prod+1 | main_stagger_prod_plus_one (Prod+1) |
| `prodPlusOneFrontend` | `(SERVICE, COMMIT_ID)` | Frontend Prod+1 (from `staggered_plugins_fe`) | frontend (Prod+1) |
| `createGreenInfra` | `(String SERVICE)` | `precheck['Infra']`, validates 5 config keys, terraform-creates green TG + N EC2s | main, nonweb, frontend (Infra) |
| `createRuleForProdPlusOne` | `(String SERVICE, Number priority)` | Builds prod+1 ALB rule with priority (150 from prodPlusOne); INSTANCE_COUNT=1 hardcoded | prodPlusOne (Infra Prod+1) |
| `deployProdPlusOne` | `(service, "preprod")` | Deploy to prod+1 instance; on catch → `approvalProd1Failure()` | prodPlusOne (Deploy Prod+1) |
| `destroyPP1Infra` | `(SERVICE)` | terraform destroy targeted to prod+1 modules | prodPlusOne (Destroy Prod+1) |
| `deploy` | main lib: `(def SERVICE, def ENVIRONMENT)` ; FE lib: `(SERVICE, ENVIRONMENT, COMMIT_ID)` | Reads service_type; routes to `nonWebDeploy` or web SSM deploy | main, nonweb, frontend (Deploy) |
| `nonWebDeploy` | `(SERVICE, ENVIRONMENT, serviceType)` | Same script bundle as web + `non_web_healthy.sh` | deploy (nonweb path) |
| `rollout` | `(def service)` | `precheck['Rollout']`; routes to `nonwebRollout` for nonweb, else web canary loop | main, nonweb, frontend (Rollout) |
| `nonwebRollout` | `(def service)` | Time-based stagger (canary_timing); no NewRelic canary | rollout (nonweb path) |
| `canary` | `(app_name, blue_tg_arn, region, account)` | Writes `canary.py` libraryResource, runs `python3 ./canary.py --percent_rollout 10` | rollout (web path) — per traffic_value |
| `destroyBlueInfra` | `(SERVICE)` | terraform destroy blue TG + old EC2s | main, nonweb, frontend (Destroy) |
| `precheck` | `runPrechecks(String stageName, Map args)` + `executePrechecks(SERVICE, List stages)` | Stage → precheck-method dispatch (checkGitRepo, checkTerraformStateFile, validateAmiExists, getInstanceCountInGreenTargetGroup, checkTargetGroupsAndTags, checkroutingeligibility, checkDestroyPrerequisites, checkRollbackPrerequisites) | every main stage |
| `pre_deployment` | `(SERVICE, JFROG_BUILD, COMMIT_ID, backup)` | Sub-stages `1.1 Validate Parameters` / `1.2 Load Service Configuration` / `1.3 Validate Config Resources`. The 1.3 sub-stage runs AWS describe-* for AMI / subnet / SG / key-pair / IAM-profile — **NotFound here is config_validation, NOT health_check.** | hotfix-noncanary (Pre-Deployment) |
| `instance_provisioning` | `(String SERVICE)` | Validates env, fallback AMI, `aws ec2 run-instances`, registers in BLUE_TG | hotfix-noncanary (Instance Provisioning) |
| `artifact_deployment` | `(String SERVICE, String JFROG_BUILD)` | `jf rt dl` JAR → unzip → S3 upload → SSM deploy on NEW_INSTANCE_IDS | hotfix-noncanary (Artifact Deployment) |
| `health_validation` | `(String SERVICE)` | `aws elbv2 describe-target-health` waitUntil + 10-min timeout on BLUE_TG_ARN against NEW_INSTANCE_IDS | hotfix-noncanary (Health Validation) |
| `cutover_cleanup` | `(SERVICE, Boolean backup)` | Deregister OLD from BLUE, optional AMI backup, terminate OLD | hotfix-noncanary (Cutover & Cleanup) |
| `hotfix_rollback` | `()` | Deregister NEW_INSTANCE_IDS from BLUE_TG, drain wait, terminate NEW (skips if NEW_INSTANCE_IDS never set) | hotfix-noncanary (post.failure, post.aborted) |
| `rollbackMain` | `(String mode, String service)` | Main + nonweb rollback (`mode`: `"Single Job Rollback"` or `"non_web_rollback"`) | main_stagger_prod_plus_one + stagger-nonweb (post.failure / aborted) |
| `frontendRollback` | `(SERVICE, env, COMMIT_ID)` | Frontend-specific rollback (from `staggered_plugins_fe`) | frontend (post.failure / aborted) |
| `UpdateJiraStatus` | `(jiraTicketId)` | Moves ticket to "Closed" (or equivalent) post-success | every pipeline (post.success) |
| `Notification.rcaAlert` | `(rcaObject, channel)` | Posts RCA summary to per-service Slack channel | main_stagger_prod_plus_one (post.failure) |
| `Notification.build` | `(...)` | Posts build-stage notification | buildJob, QuickBuildJob |
| `Notification.qaSanityApproval` | `(...)` | Posts QA approval ping into per-service Slack channel | prodPlusOne (Automation) |
| `triggerRcaWebhook` | `()` + nested `.buildAlertMessage(rca)` | Calls BB-AI RCA service; returns parsed RCA object; helper to format the Slack alert line | every pipeline (post.failure, NOT_BUILT, unstable) |
| `managerApproval` | `(String submitterEmails)` | Interactive `input` Proceed/Abort, gated by submitter list | likely inside `approval()` / `approvalProd1Failure()` |

---

## 5. How to use this doc from the agent

When the classifier fires and the agent enters its iter-0 batch:

1. Identify the pipeline by `build_meta.job` and the script_path from
   `get_jenkins_job_config(job)`. Use §1 to map to the per-pipeline doc.
2. Identify the failed stage from the `[Pipeline] { (StageName)` markers
   in `log_window`. The LAST stage marker before the fatal error is
   the failing stage.
3. Cross-reference stage → error class in §3.
4. `read_job_flow(<name>)` for the full per-pipeline detail (helper
   chain, sub-stages, post-block, gotchas).
5. `repo_read_file("jenkins_pipeline", "vars/<helper>.groovy", ...)`
   for the actual helper code at the failure line.
6. Cross-check against `docops/runbooks/<error_class>.md` for the
   action template. **When the per-pipeline doc and the class runbook
   diverge, prefer the per-pipeline guidance** — it has stage-specific
   context the class runbook lacks (e.g. `1.3 Validate Config Resources`
   is config_validation, NOT health_check, even though the log message
   contains the word "health").

---

## 6. Maintenance

- **New pipeline** → add a row in §1 + §2 + write a new
  `job_flows/<name>.md` (use one of the existing as template).
- **Helper signature change** → update §4 AND the per-pipeline doc
  using it.
- **Wrong-RCA observed** → add the case to the per-pipeline doc's
  "Stage → likely failure modes" section, and if it's a universal
  pattern, update §3 here too.
- Source of truth is `jenkins_pipeline/` git master, NOT this doc —
  re-verify by reading the file when in doubt.
