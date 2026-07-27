# Job flow: stagger-nonweb

## Identity

- **Script path:** `jenkins_pipeline/stagger-nonweb.groovy`
- **Likely Jenkins job names:** `Stagger-NonWeb`, `stagger-prod-nonweb`, `nonweb-deploy`
- **Shared library:** `staggered_plugins@master`
- **Agent / options:** `agent any`; `tools { maven 'Maven' }`; `ansiColor('xterm')`
- **Environment:** `SUBMITTER_EMAILS = "thejasvi.bhat@..., rahul.aggarwal@..., vivekanand.matta@..."`

## Match

- `script_path` ends with `stagger-nonweb.groovy`, OR
- `inline_script` contains stage bodies calling `buildJob(...)`,
  `createGreenInfra(...)`, `deploy(...)`, `rollout(...)`,
  `destroyBlueInfra(...)` (no `prodPlusOne` stage), with
  `rollbackMain("non_web_rollback", ...)` in `post { failure { ... } }`.

## Parameters

| Param | Type | Default |
|---|---|---|
| `COMMIT_ID` | string | `commit_id` |
| `SERVICE` | choice | hardcoded ~35 non-web services (consumers, crons, kafka) |
| `Jira-Ticket` | string | `''` |

## Stages

| # | Stage marker | Helper |
|---|---|---|
| 1 | `(Load Library)` | buildName + library |
| 2 | `(Jira Details)` | `JiraDetails(SERVICE, COMMIT_ID, Jira-Ticket)` |
| 3 | `(Build)` | `buildJob(SERVICE, COMMIT_ID)` |
| 4 | `(Infra)` | `createGreenInfra(SERVICE)` |
| 5 | `(Deploy)` | `deploy(SERVICE, "prod")` — auto-routes to `nonWebDeploy` |
| 6 | `(Rollout)` | `rollout(SERVICE)` — auto-routes to `nonwebRollout` |
| 7 | `(Destroy)` | `destroyBlueInfra(SERVICE)` |

NOTE — there is **no Prod+1 stage** in this pipeline.

## Helper chain

```
Same buildJob → createGreenInfra → deploy → rollout → destroyBlueInfra
as main_stagger_prod_plus_one, but:
deploy
  └─ nonWebDeploy(SERVICE, "prod", serviceType)
       ├─ same script bundle as web + non_web_healthy.sh
       ├─ optional fluent-bit.conf
       └─ parallel SSM deploy
rollout
  └─ nonwebRollout(service)
       ├─ reads canary_timing from config
       ├─ non-web-cron: clamps each value to [0..60]; default [5]
       ├─ non-web-consumer: default [0]
       └─ time-based delays; no NewRelic canary call
```

## Post

| Result | Action |
|---|---|
| `always` | `NOT_BUILT` → `triggerRcaWebhook()`; `deleteDir()` |
| `success` | `UpdateJiraStatus(params['Jira-Ticket'])` |
| `unstable` | `triggerRcaWebhook()` |
| `failure` | `rollbackMain("non_web_rollback", params.SERVICE)` — **NO VictorOps, NO Slack RCA post, NO BB-AI failure call.** |
| `aborted` | `rollbackMain("non_web_rollback", params.SERVICE)` |

## Stage → likely failure modes

| Stage marker | Error class | Drill |
|---|---|---|
| `(Load Library)` | scm, dependency | library branch / ref not resolvable |
| `(Jira Details)` | compliance | Modes 1-5 in `docops/runbooks/compliance.md` |
| `(Build)` | scm, dependency, java_runtime | git fetch / maven dep / JAR build error |
| `(Infra)` | terraform, stale_tf_state, aws_limit | terraform apply errors; "already exists" |
| `(Deploy)` | ssm, java_runtime | SSM exec fail; app launch crash; `non_web_healthy.sh` fail |
| `(Rollout)` | timeout | `nonwebRollout` time-based stagger timeout. Do NOT classify as `canary_fail` — there is no canary here. |
| `(Destroy)` | terraform, aws_limit | destroy fail |

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

- **NO Prod+1 stage** (non-web services don't take HTTP traffic, so no preprod validation).
- Failure path is intentionally thin — no on-call paging, no Slack RCA post.
- `nonwebRollout` uses time-based stagger, not canary score. Don't write RCAs that assume Kayenta / NewRelic canary semantics here.
- `non_web_healthy.sh` is the health-check signal (NOT `healthy.sh` which is for web services).
- Some pipelines under "non-web" are actually consumers / kafka workers — they don't serve HTTP, so an ALB target group may or may not exist depending on config. Don't assume TG presence for these.
