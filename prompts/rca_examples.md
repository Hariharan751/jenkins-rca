# RCA few-shot examples

## Example 1 — parse_error (jq / config.json)

**Input:**
- service: prod-gps, build: 5096, stage_failed: Infra
- error_class: parse_error
- log_window: `parse error: Invalid numeric literal at line 74, column 401`

**Expected RCA JSON:**
```json
{
  "summary": "Infra stage failed due to jq parse error reading config.json; malformed numeric field at line 74.",
  "failed_stage": "Infra",
  "error_class": "parse_error",
  "root_cause": "createGreenInfra.groovy:336 runs `echo '${configFile}' | jq -r ...`. config.json line 74 has a field whose value is a string ('\"50\"') where jq expects integer, or a value with special shell chars. jq emits 'Invalid numeric literal' and exits 4.",
  "evidence": [
    {"source": "jenkins_log:line_8791", "snippet": "parse error: Invalid numeric literal at line 74, column 401"},
    {"source": "jenkins_pipeline/vars/createGreenInfra.groovy:336", "snippet": "echo '${configFile}' | ${JQ} -r '.\"service\" | .key'"},
    {"source": "jenkins_pipeline/resources/config.json:74", "snippet": "\"disk_size\": \"50\"  ← string not integer"}
  ],
  "suggested_fix": "Fix config.json: change string '\"50\"' → integer 50 for disk_size (and similar string numerics). Commit + re-run.",
  "suggested_commands": [
    {"cmd": "cat jenkins_pipeline/resources/config.json | jq '.\"<service>\"'", "tier": "safe", "rationale": "validate service entry parses clean"}
  ],
  "confidence": 0.92,
  "needs_deeper": false,
  "tokens_used": {"input": 0, "output": 0, "cache_read": 0}
}
```

## Example 2 — canary_fail

**Input:**
- service: demand, stage_failed: Rollout
- error_class: canary_fail
- log_window: `Rollout back as Canary failed` / `Rolling Back as Result !=0`

**Expected RCA JSON:**
```json
{
  "summary": "Rollout stage rolled back because canary analysis (canary.py) returned non-zero after traffic shift.",
  "failed_stage": "Rollout",
  "error_class": "canary_fail",
  "root_cause": "rollout.groovy runs canary.py after each traffic_values step. canary.py checks New Relic error rate / response time for green vs blue. Result != 0 means green worse than blue threshold. Traffic reverted to blue 100%.",
  "evidence": [
    {"source": "jenkins_log", "snippet": "Rolling Back as Result !=0"},
    {"source": "jenkins_pipeline/vars/rollout.groovy:134", "snippet": "if (result != 0) { error('Rollout back as Canary failed') }"}
  ],
  "suggested_fix": "Check app logs on green instances. Run `bbctl rca --deep` for Opus analysis. Likely: new deployment introduced regression in error rate or latency.",
  "suggested_commands": [
    {"cmd": "tail -n 200 /var/log/blackbuck/<service>.log", "tier": "safe", "rationale": "check app errors on green instance"}
  ],
  "confidence": 0.80,
  "needs_deeper": true,
  "tokens_used": {"input": 0, "output": 0, "cache_read": 0}
}
```

# TODO: add examples 3 (scm/PAT expired), 4 (health_check), 5 (ALB rule conflict)
