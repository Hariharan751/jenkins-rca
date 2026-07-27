# Job flow: stagger-prod-plus-one-frontend

## Identity

- **Script path:** `jenkins_pipeline/stagger-prod-plus-one-frontend.groovy`
- **Likely Jenkins job names:** `Stagger-Prod-Plus-One-Frontend`, `frontend-deploy`, `stagger-fe`
- **Shared library:** **`staggered_plugins_fe@stagger-fe-temp`** ← DIFFERENT from all other pipelines
- **Agent / options:** `agent any`; `tools { maven 'Maven' }`; `ansiColor('xterm')`
- **Environment:** `SUBMITTER_EMAILS = "thejasvi.bhat@..., rahul.aggarwal@..., vivekanand.matta@..."`

## Match

- `script_path` ends with `stagger-prod-plus-one-frontend.groovy`, OR
- `inline_script` contains stage bodies calling `prodPlusOneFrontend(...)`
  AND `frontendRollback(...)` in `post { failure { ... } }`.

## Parameters

| Param | Type | Default |
|---|---|---|
| `COMMIT_ID` | string | `commit_id` |
| `SERVICE` | choice | 7 frontends: `gps-shipper-frontend`, `gps-share-frontend`, `boss-frontend`, `trip-frontend`, `brokerage-bo-fe`, `access-portal`, `bb-transformer` |
| `Jira-Ticket` | string | `''` |

## Stages

| # | Stage marker | Helper / inline |
|---|---|---|
| 1 | `(Load Library)` | buildName + lib; declarative `steps` block writes libraryResource `config.json` to `${WORKSPACE}/${BUILD_ID}.json` and `aws_account.json` to workspace |
| 2 | `(Jira Details)` | `JiraDetails(SERVICE, COMMIT_ID, Jira-Ticket)` |
| 3 | `(Build)` | `buildJob(SERVICE, COMMIT_ID)` — frontend-lib variant |
| 4 | `(Prod+1)` | `prodPlusOneFrontend(SERVICE, COMMIT_ID)` ← DIFFERENT from main's `prodPlusOne` |
| 5 | `(Infra)` | `createGreenInfra(SERVICE)` — frontend-lib variant |
| 6 | `(Deploy)` | `deploy(SERVICE, "prod", COMMIT_ID)` — **3-arg** signature (frontend lib) |
| 7 | `(Rollout)` | `rollout(SERVICE)` — frontend-lib variant |
| 8 | `(Destroy)` | `destroyBlueInfra(SERVICE)` — frontend-lib variant |

## Helper chain

```
prodPlusOneFrontend(SERVICE, COMMIT_ID)
  └─ frontend-specific Prod+1 (from staggered_plugins_fe)
buildJob / createGreenInfra / deploy / rollout / destroyBlueInfra
  └─ frontend-lib variants — resolve to DIFFERENT implementations
     than the main staggered_plugins library
frontendRollback(SERVICE, env, COMMIT_ID)
  └─ frontend-specific rollback (replaces rollbackMain from main pipeline)
```

## Post

| Result | Action |
|---|---|
| `always` | `NOT_BUILT` → `triggerRcaWebhook()`; `deleteDir()` |
| `success` | `UpdateJiraStatus(params['Jira-Ticket'])`; `sh "rm -rf ${WORKSPACE}/${BUILD_ID}.json"` |
| `unstable` | `triggerRcaWebhook()` |
| `failure` | `frontendRollback(params.SERVICE, "prod", params.COMMIT_ID)` — **NO VictorOps, NO Slack RCA post, NO BB-AI failure call.** |
| `aborted` | `frontendRollback(SERVICE, "prod", COMMIT_ID)` |

## Stage → likely failure modes

| Stage marker | Error class | Drill |
|---|---|---|
| `(Load Library)` | scm, dependency | frontend lib branch `stagger-fe-temp` not resolvable |
| `(Jira Details)` | compliance | Modes 1-5 |
| `(Build)` | dependency, java_runtime | frontend build (npm / webpack) failure |
| `(Prod+1)` | terraform, stale_tf_state, aws_limit, ssm | frontend prod+1 infra or deploy failure |
| `(Infra)` | terraform, stale_tf_state, aws_limit | terraform apply errors |
| `(Deploy)` | ssm, dependency | frontend artifact deploy fail (CloudFront / S3 invalidation issues) |
| `(Rollout)` | timeout | frontend rollout — typically time-based, no canary |
| `(Destroy)` | terraform | destroy fail |

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

- All helpers (`buildJob`, `deploy`, `createGreenInfra`, `rollout`, `destroyBlueInfra`) resolve to DIFFERENT implementations from `staggered_plugins_fe@stagger-fe-temp`. **Do not** assume the frontend `deploy` matches the main `deploy` — signature is 3-arg here, 2-arg in main.
- Library is loaded in declarative `steps`, NOT inside a `script {}` block — unusual mix; most other pipelines load inside `script {}`.
- Per-build config snapshot `${WORKSPACE}/${BUILD_ID}.json` is cleaned up only on success — failure leaves it in workspace for debugging.
- `stagger-fe-temp` library branch name implies it's still on a temporary release branch — be careful before recommending changes to the lib.
- The `Stagger Prod Plus One Frontend` job is HTTP-serving but the artifacts are static (CloudFront / S3), not running JVMs — RCA shouldn't suggest checking JVM heap or NewRelic Java agent.
