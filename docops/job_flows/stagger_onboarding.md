# Job flow: stagger-onboarding (OnBoardingJenkinFile)

## Identity

- **Script path:** `jenkins_pipeline/config/OnBoardingJenkinFile`
- **Likely Jenkins job names:** `Stagger-Onboarding`, `service-onboarding`, `onboarding-job`
- **NOT a Jenkinsfile** — this is a pure bash script with `set -e`. Runs as a single shell-step inside a Jenkins job, on the Jenkins controller. There is no `pipeline {}` block, no declarative stages, no `vars/` helpers — just chained bash with hardcoded paths to `/var/lib/jenkins/ramesh/`.

## Match

- Jenkins job name contains "onboarding" or "OnBoarding", AND
- The console log shows raw bash output (no `[Pipeline] { (StageName)` markers), AND
- Log contains `jq 'has($service_name)'` against `config.json`, AND
- The output mentions `/var/lib/jenkins/ramesh/jenkins_pipeline/` and/or
  `/var/lib/jenkins/ramesh/InfraComposer/`.

## Inputs (env vars supplied by Jenkins parameter form)

| Variable | Purpose |
|---|---|
| `service_name` | new service identifier (must not already be in config.json) |
| `traffic_value` | comma-separated ascending list (e.g. `10,50,100`) |
| `git_repo_name` | GitHub repo name (must exist under `BLACKBUCK-LABS`) |
| `aws_account_name`, `aws_region_name` | target AWS account / region |
| `lb_listener_arn`, `rule_arn` | ALB listener + rule ARNs (validated via `aws elbv2 describe-*`) |
| `ami_id` | AMI id |
| `instance_class_name`, `instance_no` | EC2 class + count |
| `disk_sizes` | must be ≥ 10 (GB) |
| `server_commands`, `build_commands` | bash commands |
| `service_type` | `web` / `non-web` / `non-web-cron` / `non-web-consumer` / `Java` / `Docker` |
| `new_relic_app_name`, `new_relic_file_path`, `new_relic_jar_path` | NewRelic config |
| `slack_channel` | non-empty |
| `qa_automation` | `yes` / `no` |
| `qa_automation_name` | when `qa_automation==yes` |
| `team_name`, `business`, `java_version` | metadata |
| `filebeat_log_path` | when `filebeat_required_for_service==true`, must match `^/var/log/[^*]+\.log$` (no `*`) |
| `filebeat_required_for_service` | bool |
| `canary_timing` | only for `non-web-cron` |
| `git` | GitHub PAT |

## "Stages" — sequential bash sections

| # | Action |
|---|---|
| 1 | **Pull config repo** — `cd /var/lib/jenkins/ramesh/jenkins_pipeline/`; `git checkout master`; `git pull` |
| 2 | **Duplicate check** — `jq 'has($service_name)' config.json` → exit 1 if already onboarded |
| 3 | **Traffic value ascending check** — comma-split `traffic_value`, verify ascending |
| 4 | **GitHub repo existence** — `curl api.github.com/repos/BLACKBUCK-LABS/$git_repo_name` with `Authorization: token $git` |
| 5 | **ALB listener_arn validation** — `aws elbv2 describe-listeners` |
| 6 | **ALB rule_arn validation** — `aws elbv2 describe-rules` |
| 7 | **Instance count positive integer** — regex `^[1-9][0-9]*$` |
| 8 | **Disk size ≥ 10 GB** |
| 9 | **service_identifier regex** — forced format `^\*preprod.*\*$` (hardcoded at line 6) |
| 10 | **Slack channel non-empty**, **new_relic_file_path / new_relic_jar_path non-empty** |
| 11 | **filebeat_log_path format** — must match `^/var/log/[^*]+\.log$` when filebeat enabled |
| 12 | **Config-compass registration** — `curl -X POST http://configcompass.alb.jinka.in/config-compass/config/service-header-mapping` (must return HTTP 200) |
| 13 | **Append to config.json** — three jq branches by `qa_automation` (yes/no) and `service_type` (`non-web-cron` adds `canary_timing`); writes ~22 fields |
| 14 | **InfraComposer terraform scaffolding** — `cd /var/lib/jenkins/ramesh/InfraComposer`; copies template dir to `/tmp/$service_name`; `sed` replaces `{{service_name}}`, `{{aws_region_name}}`, `{{aws_account_name}}` in `prod/`, `prodplusone/`, `prod-scale/` (`main.tf` + `variable.tf` each); commits to InfraComposer `main` |
| 15 | **Push config.json change** — `git add . && git commit && git push origin master` |
| 16 | **Enrich config.json** — `python3 ./resources/update_config.py "$service_name" "$aws_account_name" "$aws_region_name"` (auto-discovers infra metadata from a healthy instance) |
| 17 | **Push enrichment** — second `git push` with the auto-enriched fields |

## Post

N/A — no Jenkins `post {}` block. `set -e` exits on any failure; rollback is manual (revert the two git pushes).

## Stage → likely failure modes

| Failure point | Class hint | Drill |
|---|---|---|
| Step 2 (duplicate check exit 1) | (pipeline-level) | Service already onboarded — operator gave a wrong / existing `service_name` |
| Step 3 (ascending check) | (pipeline-level) | `traffic_value` must be ascending; e.g. `100,50,10` is invalid |
| Step 4 (GitHub 404) | scm | git repo doesn't exist under `BLACKBUCK-LABS` org or PAT lacks read access |
| Step 5 / 6 (ALB ARN validation) | aws_describe / config_validation | listener_arn / rule_arn invalid in target account/region |
| Step 9 (service_identifier regex) | (pipeline-level) | the `*preprod-${service_name}*` format check is hardcoded; can't override |
| Step 11 (filebeat_log_path format) | (pipeline-level) | path must not contain `*`; must match `^/var/log/.+\.log$` |
| Step 12 (ConfigCompass HTTP != 200) | network, dependency | ConfigCompass service down or auth missing |
| Step 13 (jq append fails) | parse_error | invalid characters in user input breaking JSON |
| Step 14 (terraform template sed) | scm, dependency | template files missing from InfraComposer; cannot copy or sed |
| Step 15 / 17 (git push) | scm | branch protection blocks; merge conflict; PAT lacks write |
| Step 16 (python enrichment) | dependency, parse_error | `update_config.py` import errors; reference instance not healthy |

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

- Two separate repos checked out into Jenkins controller filesystem at `/var/lib/jenkins/ramesh/` — fragile, won't work on other controllers.
- ConfigCompass HTTP-200 check is hard — service header mapping must register BEFORE config.json gets the new entry.
- New-relic validation block is commented out (lines 126-135 of the script). Don't rely on those checks; bad NewRelic config will slip through.
- Final python enrichment step appends additional fields (subnet, security groups, key_name, instance_profile) by querying AWS for a healthy reference instance — needs at least one healthy instance of the same `service_type` already running.
- `service_identifier` is forced to `*preprod-${service_name}*` (line 6). If the service uses a non-preprod naming convention, this script won't accommodate it.
- Operator must rollback BOTH git pushes (`jenkins_pipeline` master + `InfraComposer` main) if any later step fails after the first push.
