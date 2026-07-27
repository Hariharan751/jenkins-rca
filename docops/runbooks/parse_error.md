# Runbook: parse_error

## What this class means
A pipeline stage tried to parse a config file (most often
`resources/config.json`) with `jq` or similar and the parser exited
with an error. Usually a missing comma, wrong type (string vs int),
or unescaped char.

## Detect signals
- `parse error: Invalid numeric literal` (jq)
- `parse error: Expected separator between values`
- `jq: error (at ...)` with exit code 4
- `JSONDecodeError` / `yaml.parser.ParserError`
- Failed stage often `Infra` or `Resolve Parameters`

## Pipeline source to cross-check (MANDATORY)
- Whatever `vars/<helper>.groovy` runs the `jq` command (often
  `createGreenInfra.groovy` or `nonwebdeploy.groovy`)
- `jenkins_pipeline/resources/config.json` at the line cited in the error

## Drill plan
1. `get_jenkins_job_config(job)` → scriptPath
2. `repo_read_file("jenkins_pipeline", <scriptPath>, ...)` around failed stage
3. `repo_search("jenkins_pipeline", "jq -r")` to find the jq caller
4. `repo_read_file("jenkins_pipeline", "<jq-caller>.groovy", ...)` for context
5. `repo_read_file("jenkins_pipeline", "resources/config.json", <line>-5, <line>+5)`
   where `<line>` is from the error message (e.g. "line 74, column 401")
6. `repo_recent_commits("jenkins_pipeline", 10)` to check if config was just edited

## Action template
```
Finding: config.json line <N> has <bad value description>
         (e.g. string "50" where jq expects integer, missing comma after key,
         unescaped quote).
Action:  Edit jenkins_pipeline/resources/config.json line <N> to <fix>.
         Validate locally: cat config.json | jq '.<service>'
         Commit + push + re-run pipeline.
Verify:  Re-run pipeline; expect Infra stage to pass past the jq command.
```

## Output schema notes
- `error_class: "parse_error"`
- `evidence[]` must include:
  - `jenkins_log` with parse error line + line/column
  - `jenkins_pipeline/<jq-caller>.groovy:<line>` (the `jq -r '...'` call site)
  - `jenkins_pipeline/resources/config.json:<line>` (the offending row)

## Common pitfalls
- DO NOT cite the jq command itself as root cause — config.json is the cause.
- DO NOT suggest editing config.json on the Jenkins node directly — it's in
  the git repo, edit + commit + push.
- DO NOT use BBCTL — this is a config-repo fix, not an instance fix.
