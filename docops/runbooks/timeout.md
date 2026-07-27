# Runbook: timeout

## What this class means

A pipeline operation exceeded its wall-clock budget — typically a
`waitUntil` loop, a Jenkins `timeout(...)` block, or a downstream
provider (AWS, NewRelic, Jira, GitHub) that never responded inside
the configured deadline. The pipeline aborts with messages like:

```
Timeout has been exceeded
timed out after N minutes
deadline exceeded
```

Distinct from `health_check` (ALB-specific probe timeout — has its
own class) and from `network` (TCP/connect-level failure — also a
separate class). `timeout` is the catch-all for "the wrapper around
the operation hit its limit before the operation finished."

## Detect signals (primary)

- `Timeout has been exceeded`
- `timed out after \d+ (minutes|seconds|ms)`
- `deadline exceeded` (gRPC / AWS SDK)
- Jenkins `timeout(time: N, unit: '...') { ... }` wrapper firing
- `waitUntil` body returning false until the iteration cap

## Drill plan

1. **Identify WHICH operation timed out.** Look one screen above the
   `Timeout has been exceeded` line. The operation name is usually
   on the same logical block — common offenders:
   - `aws elbv2 describe-target-health` poll loops
   - `gradle/mvn` build phases
   - `terraform apply` waiting for AWS resource state
   - `curl` to a slow downstream service
   - Jira / GitHub / Slack HTTP calls
2. **Identify the timeout VALUE.** Search the helper code:
   `repo_search("jenkins_pipeline", "timeout(time:")`. Knowing the
   wrapper's configured limit (5min / 10min / 30min) tells you
   whether the budget is wrong vs the operation legitimately needs
   investigation.
3. **Identify upstream context.** Was this a one-off (downstream
   transient) or recurring (capacity / config issue)?
   `repo_recent_commits("jenkins_pipeline", 5)` — did anyone change
   the timeout value or the operation inside the wrapper recently?
4. **Check downstream health.** If timeout was on an AWS call,
   `aws_describe(<service>, <op>, {...})` with a fresh call — does
   it return now? If yes, transient. If no, real downstream issue
   (route to that owner).

## Action template

```
Finding:
  Build <N> of <job> hit a <T>-minute timeout on operation
  `<operation>` in stage `<stage>` (file `<helper>.groovy`:<line>).
  The wrapper aborted before the operation completed.

Action:
  Step 1 (CONFIRM transient vs persistent):
    Re-trigger the build with same params. If it succeeds, this was
    transient (downstream blip) — no code change needed.
  Step 2 (if recurring — raise the budget):
    If the operation is legitimately slower than the timeout (e.g.
    large Terraform plan, fresh Gradle daemon warmup, ALB target
    settling), edit `<helper>.groovy:<line>` to increase the
    `timeout(time: <N>)` to a value 2× the observed P95 duration.
    Commit + push.
  Step 3 (if downstream is hung):
    Check the downstream service's status. For AWS API timeouts in
    a known region, check the AWS status page + retry. For Jira /
    GitHub / Slack, hit their respective status page.

Verify:
  Re-run the pipeline. The previously-timing-out operation should
  complete inside the (possibly raised) budget.
```

## Output schema notes

- `error_class: "timeout"`
- `failed_stage`: the `[Pipeline] { (...) }` marker active when the
  timeout fired
- `evidence[]` must include:
  - `jenkins_log` line containing the timeout phrase
  - `jenkins_pipeline/<helper>.groovy:<line>` for the
    `timeout(time:...)` wrapper or the polling loop that exhausted
    its iterations
  - When AWS is the downstream: an `aws:<resource>` evidence with
    fresh state, to show whether the resource is still in the
    transition state that caused the timeout

## Common pitfalls

- **DO NOT recommend "just increase the timeout" without diagnosing
  why.** If the operation is genuinely degraded (e.g. AWS region
  outage), raising the budget masks the real issue.
- **DO NOT classify as `network` unless the log explicitly shows
  TCP-level failure** (`Connection refused`, `No route to host`,
  `UnknownHostException`). A timeout on a successful TCP connection
  is `timeout`, not `network`.
- **DO NOT classify as `health_check`** unless the failed stage is
  the ALB target-group health poll. Other waitUntil/timeout loops
  belong here.
- **DO NOT recommend `bbctl shell <id>`** unless evidence shows the
  operation was inside an instance (rare for `timeout` class —
  usually a pipeline-level wrapper).
