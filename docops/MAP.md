# docops — ownership map

Prevents future duplication. Each concept has ONE canonical home;
other files link via `See docops/<owner>` rather than re-stating.

When you write or edit a doc and the topic is in the table below,
update the canonical file. When the topic is NOT in the table, add a
new row (and pick which file owns it).

Drift check (recommended monthly):
```
grep -rn "ALB ARN format\|MANDATORY_RUNBOOK\|override-now signal" docops/
```
Should return ≤ 1 hit per concept (the owner). Multiple hits = drift,
fold them back into the owner.

## Ownership matrix

### Per-class drill knowledge (runbooks/)

Each `docops/runbooks/<class>.md` is the SOLE owner of:
- The class's detection signals (regex / log strings to look for)
- The drill plan (which AWS APIs / repo files / Jira fields to fetch)
- The Action template (Finding / Action / Verify shape, including
  PRIMARY/SECONDARY split if applicable)
- The "STRICT — DO NOT" list (anti-patterns specific to that class)
- Output schema notes (which evidence sources are mandatory)

Per-pipeline `job_flows/<pipeline>.md` MUST NOT duplicate the runbook
drill plan. The flow doc owns:
- Pipeline IDENTITY (script_path, library branch, agent options)
- Match signature (so the agent can route to the right flow)
- Parameters table
- Stage table (markers + helper/inline)
- Helper chain narrative (which helper calls which)
- Post block actions
- Stage → likely failure modes table (CLASS NAMES, not drill steps)
- Gotchas specific to this pipeline (operator-relevant quirks)

If a runbook detail belongs to one job_flow only (e.g. Build 15
JsonSlurperClassic secondary symptom for stagger_scaling), put it in
the flow's gotchas section AND in the runbook's "Common pitfalls"
section, with a cross-reference between them. The flow doc gives
pipeline context; the runbook gives the class context.

### Shared RCA rules — prompts/rca_common.md

Single source of truth for rules that apply to BOTH the agent path
(`agent.py`) and the one-shot path (`llm.py`). Owned exclusively by
this file; both `rca_system.md` and `rca_agent_system.md` get it
prepended at load time by `_load_prompt`.

| Concept                          | Owned by `rca_common.md` section            |
|---|---|
| Pipeline overview                | "Pipeline overview"                         |
| Repo layout (jenkins_pipeline / InfraComposer) | "Repos"                       |
| error_class override signals     | "error_class — when to OVERRIDE"            |
| REPEATED INFRASTRUCTURE-NOISE rule | within "error_class — when to OVERRIDE"   |
| Placeholder IDs FORBIDDEN        | "Placeholder IDs in suggested_commands"     |
| ALB ARN derivation from rule_arn | within "Placeholder IDs"                    |
| Evidence rules (verbatim, snippet-fill, main_* excluded) | "Evidence rules" |
| suggested_commands tier semantics| "suggested_commands tier"                   |
| BBCTL command conventions        | "BBCTL command conventions"                 |
| terraform "already exists" order | "terraform \"already exists\" pattern"      |
| Non-fatal noise (4 examples)     | "Non-fatal noise"                           |
| value provenance rule + table    | "STRICT — value provenance rule"            |
| Output format STRICT (no markdown wrapping) | "Output format"                  |

### Path-specific RCA rules

`prompts/rca_agent_system.md` (agent path) owns:
- Boot context + retrieved.rag interpretation
- Method (scan backwards, MANDATORY drill from code, nested-stage
  rule, parallel iter 0 batch)
- Reasoning narration (content vs tool_calls separation)
- Output schema (full version)
- Stopping rules + caps
- health_check class — mandatory files before stopping
- jenkins_agent_offline class — Primary/Secondary framing rule

`prompts/rca_system.md` (one-shot path) owns:
- `failed_stage` from `build_meta.detected_failed_stage`
- Jira / GitHub / Runbook docs context blocks
- Suggested fix STRICT format
- Canary failures (Web / Other, judge_logic, stage_analysis)
- canary_script_error (3 paths)
- Compliance modes 1-5
- Compliance commit-mismatch (Option A / B template)
- health_check failures action template
- Confidence guidance

### Cross-pipeline organization — `docops/jenkins_pipelines_golden.md`

Owns the org-wide view across all pipelines:
- §1 Pipeline catalogue (which `.groovy` file = which job_flow doc)
- §2 Cross-pipeline reference table (Build / Prod+1 / Infra / Deploy
  / Rollout / Cleanup / Failure-path helpers across every pipeline)
- §3 Stage → likely failure modes (universal index, when no
  per-pipeline doc matches yet)
- §3.X Universal rule — check recent commits FIRST when failure
  touches infra code
- §4 Helper summary table
- §5 How to use this doc from the agent
- §6 Maintenance

### Per-pipeline docs — `docops/job_flows/<name>.md`

Each owns:
- Identity (script_path, library branch, agent options)
- Match section (signature lines for routing)
- Parameters table
- Stages table (marker → helper/inline)
- Helper chain narrative
- Post block actions
- Stage → likely failure modes table (per-pipeline, supersedes
  universal table when more specific)
- Gotchas (operator-relevant quirks)
- "Before drilling — check recent commits" pointer (links back to
  `jenkins_pipelines_golden.md` §3.X)

### RAG documentation — `docs/rca/RAGflow.md` + `docs/rca/cli_commands_RAG.md`

`RAGflow.md` owns:
- RAG architecture (PG + pgvector + HNSW)
- Schema (rca_chunks, query_emb_cache, retrieval_cache)
- Indexer (docops + audits + log windows)
- Auto-inject behavior (one-shot vs agent path)
- R-version history (R1 schema, R2 tool, R3 inject, R3.1 sharper
  query, R3.2 anti-anchoring)

`cli_commands_RAG.md` owns:
- All operator commands (index, query, reset, service start/stop)
- Quick troubleshooting commands
- Cheat sheet for "I just edited a runbook, what do I run?"

### Phase-3+ server-side gates — `jenkins_rca/agent.py` (source of truth)

The Phase 4 ULTIMATUM gate (mandatory runbook + jenkins_agent_offline
PRIMARY/SECONDARY check) is server-side code, not docs. The
`MANDATORY_RUNBOOK_CLASSES` set lives in `agent.py` next to the gate
implementation. When you add a class to the set, also:
1. Document the class in `outcome_log.py` failure_signals vocab
2. Update this file's matrix if the class adds new owned concepts

### Failure signal vocabulary — `jenkins_rca/outcome_log.py` docstring

Single owner of the failure_signal name catalog. Code that emits a
new signal MUST add a docstring entry in `outcome_log.py`. Operators
read the catalog to interpret audit log rows.

Current vocab:
- text_tool_calls_rescue / force_final_{wall_clock,cost_cap,iter_cap}
- file_not_found_in_chain / final_json_parse_failed
- low_evidence_count / runbook_evidence_dropped
- hallucinated_file_evidence / hallucinated_snippet
- malformed_final_schema
- ultimatum_gate_triggered
- tier_autobumped_terraform_restricted / hallucinated_id_in_command
- dup_call_warning / dup_call_rejected
- chain_walk_verification_injected

## Anti-duplication rules

1. **One concept, one home.** If you find yourself writing the same
   paragraph in two files, one of them is wrong. Pick the canonical
   home (per this matrix), put it there, link to it from the other.

2. **Cross-references use `docops/<path>` or `[[name]]` style**, not
   "duplicate the section for convenience." A 2-line "see X" is
   cheaper than 80 lines that drift apart over six months.

3. **When you add new doctrine**, update this MAP.md FIRST so future
   contributors know where it lives. If you can't decide which file
   owns it, that's a signal the doctrine is in the wrong scope —
   maybe it should be split or merged.

4. **History docs are exempt.** `docs/rca/jenkins-rca.md`,
   `docs/rca/plan.md`, `docs/rca/analyse.md` etc. are decision history
   / planning artifacts. They MAY contain references to since-deleted
   files (e.g. `HealthCheckFailure.md`). Do not "update" history docs
   to reflect current state — they're snapshots.

## Phase history (Phases 1-5, May 2026)

Phase 1: Deleted `docops/HealthCheckFailure.md` (dup of
`runbooks/health_check.md`), `prompts/docs_system.md` (stale stub).
Merged unique HealthCheckFailure sections into the runbook.

Phase 2: Extracted shared RCA rules to `prompts/rca_common.md`.
Both `rca_system.md` (one-shot) and `rca_agent_system.md` (agent)
now have body-only content; common is prepended at load time.

Phase 3: Wired RAG auto-inject in agent path (`agent.py:
_rag_inject_for_agent`). Agent now sees top-k runbook + audit
chunks in iter 0 primer — previously RAG-blind unless LLM
remembered to call `rag_search`.

Phase 4: ULTIMATUM gate (`agent.py` post-parse). Mandatory runbook
fetch for select classes + PRIMARY/SECONDARY framing check for
`jenkins_agent_offline`. One retry pair on trigger.

Phase 5: This file.
