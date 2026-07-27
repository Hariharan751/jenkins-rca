# Runbook: canary_fail

## What this class means
Kayenta judged the canary (green) instance worse than baseline (blue)
during the Rollout stage's traffic-shift loop. Pipeline rolled back to
blue 100%. The deployed service likely has a regression.

## Detect signals
- `Rolling Back as Result !=0`
- `Rollout back as Canary failed`
- `canary_run_status: Fail`
- `KAYENTA_FAIL`
- Failed stage = "Rollout"

## Pipeline source to cross-check (MANDATORY)
- `jenkins_pipeline/vars/rollout.groovy` — traffic-shift loop
- `jenkins_pipeline/resources/canary.py` — NewRelic query + Kayenta call

## Drill plan
1. `get_jenkins_job_config(job)` → scriptPath
2. `repo_read_file("jenkins_pipeline", "vars/rollout.groovy", 100, 160)` — canary loop
3. `repo_read_file("jenkins_pipeline", "resources/canary.py", 1, 100)` — measurement logic
4. Parse log for:
   - Which canary config name failed (e.g. `<SERVICE>-Web-latency`,
     `<SERVICE>-Other-error-rate`)
   - Traffic percent at failure (5% / 20% / 50% / 100%)
   - Which configs PASSED in same run
5. `github_recent_commits("<service_repo>", "main", 10)` — recent service changes
6. `aws_describe_target_group(<canary_tg_arn>)` — confirm canary TG config
7. `aws_describe_listener_rule(<rule_arn>)` — confirm traffic split

## Action template
```
Finding: <SERVICE> canary FAILED at <X>% traffic on config <config_name>
         (measures <latency|error-rate> on <Web|Other> path). Configs
         that PASSED: <list>. Per canary.judge_logic, this means
         <interpretation: Web-latency = 2.5x baseline = moderate;
         Other-latency = 50x baseline = catastrophic;
         error-rate = 1x = zero tolerance>.

         <If failed at 5%>: hot-path code bug — diff recent commits.
         <If failed at 50%>: load-dependent — heap/thread dump needed
                              BEFORE re-deploy; likely DB pool / GC pressure.
         <If newrelic.slow_transactions block present>: top transactions:
                              1. <txn>: p95=<ms>, rate=<r/min>
                              2. ...
Action:
  Path 1 (RECOMMENDED — investigate regression):
    Open NewRelic for app <new_relic_name>, compare canary build vs
    baseline. Scope to slow transactions if cited.
  Path 2 (canary threshold mismatch — rare):
    Inspect Kayenta config <config_name>. If baseline SLO legitimately
    changed, adjust pass/marginal in config.
  Path 3 (emergency bypass — manager approval required):
    Re-deploy with NON_CANARY=true param.
Verify:
  Re-run pipeline. Confirm all canary configs show canary_run_status: Pass.
```

## Output schema notes
- `error_class: "canary_fail"`
- `failed_stage: "Rollout"`
- `evidence[]` must include:
  - `jenkins_log` showing Result !=0 line
  - `jenkins_pipeline/vars/rollout.groovy:<line>` (canary loop)
  - `jenkins_pipeline/resources/canary.py:<line>` (relevant measurement)
  - `aws:target_group(<tg_arn>)` config

## STRICT rules
- NEVER invent canary scores. Log has Pass/Fail only, not numeric score.
- NEVER suggest `NON_CANARY=true` as the primary fix — only as Path 3
  with explicit "manager approval required" callout.
- If `newrelic.slow_transactions` block is empty, name the appName +
  time window for operator to query — do NOT fabricate transaction names.
