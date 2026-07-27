# Runbook: ssm

## What this class means

An AWS Systems Manager (SSM) command execution failed — the pipeline
issued `SendCommand` or `StartSession` against a target instance and
the response was an error, or `aws ssm send-command` exited non-zero
inside the pipeline. The pipeline aborts with messages like:

```
SSM command failed
ssm:SendCommand
Failed to execute SSH
```

Distinct from `network` (no TCP connection — SSM uses the agent on
the instance) and from `health_check` (ALB-side probe). `ssm` is
specifically "SSM SendCommand round-trip didn't return Success."

## Detect signals (primary)

- `SSM command failed`
- `ssm:SendCommand` followed by an error code
- `InvocationDoesNotExist` (SSM never started the invocation)
- `Failed to execute SSH` (Jenkins fallback when SSM was the primary
  path)
- `Status: Failed` in an SSM output block
- `Output: ` followed by an exit-code-non-zero from the shell script
  SSM ran on the instance

## Drill plan

1. **Identify the target instance.** SSM commands name the
   instance ID: `i-XXXXXXXXXXXXXXXXX`. Note it verbatim.
2. **Identify the document + command.** SSM commands reference a
   document name (e.g. `AWS-RunShellScript`) and a parameter set.
   Search the pipeline: `repo_search("jenkins_pipeline",
   "ssm send-command")`. The match tells you which helper issued
   the call.
3. **Check the instance's SSM agent status.**
   `aws_describe(ssm, DescribeInstanceInformation, {InstanceIds:
   [<id>]})` — confirm the agent is `Online`. If `Inactive`, the
   agent itself is dead (instance reboot / agent crash / IAM
   permission missing).
4. **Check the IAM role.** `aws_describe(iam,
   GetInstanceProfile, ...)` for the instance — does it include
   `AmazonSSMManagedInstanceCore`? Without it, the agent can't
   register with SSM.
5. **Check the command's actual output.** If `Status: Failed` came
   back with output, the script ran but exited non-zero. Read the
   output for the actual error — this is rarely an SSM problem,
   it's the underlying shell command's error.

## Action template

```
Finding:
  Build <N> of <job> in stage `<stage>` issued SSM `<document>`
  against `<instance_id>` and got `<status>` / `<error_code>`. The
  pipeline aborted because the SSM round-trip didn't complete
  successfully.

Action:
  Step 1 (CONFIRM agent state):
    `aws ssm describe-instance-information --instance-information-
    filter-list "key=InstanceIds,valueSet=<instance_id>"`. If
    PingStatus != Online, the agent is the problem — not the
    command. Restart the agent on the instance OR re-create the
    instance.
  Step 2 (if agent OK but command failed):
    Read the actual command output (the SSM `Output: ` block).
    Treat the underlying exit code as the real error class — this
    is NOT an SSM bug, the shell command failed. Re-classify the
    RCA to whichever class the underlying error matches.
  Step 3 (if agent missing IAM):
    Attach `AmazonSSMManagedInstanceCore` to the instance profile.
    Restart the SSM agent on the instance: `sudo systemctl restart
    amazon-ssm-agent`.
  Step 4 (if InvocationDoesNotExist):
    SSM never accepted the invocation. Check IAM permissions on
    the Jenkins agent's role — it needs `ssm:SendCommand` on the
    target document.

Verify:
  Re-run the pipeline. The SSM command returns `Status: Success` +
  the underlying shell script exits 0.
```

## Output schema notes

- `error_class: "ssm"` (only when SSM transport itself is the
  problem). If the underlying shell command failed, RECLASSIFY.
- `failed_stage`: usually `Deploy`, `Deploy Prod+1`, or a custom
  stage that wraps `aws ssm send-command`.
- `evidence[]` must include:
  - `jenkins_log` line with the SSM failure marker
  - `aws:ssm(<instance_id>)` with PingStatus + LastPingDateTime
  - `jenkins_pipeline/<helper>.groovy:<line>` for the `aws ssm
    send-command` call that fired

## Common pitfalls

- **DO NOT classify a failed shell script run via SSM as `ssm`** —
  re-classify based on what the script tried to do (e.g.
  `deploy.sh exit 1` → likely `health_check` or `java_runtime`).
- **DO NOT recommend "open SSH" as a fallback.** BBCTL is the
  org-standard for instance access. Use `bbctl shell <id>` instead.
- **DO NOT recommend running the SSM command manually from
  laptops** — the Jenkins agent runs SSM with a specific IAM role
  that operator laptops likely lack. Re-trigger the build instead.
- **DO NOT cite the pipeline groovy as the root cause** unless the
  command body itself is malformed — most SSM failures are
  agent-side or IAM-side, not pipeline-code-side.
