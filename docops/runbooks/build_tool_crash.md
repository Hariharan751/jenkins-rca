# Runbook: build_tool_crash

## What this class means

The Gradle (or Maven) build daemon crashed mid-task. Pipeline aborts
in the `Build` stage with messages like:

```
The message received from the daemon indicates that the daemon has disappeared.
Daemon vm is shutting down... The daemon has exited normally or was terminated in response to a user interrupt.
FAILURE: Build failed with an exception.
* What went wrong:
Gradle build daemon disappeared unexpectedly (it may have been killed or may have crashed)
```

This is **build-tooling infrastructure**, not pipeline code or app
code. The daemon process is what runs `./gradlew clean build -xtest`;
if its JVM dies, the build can't complete. The compile warnings
preceding the failure (e.g. `@Builder will ignore the initializing
expression`) are NOT the cause — they're informational, not fatal.

## Detect signals

Primary (mandatory — at least one):
- `Gradle build daemon disappeared`
- `The message received from the daemon indicates that the daemon has disappeared`
- `Daemon vm is shutting down`
- `Maven daemon was killed` / `mvnd.*daemon.*disappeared`

Companions (do NOT classify on these alone):
- `daemonOpts=-XX:MaxMetaspaceSize=...-Xms256m,-Xmx512m` in the daemon
  log dump (default daemon heap is small)
- `* What went wrong: Gradle build daemon disappeared` at the bottom
- `* Try: Run with --stacktrace option` Gradle help footer

## Drill plan

1. **Confirm primary signal.** Pull the daemon PID from the log
   (`Daemon pid: <N>`). Note in `evidence[]`.
2. **Identify the heap config.** Look for `daemonOpts=...` line in
   the daemon log dump — extract `-Xmx<size>`. Default Gradle daemon
   heap is `512m`. For large Spring/Lombok projects with
   `compileJava` doing annotation processing, 512m is frequently OOM.
3. **Check the failing project**. Read the build.gradle file of the
   service being built (e.g. `repo_read_file("<service_repo>",
   "build.gradle", 1, 50)`). Note Spring Boot version, Lombok presence
   — large dependency graphs need bigger daemon heap.
4. **Check JenkinsMasterRole / build-agent free memory** if AWS state
   accessible — daemon OOM often correlates with agent host memory
   pressure (other concurrent builds, leaked daemon processes).
5. **Skip drilling app-code stack traces** — the warnings in the log
   are pre-failure compile chatter; nothing in `vars/*.groovy` or the
   app source is the root cause.

## Action template

```
Finding:
  Build <N> of <job> failed in stage `Build`. Gradle build daemon
  (pid <DAEMON_PID>) disappeared mid-`compileJava` while building
  <service>. Daemon heap was -Xmx<HEAP_FROM_LOG> (default Gradle
  daemon heap is 512m). The daemon log shows normal compile chatter
  followed by `Daemon vm is shutting down` with no application
  exception — strong signal of JVM OOM on the daemon itself.

Action:
  Step 1 (PRIMARY — raise daemon heap):
    Add `org.gradle.jvmargs=-Xmx2g -XX:MaxMetaspaceSize=512m` to
    `<service_repo>/gradle.properties`. Commit + re-run pipeline.
    For very large modules, try `-Xmx4g`.

  Step 2 (verify build-agent memory headroom — OPTIONAL):
    SSH to JenkinsMasterRole host. Run `free -m` and `ps aux | grep
    -i gradle`. If multiple stale daemon processes (>1h old) are
    pinned, kill them: `pkill -9 -f 'GradleDaemon'`. Long-term: set
    `org.gradle.daemon.idletimeout=10800000` in gradle.properties so
    daemons auto-recycle.

  Step 3 (SECONDARY — drop unused compile-time deps if Step 1 fails):
    Audit `build.gradle` dependencies — large frameworks (Spring
    Boot, Lombok with full annotation graph) inflate Metaspace.
    Remove dependencies the service no longer uses. Lower priority
    than Step 1 since Step 1 buys time immediately.

Verify:
  Re-trigger the failing build (same SERVICE / COMMIT_ID / Jira-
  Ticket). Expect `compileJava` to complete (warnings still appear
  but BUILD SUCCESSFUL replaces the daemon-disappeared error).
```

## Output schema notes

- `error_class: "build_tool_crash"`
- `failed_stage`: the stage marker — `Build` for stagger pipelines.
  Stages AFTER Build (`Prod+1`, `Infra`, `Deploy`, `Rollout`,
  `Destroy`) all show `skipped due to earlier failure(s)` — those
  are downstream, NOT the cause.
- `evidence[]` must include:
  - `jenkins_log` line with `Gradle build daemon disappeared`
  - `jenkins_log` line with `Daemon pid: <N>` for the crashed daemon
  - `jenkins_log` line with `daemonOpts=...` to capture the actual
    heap that wasn't enough
  - Optionally `<service_repo>/gradle.properties` if currently
    pinned at a too-low heap

## Common pitfalls

- **DO NOT classify as `compliance` just because the `=== Compliance:
  ...` info banners appear earlier in the log.** Those are positive
  status messages from JiraDetails — they ONLY indicate compliance
  failure when preceded by `ERROR: Compliance:` or `Compliance: ...
  has no Signed Off`. Stagger Prod+1 build 5225 case was misrouted
  to compliance because of this pattern.
- **DO NOT classify as `java_runtime`** — the compileJava warnings
  (e.g. `@Builder will ignore the initializing expression`) are not
  exceptions, they're javac advice. The actual fatal is the daemon
  death.
- **DO NOT recommend re-signing Jira or editing config.json** — both
  are common compliance/parse_error fixes that have nothing to do
  with this class. Daemon OOM is build-tooling infra.
- **DO NOT cite `vars/*.groovy` files as the root** — the pipeline
  helper just shells out `./gradlew ...`. The pipeline did its job;
  the build tool's daemon crashed.
- **DO NOT recommend a code-level fix in the failing service** —
  warnings about `@Builder.Default` are Lombok hints, not causes.
- **DO NOT recommend "re-run" alone** — without the heap bump or
  daemon kill, the next run likely hits the same OOM (especially
  during peak hours when multiple services build concurrently).
