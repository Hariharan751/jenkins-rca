# Runbook: java_runtime

## What this class means
The Groovy/Java VM running the pipeline threw an exception. Could be
a code bug (MissingMethodException, NullPointerException) in the
pipeline shared library, or an exception thrown by the deployed
service itself. The log usually has a stack trace.

## Detect signals
- `groovy.lang.MissingMethodException`
- `java.lang.NullPointerException`
- `groovy.lang.MissingPropertyException`
- `org.codehaus.groovy.runtime.NullObject.invokeMethod`
- Stack trace lines like `at vars.<helper>.<method>(<helper>.groovy:<line>)`
- `WorkflowScript:<line>: <error>`

## Pipeline source to cross-check (MANDATORY)
- The entrypoint `.groovy` (from `scriptPath`)
- The `vars/<helper>.groovy` named in the stack trace
- Recent commits to spot a just-broken change

## Drill plan
1. `get_jenkins_job_config(job)` → scriptPath
2. Parse stack trace for the deepest in-our-code frame:
   `WorkflowScript:<line>` → that's the scriptPath at <line>
   `vars/<helper>.groovy:<line>` → that's the helper
3. `repo_read_file("jenkins_pipeline", <scriptPath>, <line>-10, <line>+10)`
   to see the failing call site
4. `repo_find_function("jenkins_pipeline", "<helper-name>")` to locate impl
5. `repo_read_file("jenkins_pipeline", "vars/<helper>.groovy", 1, 50)` to read impl signature
6. `repo_recent_commits("jenkins_pipeline", 10)` — was scriptPath or helper just changed?
7. If service code is suspected (less common): `github_read_file("<service_repo>", "<path>", <COMMIT_ID>, ...)`

## Action template
```
Finding: <ExceptionClass> at <file>:<line> — <one-line description of mismatch>.
         Example: "Call site at Jenkinsfile_create_quick_infra:330 passes 1 arg
         (the Jira ticket), but the helper at vars/JiraDetails.groovy:9
         requires 3 args: service, commitId, jiraTicket."
Action:  Edit <file>:<line> to match helper signature.
         Or update helper to accept fewer args + provide defaults.
Verify:  Re-run pipeline; expect stage to pass past the failing line.
```

## Output schema notes
- `error_class: "java_runtime"`
- `evidence[]` must include:
  - `jenkins_log` with the exception line
  - `jenkins_pipeline/<scriptPath>:<line>` (the call site)
  - `jenkins_pipeline/vars/<helper>.groovy:<line>` (the impl signature)
- If recently committed by someone: cite their commit in `root_cause`

## Common pitfalls
- DO NOT stop at the call site — read the helper impl too.
- `vars/<X>.groovy` defining `def call(...)` IS the implementation of step
  `<X>()` (Jenkins shared-lib convention). `repo_find_function` handles this.
- DO NOT use BBCTL — this is a code fix in jenkins_pipeline repo.
