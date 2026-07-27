# Runbook: jenkins_agent_offline

## What this class means

The Jenkins build agent (a slave node, e.g. `slave-4`) lost
connectivity to the Jenkins master mid-step. The `sh` step that was
running on the agent could not report its result back; Jenkins
waited a configured grace period for the agent to come back online;
in some cases the agent reconnected, in others the pipeline aborted
with a serialization-related secondary failure when the workflow
engine tried to checkpoint state during the bounce.

This is **infrastructure-level**, not pipeline-code-level. The
`Caused: java.io.NotSerializableException ...` line you may see at
the very bottom of the log is a SECONDARY failure (Jenkins workflow
plugin tried to save the in-flight CPS state, found a non-serializable
object retained in the helper's scope, save failed → pipeline
aborted). It is NOT the root cause; the agent disconnect is.

## Detect signals

Primary (mandatory — at least one must appear):
- `slave-\d+ seems to be removed or offline`
- `agent <name> seems to be removed or offline`
- `cannot find current thread in CpsStepContext`
- `Connection was broken: java.io.IOException`
- `will wait for 5 min 0 sec for it to come back online`

Secondary (common companions; do NOT classify on these alone):
- `Caused: java.io.NotSerializableException` (e.g. `groovy.json.JsonSlurperClassic`)
- `slave-X is back online` followed soon after by another `slave-X seems to be removed or offline`
- Repeated alternation of online/offline across multiple stages

`error_class` should be `jenkins_agent_offline`.

## Drill plan

1. **Confirm primary signal.** `repo_search` is NOT useful here —
   the log lines come from Jenkins-core, not the pipeline groovy.
   Just count how many times `seems to be removed or offline`
   appears in the log window. ≥ 2 occurrences = chronic agent
   instability for that build; 1 = isolated blip.
2. **Identify the agent.** Pull the slave name from the log
   (`slave-N` / `<agent-name>`). Note in `evidence[]`.
3. **Look at the build's stage timing.** Use `get_jenkins_job_config`
   if it exposes timing — the slave bounce typically happens during
   an `sh` step that runs > 1 minute. Identify which `sh` was in
   flight when the disconnect occurred.
4. **Identify what code was running on the slave.** Cite the
   `sh` step's source (pipeline file + line range). For build 15
   of Stagger Scaling this was inside
   `vars/pre_deployment.groovy` stage 1.4 running
   `aws elbv2 describe-rules ...`.
5. **Look for the secondary symptom.** If the log ends with
   `Caused: java.io.NotSerializableException: <ClassName>`, identify
   which helper retained that class. This is the pipeline-code
   hardening target — but it is NOT the primary fix.

## Action template

```
Finding:
  PRIMARY (root cause):
    Build <N> of <job> ran on Jenkins agent `<slave-name>`. During
    stage `<stage>` (sh: `<command>`), the agent disconnected from
    the Jenkins master <N> times (5-minute wait each cycle). Jenkins
    could not recover the step's context.

  SECONDARY (symptom that surfaced the disconnect to the user):
    On the final disconnect, Jenkins attempted to checkpoint pipeline
    state for retry, and the in-flight workflow held a reference to
    `<NotSerializableClass>` (e.g. groovy.json.JsonSlurperClassic) via
    `vars/<helper>.groovy:<line-range>`. The save failed with
    `java.io.NotSerializableException`, aborting the pipeline. Without
    the disconnect, the pipeline would not have tried to serialize
    that object.

Action:
  Step 1 (PRIMARY — agent health):
    Investigate `<slave-name>` health. SSH to the slave (if BBCTL
    is configured) OR ask devops to check:
      - bbctl shell <slave-instance-id>
      - sudo systemctl status jenkins-agent || cat /var/log/jenkins/*.log | tail -200
      - dmesg -T | tail -100
      - df -h, free -m, uptime, ss -tlnp | grep <jenkins-port>
    If the slave has high disk / OOM / network drops, fix that.
    If the slave is healthy but the connection from the master is
    flaky, restart the agent process AND/OR re-create the agent
    instance.

  Step 2 (PRIMARY — re-run the build):
    Once `<slave-name>` is stable (or move the job to a different
    agent label), re-trigger the build. Most slave-bounce failures
    don't recur on a healthy agent.

  Step 3 (SECONDARY — pipeline-code hardening; OPTIONAL, defense in depth):
    Refactor `vars/<helper>.groovy:<lines>` so the
    `<NotSerializableClass>` instance is local-scope only — extract
    primitive Map / List / String values immediately and discard the
    parser object before any subsequent `sh` step. Pattern:
      def parsed = new JsonSlurperClassic().parseText(text)
      def keys = parsed.collectEntries { k, v -> [k, v.toString()] }
      parsed = null   // explicit, OR put inside a method with no return
    This won't fix the slave instability, but it makes future
    slave-bounces recoverable.

Verify:
  Re-run the build on the same job (or a different slave). Expect
  the stage that previously failed to complete; if the same slave
  bounces again, escalate the agent-infrastructure investigation.
```

## Output schema notes

- `error_class: "jenkins_agent_offline"`
- `failed_stage`: the stage marker that was active when the slave
  bounce happened (NOT the `(Declarative: Post Actions)` stage,
  even if that's the last `[Pipeline] { (...)` line — that's just
  the post-block running rollback).
- `evidence[]` must include:
  - `jenkins_log` line(s) with `seems to be removed or offline`
  - `jenkins_log` line(s) with `Caused: java.io.NotSerializableException`
    IF present (this is the secondary symptom — it's part of the
    evidence, not the primary cause)
  - `jenkins_pipeline/vars/<helper>.groovy:<line>` for the sh step
    that was in flight (so the operator sees which code was active)
  - For repeating slave bounces, cite the slave name explicitly

## Common pitfalls

- **DO NOT classify as `java_runtime`** just because the trace ends
  with a Java exception. The `cannot find current thread in
  CpsStepContext` line is the giveaway — that's Jenkins workflow
  reporting a missing thread context, not an app-code crash.
- **DO NOT report only the secondary symptom.** A pipeline-code fix
  for `NotSerializableException` would NOT prevent the next
  slave-bounce — the agent is the root issue. Always state both.
- **DO NOT recommend pipeline code changes as the ONLY fix.**
  Hardening helpers to drop non-Serializable objects is good practice,
  but if the slave keeps disconnecting, the next bounce will hang
  the pipeline differently. Fix the slave.
- **DO NOT classify a single-occurrence "agent offline" line as
  this class** if the build still succeeded (some pipelines tolerate
  brief reconnects). Only classify here if the disconnect aborted
  the pipeline.
- **DO NOT recommend `aws ec2 describe-*` to diagnose this** — the
  failing AWS CLI line in the log is what was *running* when the
  slave dropped, not what *caused* the slave to drop. AWS API state
  is irrelevant to slave health.
