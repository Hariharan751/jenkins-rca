# Runbook: health_check

## What this class means
ALB target group probe never returned healthy during deploy. Pipeline aborts
in `Deploy` or `Deploy Prod+1` stage after N failed poll iterations. Service
crashed at startup, bound to wrong port, or health-check path returns non-2xx.
RCA confirms WHERE (TG, instance, config) — does NOT determine WHY without
instance access. Operator uses `bbctl shell <instance_id>` for that.

## Detect signals
- `Health Status failed to move to healthy within the time limit`
- `ALB target unhealthy` / `Iteration <N> of <M>: still draining`
- Failed stage = `Deploy` or `Deploy Prod+1`

## Chain-walk rule (MANDATORY)

Main pipeline file = dispatch only. `vars/<helper>.groovy` = implementation.
Evidence must cite `vars/` files, NOT the main pipeline stage block.

Chain: `main_*.groovy` → `vars/prodPlusOne.groovy` → `vars/deployProdPlusOne.groovy`
→ read `libraryResource 'scripts/X'` line → `resources/scripts/X`

- `libraryResource 'X'` resolves on disk to `resources/X`
- Do NOT assume script name — read `deployProdPlusOne.groovy` to find actual `libraryResource` reference (`healthy.sh` or `non_web_healthy.sh`)
- Script lives under `resources/scripts/` only — not `vars/` or `resources/` root

## Drill plan — ALL in parallel once runbook loaded

1. `get_jenkins_job_config(job)` — confirm scriptPath (may already be done)
2. **MANDATORY** `repo_read_file("jenkins_pipeline", "vars/deployProdPlusOne.groovy", 1, 80)` — implementation helper
3. **MANDATORY** `repo_read_file` the health script found in step 2's `libraryResource 'scripts/X'` line — the poll loop
4. **MANDATORY** `aws_describe(elbv2, DescribeTargetGroups, {TargetGroupArns:[<tg_arn>]}, ...)` → `HealthCheckPath`
5. **MANDATORY** `aws_describe(elbv2, DescribeTargetHealth, {TargetGroupArn:<tg_arn>}, ...)` → `Target.Port` + state
6. **MANDATORY** `aws_describe(ec2, DescribeInstances, {InstanceIds:[<instance_id>]}, ...)` → instance state

Note: steps 3 depends on step 2 result — sequential read is correct.

## Values discipline

| Value in suggested_commands | Required source |
|---|---|
| curl/ss port | `DescribeTargetHealth.Target.Port` — instance registration port, NOT `DescribeTargetGroups.Port` (ALB-side default) |
| health-check path | `DescribeTargetGroups.HealthCheckPath` |
| log path | `service.lookup.filebeat_log_path` |

## Action template

```
Finding: <svc> on <instance_id> in <aws_account>/<region> failed to become
healthy during <failed_stage>. Target.Port=<from DescribeTargetHealth>,
HealthCheckPath=<from DescribeTargetGroups>, state=unhealthy,
reason=<from DescribeTargetHealth>.

Action: bbctl shell <instance_id>
  sudo tail -n 200 <service.lookup.filebeat_log_path>
  sudo ss -tlnp | grep <DescribeTargetHealth.Target.Port>
  curl -i http://localhost:<Target.Port><HealthCheckPath>
  systemctl status <svc>

Verify: re-run pipeline; Deploy stage should pass health-check loop.
```

## Output schema — evidence[] required (ALL must be present)

- `jenkins_log` — health-check timeout line
- `jenkins_pipeline/vars/deployProdPlusOne.groovy:<line>` — deploy helper (NOT main pipeline)
- `jenkins_pipeline/resources/scripts/<X>:<line>` — health poll script (`libraryResource` reference from above)
- `aws:target_health(<tg_arn>)` — state + reason
- `aws:target_group(<tg_arn>)` — HealthCheckPath
- `aws:instance(<instance_id>)` — running state

## STRICT — DO NOT

- `aws_run_ssm_command(...)` — removed tool
- `ssh -i <pem>` — use BBCTL only
- hardcode port 8080 or 80 — use `DescribeTargetHealth.Target.Port`
- hardcode `/admin/version` — use `DescribeTargetGroups.HealthCheckPath`
- hardcode `/var/log/blackbuck/gps.log` — use `service.lookup.filebeat_log_path`
- finalize without `vars/deployProdPlusOne.groovy` in evidence — AWS state alone is insufficient
- finalize without the health poll script in evidence — read `resources/scripts/X` first

## Likely root causes (priority order)

When the drill plan returns data but the cause isn't obvious from
`DescribeTargetHealth.Reason` alone, walk this list:

1. **Service didn't start on the instance.** Crash in main(), missing
   config, port already in use, JVM OOM at startup. Tail the service log
   (`service.lookup.filebeat_log_path`) via
   `bbctl shell <instance_id>` — look for stack trace at the end.

2. **Port mismatch between service and target group.** Service listens
   on port X, TG `health_check_port` (or `port`) set to Y.
   `sudo ss -tlnp | grep -E "<service_port>|<health_check_port>"` plus
   `DescribeTargetGroups [HealthCheckPort, Port]`.

3. **Health endpoint path returns non-2xx.** Service up + listening,
   but `<HealthCheckPath>` returns 4xx/5xx.
   `curl -i http://localhost:<Target.Port><HealthCheckPath>` — expect
   `HTTP/1.1 200`.

4. **Security group blocks ALB → instance on the TG port.** Instance SG
   missing ingress from ALB SG on TG port.
   `aws ec2 describe-security-groups --group-ids <instance-sg>` —
   confirm ingress rule from ALB SG on TG port.

5. **Slow boot vs health-check threshold.** Service eventually healthy
   but takes longer than `healthy_threshold × interval` (typically
   2 × 30s = 60s). Compare service start timestamp in log vs deploy
   timestamp. Fix: raise `HealthCheckIntervalSeconds` or
   `HealthyThresholdCount`.

6. **Dependency unreachable** (DB / Redis / Kafka / downstream API).
   Health endpoint does deep check; the dep is down/blocked. Service log
   shows connect-timeout/refused on the dep. Check dep status separately.

## Non-fatal upstream noise (IGNORE in RCA)

These appear in the same log but are NOT the cause:

- `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!` — pipeline has SSM
  fallback for instance login; SSH host-key mismatch never blocks deploy.
- `<error>Application X does not exist.</error>` from NewRelic — appName
  not registered yet. Observability gap, not a deployment failure.
- `Did you forget the def keyword? ... seems to be setting a field named
  pipelineSuccess` — Jenkins script-warning, not the failure cause.

## Related code

- `vars/nonwebdeploy.groovy` — wraps `healthy.sh` invocation
- `vars/deployProdPlusOne.groovy` — Prod+1 deploy variant
- `resources/scripts/healthy.sh` / `non_web_healthy.sh` — poll loop
  (50 iterations × interval)
- `resources/config.json` — service registry; per-service `log_path`,
  `service_port`, `health_check_path`, `health_check_port`
