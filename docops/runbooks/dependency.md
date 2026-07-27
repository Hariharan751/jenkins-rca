# Runbook: dependency

## What this class means

A build-tool dependency resolution failure — Gradle, Maven, npm, pip,
or apt cannot fetch a required artifact from its remote repository.
The pipeline aborts with messages like:

```
Could not resolve <group>:<artifact>:<version>
artifact not found
Download failed
BUILD FAILED ... dependencies
npm ERR! 404 Not Found
pip._vendor.resolvelib... Could not find a version that satisfies
```

Distinct from `build_tool_crash` (the build tool's process died —
OOM, signal) and from `scm` (git/repo access failed). `dependency`
is "the build tool ran fine, but a transitive artifact couldn't be
downloaded."

## Detect signals (primary)

- `Could not resolve <group>:<artifact>:<version>` (Gradle)
- `[ERROR] Failed to execute goal ... missing artifact` (Maven)
- `npm ERR! code E404` / `npm ERR! 404 Not Found`
- `ERROR: Could not find a version that satisfies the requirement`
  (pip)
- `Unable to locate package` (apt)
- `artifact ... not found` (generic)
- `Download failed` followed by a 404/403 from the registry

## Drill plan

1. **Identify the missing artifact.** The log line names it:
   `<group>:<artifact>:<version>`. Note this verbatim for evidence.
2. **Identify the registry attempted.** Build tools show the URL
   they tried (e.g. `https://repo.maven.apache.org/maven2/...`,
   `https://repo.jfrog.bbctl.com/...`). The registry name + the
   failure code (403 vs 404) determines next step.
3. **Identify the upstream.** Run `repo_recent_commits("<service-
   repo>", 10)` for the service being built (NOT jenkins_pipeline)
   — did anyone bump a dependency version recently? If yes, that
   commit is the prime suspect.
4. **Check the registry.** For JFrog, `curl -I -u<creds>
   https://repo.jfrog.bbctl.com/<path>` — does the artifact exist
   there? 200 = build tool config issue; 404 = artifact never
   published; 403 = auth/permissions.
5. **Check build config** (`build.gradle`, `pom.xml`,
   `package.json`, `requirements.txt`) — is the version pinned vs
   floating? Floating version + upstream yank = sudden 404.

## Action template

```
Finding:
  Build <N> of <job> in stage `<stage>` failed to resolve
  `<group>:<artifact>:<version>` from `<registry>`. Build tool
  exited because the dependency is required at compile time and
  not available locally or remote.

Action:
  Step 1 (CONFIRM — is the artifact actually published?):
    Check the registry directly: `curl -I -u<creds>
    <registry>/<path-to-artifact>`. If 404, the artifact was never
    published or was yanked. If 200, it exists — local cache or
    auth issue.
  Step 2 (if artifact missing — fix the version):
    Edit the service's build config to a known-good version (look
    at the artifact's registry listing for the latest valid
    version). Commit + push the service repo. Re-run the pipeline.
  Step 3 (if auth — fix credentials):
    For JFrog: confirm the Jenkins agent has the `bb-jfrog-creds`
    credential bound. Re-run.
  Step 4 (if registry was the wrong one — bad config):
    Some service repos point at a stale internal registry. Update
    the `repositories { ... }` block in `build.gradle` (or
    equivalent in pom.xml / package.json) to the current registry
    URL. Commit + push the service repo.

Verify:
  Re-run the pipeline. The build tool's dependency-resolution phase
  completes without 404/403.
```

## Output schema notes

- `error_class: "dependency"`
- `failed_stage`: usually `Build` (or `Build Frontend` for npm).
- `evidence[]` must include:
  - `jenkins_log` line with the `Could not resolve` / `404 Not
    Found` failure
  - `jenkins_log` line with the registry URL the build tool tried
  - When relevant, the service repo's build config file
    (`<service>/build.gradle:<line>` etc) showing the dependency
    declaration

## Common pitfalls

- **DO NOT classify a Gradle daemon crash as `dependency`** — that's
  `build_tool_crash` (the process died). `dependency` is "process
  ran fine, network 404'd on an artifact."
- **DO NOT recommend deleting `~/.gradle/caches/`** as a fix unless
  the log shows checksum mismatch. Cache deletion is a workaround,
  not a root cause.
- **DO NOT recommend pinning the version to LATEST** — that
  reintroduces the floating-version problem. Pin to a specific
  known-good version.
- **DO NOT recommend editing `jenkins_pipeline/` for this class** —
  the dependency lives in the SERVICE repo's build config, not the
  pipeline.
