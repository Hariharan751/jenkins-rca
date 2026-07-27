# Job-flow index — pick the right doc per Jenkins job

This directory has one markdown file per Jenkins pipeline family. Each
file describes WHERE that family's code lives — main pipeline path,
top-level stages, which helper file each stage delegates to, and where
chains nest into other helpers.

These are FACTUAL descriptions derived from reading the actual pipeline
source. They contain only file path references — no error message
examples, no port numbers, no ARNs, no fix recipes. For fix recipes use
the error-class runbooks under `../runbooks/`.

## How to match a Jenkins job to a job_flow doc

The Jenkins display name (`build_meta.job`, e.g. "Stagger Prod Plus One")
is just a label set by ops. It is NOT used for routing.

Use this priority order:

1. **`script_path`** from `get_jenkins_job_config(job)` response.
   If non-null, this is the .groovy filename Jenkins loads from the
   `jenkins_pipeline` repo. Match its stem (filename without `.groovy`)
   against the job_flow doc name. Example: `script_path =
   "main_stagger_prod_plus_one.groovy"` → read job_flow
   `main_stagger_prod_plus_one`.

2. **`inline_script`** body from the same response.
   If `script_path` is null, Jenkins runs an inline pipeline body
   stored in its own config. Match by SIGNATURE LINES inside that body
   — distinctive helper calls or stage names. Each job_flow doc's
   `## Match` section lists the signature lines to look for.

3. **`repo_list_dir("jenkins_pipeline", "")`** as a discovery fallback.
   If neither (1) nor (2) matches an existing flow doc, call this to
   list main pipeline files. The job is likely a new family not yet
   documented. Read the file whose name best fits the
   `script_path` / `inline_script` evidence. Derive the chain by
   reading its body — universal Jenkins facts below still apply.

When in doubt about which flow to read, call `list_job_flows()` (cheap)
and skim the `match` strings before committing to one. Never trust the
Jenkins display name alone — it can be renamed without code changes.

## Where the source lives

| What | Where |
|------|-------|
| Main pipeline entrypoints | `jenkins_pipeline/<job_family>.groovy` |
| Pipeline-step helpers (Jenkins shared-lib convention) | `jenkins_pipeline/vars/<helperName>.groovy` |
| Shell scripts, config.json, canary configs, conf templates | `jenkins_pipeline/resources/...` |
| Utility classes | `jenkins_pipeline/src/com/blackbuck/...` |
| Terraform modules for infra creation (Prod & Prod+1) | `InfraComposer/...` |

## Universal Jenkins shared-lib facts (true for ALL flows)

- A call to `someName(...)` from any pipeline OR helper is the step
  defined in `vars/someName.groovy` (its `def call(...)` is the body).
- `libraryResource 'path/to/x'` resolves to `resources/path/to/x` on
  disk.
- `import com.blackbuck.<pkg>.<Class>` resolves to
  `src/com/blackbuck/<pkg>/<Class>.groovy`.

These are framework facts, not project-specific. They are TRUE for any
helper name you encounter — apply them to the helper names you read
out of the actual code, not to names you guessed.
