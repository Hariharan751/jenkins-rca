# Runbook: canary_script_error

## What this class means
The canary script (`canary.py`) crashed BEFORE Kayenta could judge
anything. The deployed service might be perfectly fine — the failure is
in the canary infrastructure, not in the service.

## Detect signals
- Python traceback at `canary.py:<line>` with `TypeError`, `KeyError`,
  `AttributeError`, `NoneType has no attribute`
- `Traceback (most recent call last)` followed by lines from canary.py
- NewRelic XML response `<error>Application <X> does not exist.</error>`
- `canary_run_status` never set (vs `Fail`)
- Failed stage = "Rollout"

## Pipeline source to cross-check (MANDATORY)
- `jenkins_pipeline/vars/rollout.groovy` — caller
- `jenkins_pipeline/resources/canary.py` — at the line in the traceback

## Drill plan
1. `get_jenkins_job_config(job)` → scriptPath
2. Parse traceback for deepest in-our-code frame: `canary.py:<line>`
3. `repo_read_file("jenkins_pipeline", "resources/canary.py", <line>-10, <line>+10)` — see the crashing code
4. `repo_read_file("jenkins_pipeline", "resources/config.json", ...)` — find service's `new_relic_name`
5. (optional) `aws_describe_listener_rule(<rule_arn>)` to confirm canary TG present

## Common causes (in order)

**1. NewRelic has no data for the appName**
- canary.py queries `SELECT ... FROM Transaction WHERE appName = '<NR_APPNAME>' SINCE 7 days ago`
- Returns zero rows → division by zero / None access → traceback
- Common when service is newly renamed, brand new, or just hasn't seen traffic

**2. appName mismatch**
- `config.json.new_relic_name` doesn't match what service actually reports
- E.g. config says "FMS - Fuel" but service reports as "fms-fuel"

**3. canary.py defensive-code gap**
- Script doesn't handle None gracefully from NewRelic
- Specific line + None operation visible in traceback

## Action template
```
Finding: <ExceptionClass> at canary.py:<line> ("<line content>").
         App name involved: <new_relic_name from config.json>.
         Service performance is NOT the cause; canary infrastructure
         script crashed.
Action:
  Path 1 (operator self-serve): Verify NewRelic has data for app
    '<new_relic_name>'. Run NRQL:
        SELECT count(*) FROM Transaction WHERE appName = '<NR_APPNAME>'
        SINCE 7 days ago
    If zero or null, service isn't reporting → fix service's NewRelic
    agent config, then retry pipeline.

  Path 2 (config fix): Compare config.json's new_relic_name with what
    the service actually reports. If mismatch, update config.json and
    re-deploy.

  Path 3 (long-term, requires PR): canary.py:<line> needs None handling.
    Wrap <operation> in defensive check. File ticket to platform/devops
    team — do NOT block this deploy on it. Use NON_CANARY=true with
    manager approval to ship the current build.
Verify:
  Re-run pipeline. Confirm canary.py doesn't crash + canary_run_status
  prints either Pass or Fail (not absent).
```

## Output schema notes
- `error_class: "canary_script_error"`
- `failed_stage: "Rollout"`
- `evidence[]` must include:
  - `jenkins_log` showing Python traceback
  - `jenkins_pipeline/resources/canary.py:<line>` (crash site)
  - `jenkins_pipeline/resources/config.json:<line>` showing `new_relic_name`

## STRICT rules
- DO NOT say "regression" — there is no canary judgement to interpret.
- DO NOT default-assume the service is broken; canary.py crashed.
