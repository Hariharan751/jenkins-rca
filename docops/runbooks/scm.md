# Runbook: scm

## What this class means
A source-control operation failed: clone, fetch, push, PR lookup, tag
resolution. Usually GitHub PAT issue, deleted branch/tag, or network
glitch reaching github.com.

## Detect signals
- `fatal: Authentication failed`
- `remote: Repository not found`
- `HTTP 401` / `HTTP 403` from `api.github.com`
- `HTTP 404 ... not found` when resolving a SHA/tag
- `ERROR: Cannot fetch ... 401`
- `Could not resolve ref ...`
- Failed stage often = "Declarative: Checkout SCM" or "Load Library"

## Pipeline source to cross-check (MANDATORY)
- The failing pipeline file (from scriptPath)
- If a custom library load failed: `jenkins_pipeline/Jenkinsfile_*`
  or the library declaration line

## Drill plan
1. `get_jenkins_job_config(job)` → see `scm_url` + `scm_branch`
2. `repo_read_file("jenkins_pipeline", <scriptPath>, 1, 30)` — see any
   `library "...@<branch>"` declarations
3. If commit/PR issue: `github_get_commit(<repo>, <sha>)` to confirm SHA
   actually exists on the branch
4. If PR-title issue: `github_find_pr_for_commit(<repo>, <sha>)`
5. If branch missing: `github_recent_commits(<repo>, <branch>, 5)` to
   verify branch exists

## Action template
```
Finding: SCM <operation> failed: "<exact error from log>".
         Repo: <repo>. Ref: <branch-or-sha>.
         <If 401/403>: GitHub PAT (jenkins-git-bb) may have expired or
                       lacks scope for this repo.
         <If 404 on SHA>: SHA doesn't exist on the branch — maybe wrong
                       branch, force-pushed history, or JFrog version
                       string mistaken for git SHA.
         <If 404 on branch>: Branch was deleted/renamed.

Action:
  <If PAT issue>:
    Jenkins admin: regenerate PAT for shared user 'Jenkins-git-bb' at
    github.com/settings/tokens. Update credential 'jenkins-git-bb' in
    Jenkins. Re-run pipeline.
  <If SHA mismatch>:
    Verify COMMIT_ID param: must be 7-40 hex chars, on the branch you're
    deploying. JFrog version strings (e.g. v6.24) only work if there's
    a matching GitHub tag.
  <If branch deleted>:
    Use a different branch, or restore the deleted branch on GitHub.
Verify:
  Re-run pipeline; expect Checkout SCM / Load Library stage to pass.
```

## Output schema notes
- `error_class: "scm"`
- `evidence[]` must include:
  - `jenkins_log` with the SCM error
  - `jenkins_pipeline/<scriptPath>:<line>` (the library/checkout declaration)
  - If commit issue: `github:<repo>@<sha>` (proof it exists or not)

## Common pitfalls
- DO NOT use BBCTL — this is a GitHub/PAT issue, not an instance issue.
- DO NOT suggest cloning manually as the fix — fix the underlying creds.
