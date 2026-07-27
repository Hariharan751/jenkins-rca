# Runbook: compliance

## What this class means
The Jenkins `JiraDetails` stage rejected the build before any deploy
action. The gate validates the build against Jira + GitHub. Five
distinct sub-modes exist; pick ONE based on the log + Jira ticket data.

## Detect signals
- `Compliance:` prefix in log lines
- `Jira ticket <KEY>` mentioned in failure message
- Failed stage = "Jira Details" (from `[Pipeline] { (Jira Details)` marker)
- `error_class` should be `compliance`

## Pipeline source to cross-check (MANDATORY)
- `jenkins_pipeline/vars/JiraDetails.groovy` — the gate implementation
- Whatever script_path Jenkins runs (e.g. `Jenkinsfile_create_quick_infra`)
  for the call site

## STEP 0 (do this ONLY for the `create-quick-infra` job family)

For the bootstrap job (`create-quick-infra` and its variants), the
compliance gate has a build-param fallback for `SERVICE` — `config.json`
is enrichment only on this job. A compliance failure on this job family
may therefore be a gate-logic regression rather than a real missing
entry. See also the universal "infra-code recent-commits check" in
`docops/jenkins_pipelines_golden.md` §3.

For this job family ONLY:
1. `repo_recent_commits("jenkins_pipeline", 5)` — list the last 5
   commits on the pipeline repo.
2. If any recent commit touched `vars/JiraDetails.groovy` or a file
   containing "Compliance" in its name, open the diff and verify the
   failure mode you are about to recommend a fix for is *still* a
   real check in the current code.
3. If the current code does NOT make this check (or the check was
   weakened/removed), the gate regressed — see Mode 6 below.

Skipping this step has caused at least one wrong-fix RCA (build 42 of
`create-quick-infra`: the agent told the operator to add a service to
`config.json` when the actual fix was to restore the build-param
fallback in `JiraDetails.groovy`).

For OTHER job families (`Stagger Prod Plus One`, `Stagger Prod`, deploy
jobs, canary jobs), skip STEP 0 and go straight to Mode 1-5 — those
jobs legitimately require services to be registered in `config.json`.

## Modes — pick one

### Mode 1 — Ticket status not in allowed list
**Log signal:** `Compliance: Jira ticket <KEY> is not READY FOR RELEASE
(current: Done)` or similar status mismatch.

**Drill plan:**
1. `repo_read_file("jenkins_pipeline", "vars/JiraDetails.groovy", 30, 80)`
2. `jira_get_ticket(<KEY>)` to confirm current status

**Action — SINGLE PATH ONLY. DO NOT EMIT Option A / Option B.**
```
Finding: Jira ticket <KEY> is in status '<current>', but the pipeline
         requires '<expected>' (typically 'READY FOR RELEASE', or
         'HOT FIX' for hotfix pipelines).
Action:  Operator must transition Jira ticket <KEY> to '<expected>' in
         the Jira UI, then re-run the pipeline.
Verify:  Re-run; expect 'Jira Details' stage to pass.
```

**STRICT — what NOT to write for Mode 1:**
- DO NOT suggest "update the pipeline compliance logic to accept '<current>'
  as a valid status" as an alternative path. Compliance gates exist for
  audit/regulatory reasons; the fix is always to fix the Jira state,
  never to weaken the gate.
- DO NOT structure the fix as Option A / Option B. The Option-A/B template
  is only for Mode 2 (commit-mismatch), where operator intent is genuinely
  ambiguous (signed-off wrong vs commit changed). Status mismatch has no
  such ambiguity — the ticket needs to move.
- DO NOT cite "clone detection" as the cause unless the log explicitly
  shows the "is a clone of" line.

### Mode 2 — Missing Signed Off Commit ID
**Log signal:** `Compliance: Jira ticket <KEY> has no Signed Off
commit id` or `Parent ticket has no Signed Off commit id`.

**Drill plan:**
1. `repo_read_file("jenkins_pipeline", "vars/JiraDetails.groovy", ...)`
2. `jira_get_ticket(<KEY>)` — confirm `custom_fields["Signed Off Commit ID"]`
   is null/missing

**Action:**
```
Operator: open Jira <KEY> → edit 'Signed Off Commit ID' field
(customfield_10973) → paste full 40-char SHA of COMMIT_ID =
<actual sha from log> → save. Re-run pipeline.
```
DO NOT suggest Jira REST API curl. UI edit only.

### Mode 3 — Commit SHA mismatch
**Log signal:** `Compliance: Signed Off commit id <X> does not match
resolved SHA <Y>`.

**Drill plan:**
1. `repo_read_file("jenkins_pipeline", "vars/JiraDetails.groovy", ...)`
2. `jira_get_ticket(<KEY>)` — get signed-off SHA
3. `github_get_commit(<repo>, <signed_off_sha>)` — see what was signed off
4. `github_get_commit(<repo>, <resolved_sha>)` — see what build resolved to
5. Compare authors / files_changed / dates

**Action (MUST output BOTH options):**
```
Option A (RECOMMENDED — operator passed wrong param):
  Re-run pipeline with COMMIT_ID/TAG = <signed_off_sha>.
  Check for: leading/trailing space in COMMIT_ID input,
  JFrog version string mistaken for git SHA, wrong branch/tag.

Option B (operator intends new commit — re-sign required):
  Update Jira <KEY> 'Signed Off Commit ID' to <resolved_sha>.
  Ensure merged PR title contains <KEY> (per Mode 5).
  Re-run pipeline.
```

### Mode 4 — Clone-of-clone chain
**Log signal:** `Compliance: clone-of-clone chain detected: X → Y → Z`.

**Drill plan:**
1. `jira_search('issuekey = X OR issuekey = Y OR issuekey = Z')`
2. For each ticket: `jira_get_ticket(<key>)` to see clone chain

**Action:**
```
Operator: identify the correct parent ticket (the original, not the
grandparent). Re-run pipeline with that ticket as Jira-Ticket param.
```

### Mode 5 — PR title missing Jira ticket ID
**Log signal:** `Compliance: merged PR title does not contain <KEY>`.

**Drill plan:**
1. `jira_get_ticket(<KEY>)` — confirm ticket exists
2. `github_find_pr_for_commit(<repo>, <sha>)` — get the offending PR

**Action:**
```
gh pr edit <PR_NUMBER> --title '`<KEY>` <existing title>'
```
Re-run pipeline.

### Mode 6 — Service not in `config.json` (GATE BUG, `create-quick-infra` family ONLY)

> **🚨 ABSOLUTE RULE — for `create-quick-infra`: NEVER recommend
> editing `config.json`.** This job is the BOOTSTRAP that CREATES the
> infra for a brand-new service. `config.json` does NOT have an entry
> for the service yet — that is the DESIGN, not the bug.
>
> Pipeline order of operations:
>   1. **`create-quick-infra`** — provisions the infra (THIS job)
>   2. **`Stagger-Onboarding`** — writes the `config.json` entry,
>      referencing the infra from step 1
>   3. `Stagger Prod Plus One` (and other deploy jobs) — use the
>      `config.json` entry from step 2
>
> Step 1 cannot depend on the output of step 2. If the agent's RCA
> says "add the service to config.json" for a `create-quick-infra`
> failure, it has confused the order. The real fix is one of:
> gate-logic regression, missing `team-board-mapping` entry, or
> wrong build params — never `vim config.json`.

**Job scope (REQUIRED):**
This mode applies ONLY when ALL of:
- `build_meta.job` ∈ {`create-quick-infra`, `create-quick-infra-*`,
  `*-quick-infra`} — the quick-infra family
- Log signal matches `Compliance: SERVICE '<service>' not found in config.json`
  (or close wording: "service ... not registered", "no entry in service
  registry")

For any OTHER job (`Stagger Prod Plus One`, `Stagger Prod`, deploy jobs,
canary jobs, …) a missing `config.json` entry is the legitimate failure
mode — those jobs DO require the service to be registered, and the
correct fix is to add the entry. Use the default "register service"
guidance for those, NOT Mode 6.

**Why this is a gate bug ONLY for `create-quick-infra`:**
`create-quick-infra` is the bootstrap job — it spins up infra for a NEW
service that does not yet exist in `config.json` (that's the whole
point). The compliance gate has a build-param fallback for this job:
the service identity is sourced from the git build parameters
(`SERVICE` / `COMMIT_ID` / repo URL passed in by the trigger), and
`config.json` is used only as an enrichment lookup (team, NewRelic
name, Jira board). A missing `config.json` entry should not block the
quick-infra build.

If you see this error in a fresh `create-quick-infra` run, the gate
either regressed (the fallback was removed) or the build is running
on a stale branch that pre-dates the fallback.

**Drill plan:**
1. Confirm job scope: `build_meta.job` matches the quick-infra family.
   If not, abandon Mode 6 and go back to the regular registration fix.
2. `repo_recent_commits("jenkins_pipeline", 5)` — find the build-param
   fallback patch on `vars/JiraDetails.groovy` (commit message likely
   mentions "compliance", "config.json", "quick-infra", "service
   routing", or "build params"). If it's not within the last 5 commits,
   widen to 10 or 20.
3. `repo_read_file("jenkins_pipeline", "vars/JiraDetails.groovy", ...)`
   at the lines that decide the service lookup — confirm the
   build-param fallback path exists for the quick-infra job branch.
4. If the fallback is missing → the patch reverted or was never merged
   on this branch.
5. If the fallback is present but did not fire → check the build params
   actually carried `SERVICE`. The Jenkins job config or the trigger
   payload may have dropped it.

**Action (`create-quick-infra` family ONLY):**
```
Finding: create-quick-infra build failed with 'SERVICE <s> not found in
         config.json'. For the quick-infra bootstrap job this message
         is misleading — the gate is supposed to derive SERVICE from
         git build params (the service is new and not yet in
         config.json by design). Either the build-param fallback was
         removed or build params dropped SERVICE.
Action:  DO NOT edit config.json. Either:
         (a) Verify vars/JiraDetails.groovy has the build-param
             fallback for the quick-infra branch (recent patch on
             this file). If missing, re-apply / cherry-pick the
             patch and re-run.
         (b) If the fallback is present, inspect the Jenkins job's
             SERVICE / build-param wiring — the trigger likely
             dropped SERVICE from the parameter set.
Verify:  Re-run pipeline; expect 'Jira Details' stage to pass without
         any config.json change.
```

**STRICT — what NOT to write for Mode 6:**
- DO NOT apply Mode 6 to any job outside the `create-quick-infra`
  family. Other jobs' compliance gates legitimately require
  `config.json` registration; treating them as gate bugs would mask
  real misconfiguration.
- DO NOT recommend editing `jenkins_pipeline/resources/config.json` to
  add the missing service for `create-quick-infra` — that workaround
  re-couples the gate to a file the patch specifically decoupled it
  from for the bootstrap case.
- DO NOT cite `config.json` as the offending file in `evidence[]` for
  Mode 6. The offender is `vars/JiraDetails.groovy` (the gate) or the
  Jenkins job parameter wiring.
- DO NOT propose a `vim config.json` + `git push` recipe.

## Output schema notes
- `error_class: "compliance"`
- `failed_stage: "Jira Details"`
- `evidence[]` must include:
  - `jenkins_log` line with the Compliance prefix
  - `jenkins_pipeline/vars/JiraDetails.groovy:<line>` (the check that fired)
  - `jira:<KEY>` (the ticket field that failed)
  - For Mode 3: BOTH `github:<repo>@<sha>` entries

## Common pitfalls
- DO NOT cite "clone detection" as cause unless log explicitly says so.
- DO NOT suggest `curl -X PUT ... atlassian.net/rest/api/2/issue/...` —
  custom-field edit via REST often needs special perms; UI is org-standard.
- DO NOT use BBCTL commands here — this is a Jira/GitHub fix, not an
  instance fix.
