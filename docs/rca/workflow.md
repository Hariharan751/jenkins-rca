# jenkins-rca — code-base workflow

End-to-end trace of one RCA request, from `POST /v1/rca` through
the LangGraph gates and audit persistence. Use this as the
single-page mental model when reading the codebase.

For the per-layer design rationale (why RAG, why LangGraph, why
Postgres), read [`RAG_and_LangGraph.md`](RAG_and_LangGraph.md).
For ops procedures (deploy / restart / secrets), read
[`bbctlrca.md`](bbctlrca.md).

---

## 1. Top-level flow (12 steps)

```
                ┌──────────────────────────────────────┐
                │  Jenkins build fails                 │
                │  → curl POST /v1/rca {job, build, …} │
                └──────────────────┬───────────────────┘
                                   ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  jenkins_rca/main.py  —  FastAPI endpoint                        │
 │  1. signature verify (webhook HMAC)                            │
 │  2. fetch Jenkins console log (jenkins.get_console_log)        │
 │  3. classifier (regex over log_window)        → error_class    │
 │  4. route: AGENT_CLASSES → agent.py                            │
 │            else         → llm.py one-shot path                 │
 └──────────────────────────────┬─────────────────────────────────┘
                                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  jenkins_rca/agent.py  —  primer (BEFORE first LLM call)         │
 │                                                                │
 │  asyncio.gather:                                               │
 │    ├─ service.lookup(service)                  [mcp_tools]     │
 │    ├─ jira_prefetch_for_agent(log)             [jira API]      │
 │    ├─ jenkins_node_prefetch_for_agent(log)     [Jenkins API]   │
 │    └─ rag_inject_for_agent(log, class)         [PG vector]     │
 │                                                                │
 │  System message = rca_common.md + rca_agent_system.md          │
 │                 + ## build_meta                                │
 │                 + ## service.lookup                            │
 │                 + ## log_window                                │
 │                 + ## jira.tickets        (if Jira key in log)  │
 │                 + ## jenkins.node        (if slave-N in log)   │
 │                 + ## retrieved.rag       (top-k chunks)        │
 └──────────────────────────────┬─────────────────────────────────┘
                                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  agent loop (run_agent)  —  OpenAI function-calling            │
 │                                                                │
 │  iter 0  (parallel tool_calls)                                 │
 │    ├─ read_runbook(class)                                      │
 │    ├─ list_job_flows()                                         │
 │    ├─ get_jenkins_job_config(job)                              │
 │    ├─ repo_read_file("jenkins_pipeline", main_pipeline.groovy) │
 │    ├─ aws_describe(...)                  [class-dependent]     │
 │    └─ rag_search(query, k=5)             [if more context needed]
 │                                                                │
 │  iter 1+: drill into helpers identified by iter 0              │
 │           (vars/<helper>.groovy / resources/scripts/…)         │
 │                                                                │
 │  Target ≤ 3 iters. Hard caps: 25 tool calls / 180s / $5.       │
 │                                                                │
 │  Output: final_text (RCA JSON)                                 │
 └──────────────────────────────┬─────────────────────────────────┘
                                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  Schema-completion retry                                       │
 │    _parse_final_json(final_text) → rca                         │
 │    If missing required keys (summary / failed_stage / …):      │
 │       fire ONE more LLM call w/ response_format=json_object    │
 │       to force the full RCA schema.                            │
 │       Emit `malformed_final_schema` signal.                    │
 └──────────────────────────────┬─────────────────────────────────┘
                                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  LangGraph ULTIMATUM gates  (gates.py:build_gate_graph)        │
 │                                                                │
 │   check_schema      → backfill defaults                        │
 │       ↓                                                        │
 │   check_runbook_fetched ─ trip ─► corrective_reiter            │
 │       ↓ pass                       (one tool iter + final JSON)│
 │   check_primary_secondary ─ trip ► corrective_reiter           │
 │       ↓ pass                                                   │
 │   check_compliance_hallucination ► corrective_reiter           │
 │       ↓ pass                                                   │
 │   END                                                          │
 │                                                                │
 │   retry_budget=1, recursion_limit=25 (prevents tight loops).   │
 │   Signal: `ultimatum_gate_triggered` per gate fire.            │
 └──────────────────────────────┬─────────────────────────────────┘
                                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  Validator hard gates  (agent.py post-graph)                   │
 │                                                                │
 │   _fill_repo_snippets               — fill snippet from disk   │
 │   drop entries with _error          — unreadable file path     │
 │   _filter_fake_repo_evidence        — file not in read_files   │
 │   _filter_hallucinated_snippets     — quoted-string mismatch   │
 │   _drop_unfetched_runbook_evidence  — runbook never fetched    │
 │                                                                │
 │   Signals: hallucinated_file_evidence / hallucinated_snippet / │
 │            runbook_evidence_dropped                            │
 └──────────────────────────────┬─────────────────────────────────┘
                                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  Server-side slave-ID substitution  (Phase 7)                  │
 │                                                                │
 │   If log mentions slave-N AND any cmd has <slave-instance-id>: │
 │     try jenkins.get_node_info(slave-N)                         │
 │       → real id  → substitute into every offending cmd         │
 │                    emit `server_substituted_slave_id`          │
 │       → no id    → drop cmd; if all cmds dropped, append       │
 │                    safe-tier ops-contact fallback              │
 │                    emit `server_dropped_unresolvable_slave_cmd`│
 └──────────────────────────────┬─────────────────────────────────┘
                                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  value_validator                                               │
 │   port 8080      → real target_port from service.lookup        │
 │   /admin/version → real health_check_path                      │
 │   gps.log        → real filebeat_log_path                      │
 │   Signal: value_validator_substituted                          │
 └──────────────────────────────┬─────────────────────────────────┘
                                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  Persistence                                                   │
 │   audit.record(payload)                                        │
 │     → /var/log/jenkins-rca/<request_id>.json                     │
 │   outcome_log.log(...)                                         │
 │     → /var/cache/jenkins-rca/outcomes.sqlite                     │
 │   trace (debug)                                                │
 │     → /var/log/jenkins-rca/jenkins-rca-trace-<job>-<build>.txt     │
 └──────────────────────────────┬─────────────────────────────────┘
                                ▼
                       Response → operator
                       (Jenkins console / report URL)
```

---

## 2. Parallel structure inside the primer

Three independent context blocks fetched concurrently. Cuts wall
time by ~70% vs serial.

```
                          _build_primer(...)
                                  │
              ┌───────────────────┼───────────────────┬──────────────────┐
              ▼                   ▼                   ▼                  ▼
   ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌────────────┐
   │ service.lookup() │ │ jira.fetch_all() │ │ jenkins.get_node │ │ rag.search │
   │ (mcp_tools)      │ │ for keys in log  │ │ _info(slave-N)   │ │  k=5       │
   │ ~ms              │ │ ~500ms / ticket  │ │ ~200ms           │ │ ~300ms     │
   └────────┬─────────┘ └────────┬─────────┘ └────────┬─────────┘ └─────┬──────┘
            │                    │                    │                 │
            └─────────────────┬──┴────────────────────┴─────────────────┘
                              ▼
              ## blocks concatenated into system message
                              ▼
                       OpenAI iter 0 starts
```

`_build_primer` is `async def`. Each helper is awaited individually
today (not strictly parallel via `gather`) but each is the only
network call in its branch, so the cost is bounded by the slowest
single block (~500ms typical, ~1.5s worst case).

---

## 3. Tool calls in iter 0 (parallel via OpenAI function-calling)

The LLM can emit multiple tool_calls in ONE response. We dispatch
them concurrently and feed all results back before iter 1.

```
                       LLM picks tools  (one response, N calls)
                                  │
        ┌─────────────────────────┼─────────────────────┬──────────────────┐
        ▼                         ▼                     ▼                  ▼
 ┌──────────────┐          ┌────────────┐        ┌────────────┐    ┌──────────────┐
 │ read_runbook │          │ list_job_  │        │ repo_read_ │    │ rag_search   │
 │  (class)     │          │  flows()   │        │  file(...) │    │  (focused q) │
 └──────┬───────┘          └─────┬──────┘        └─────┬──────┘    └──────┬───────┘
        │                        │                     │                  │
        └────────────────────────┴──────┬──────────────┴──────────────────┘
                                        ▼
                              all results → messages
                                        ▼
                               iter 1 (deeper drill)
```

Tool dedup cache (`fingerprint = (name, json.dumps(args))`) prevents
the LLM from re-issuing the same call across iters — wastes input
tokens on redundant context resend.

---

## 4. LangGraph gate routing (state machine)

```
                       START
                          │
                          ▼
                  ┌────────────────┐
                  │  check_schema  │ ─── all keys present? ────┐
                  └───────┬────────┘                            │
                          │ missing → backfill                  │
                          ▼                                     │
                  ┌──────────────────────────┐                  │
                  │  check_runbook_fetched   │ ── pass ─────────┤
                  └───────┬──────────────────┘                  │
                          │ trip                                │
                          ▼                                     │
                   ┌──────────────────┐                         │
                   │ corrective_reiter│  (tool iter + final JSON)
                   └────────┬─────────┘                         │
                            │   retry_budget=0? ─── yes ─► END  │
                            │   no                              │
                            └──────────────┐                    │
                                           │                    │
                  ┌────────────────────────┴────┐               │
                  │  check_primary_secondary    │ ── pass ──────┤
                  └─────────┬───────────────────┘               │
                            │ trip                              │
                            ▼                                   │
                     (corrective_reiter)                        │
                            │                                   │
                  ┌─────────┴────────────────────────┐          │
                  │ check_compliance_hallucination   │ ─ pass ──┤
                  └─────────┬────────────────────────┘          │
                            │ trip                              │
                            ▼                                   │
                     (corrective_reiter)                        │
                            │                                   │
                            ▼                                   │
                          END  ◄─────────────────────────────────┘
```

Each `trip → corrective_reiter` is a single retry pair (one
tool-iter + one forced-JSON finalize). `retry_budget` starts at 1;
once exhausted, reiter becomes a no-op and the next gate trip
routes directly to END.

---

## 5. Class routing

```
log_window
   │
   ▼
classifier (regex, first-match-wins)  ──► error_class
   │
   ▼
in AGENT_CLASSES (17/18)?
   │                                   one-shot path (only `unknown` falls here)
   ▼ yes                                          │
agent.py:run_agent                                ▼
   │                                       llm.py:run_rca
   ▼                                          (Gemini or OpenAI,
LangGraph gates                                no tool calls)
   │
   ▼
output
```

Agent path = recursive tool-using LLM with full RAG + LangGraph.
One-shot path = single LLM call with pre-stuffed context, no tool
use, no gates — kept as fallback for trivial cases AND for the
`unknown` class where the gate plan doesn't apply.

---

## 6. Where the codebase modules attach to each step

| Step | Module |
|---|---|
| HTTP entry + webhook auth | `jenkins_rca/main.py`, `jenkins_rca/auth.py` |
| Jenkins log fetch | `jenkins_rca/jenkins.py` (REST helpers) |
| Classifier (regex) | `jenkins_rca/classifier.py` + `classifier_rules.yml` |
| Service.lookup | `jenkins_rca/mcp_tools.py:service_lookup` |
| Jira pre-fetch | `jenkins_rca/jira.py:extract_tickets` + `fetch_all` |
| Jenkins node pre-fetch | `jenkins_rca/jenkins.py:get_node_info` |
| RAG search | `jenkins_rca/rag.py:search`, `embed` |
| RAG indexing | `jenkins_rca/rag.py:index_docops`, `index_audits` |
| Agent loop | `jenkins_rca/agent.py:run_agent` |
| Tool dispatch | `jenkins_rca/agent_dispatch.py` (single `TOOL_DISPATCH` dict) |
| LangGraph gates | `jenkins_rca/gates.py:build_gate_graph` |
| Validator hard gates | `jenkins_rca/agent.py` (`_filter_fake_repo_evidence` etc.) |
| Slave-ID substitution | `jenkins_rca/agent.py` (Phase 7 block, post-validator) |
| Value validator | `jenkins_rca/value_validator.py` |
| Persistence | `jenkins_rca/audit.py:record`, `jenkins_rca/outcome_log.py:log` |
| One-shot path | `jenkins_rca/llm.py` |
| Tool schemas | `jenkins_rca/tool_schemas.py` (22 tools) |
| AWS describe (cross-acct) | `jenkins_rca/aws_tools.py` |
| GitHub commits / PRs | `jenkins_rca/github.py` |
| Jenkins MCP plugin | `jenkins_rca/jenkins_mcp.py` |
| Sanitization | `jenkins_rca/sanitize.py` |
| Window slicing | `jenkins_rca/window.py` |

---

## 7. Failure signals catalog (audit output)

Every RCA emits zero or more failure_signals. Use these to filter
audits + spot drift in production behavior.

| Signal | Layer | Meaning |
|---|---|---|
| `force_final_iter_cap` | agent loop | 25-tool-call cap fired |
| `force_final_wall_clock` | agent loop | 180s deadline hit |
| `force_final_cost_cap` | agent loop | $5 panic killswitch |
| `dup_call_warning` | agent loop | LLM re-issued identical tool call |
| `dup_call_rejected` | agent loop | 2nd+ repeat — server returned ERROR |
| `text_tool_calls_rescue` | agent loop | LLM wrote tool_calls as text in content |
| `final_json_parse_failed` | parse | final_text unparseable JSON |
| `malformed_final_schema` | schema gate | missing required keys; retry fired |
| `ultimatum_gate_triggered` | LangGraph | a gate fired and triggered re-iter |
| `compliance_status_hallucination` | LangGraph | LLM claimed status rejected but pre-fetched Jira says allowed |
| `hallucinated_file_evidence` | validator | cited file not in `files_read` set |
| `hallucinated_snippet` | validator | snippet quoted strings not in file |
| `runbook_evidence_dropped` | validator | cited docs/runbooks/X.md never fetched |
| `low_evidence_count` | validator | <3 evidence items in final RCA |
| `hallucinated_id_in_command` | validator | cmd contains `<placeholder>` |
| `tier_autobumped_terraform_restricted` | validator | tf state/apply cmd auto-bumped to restricted |
| `server_substituted_slave_id` | Phase 7 | server resolved real slave instance_id |
| `server_dropped_unresolvable_slave_cmd` | Phase 7 | slave gone; cmd dropped + ops fallback appended |
| `value_validator_substituted` | DEPRECATED | (phase 10 removed validator) |
| `file_not_found_in_chain` | tool exec | repo_read_file / github_read_file error |

---

## 8. Caches + dedup layers

| Cache | Where | TTL | Purpose |
|---|---|---|---|
| `query_emb_cache` | PG | 24h | Avoid re-embedding repeated log windows |
| `retrieval_cache` | PG | 2h | Avoid re-running vector search for retries |
| Tool-call cache | in-memory per request | request | LLM dedup — 2nd repeat warns, 3rd+ rejects |
| Repo freshness | diskcache (`/var/cache/jenkins-rca/`) | 10s | Per-request `git fetch + reset --hard` skipped if recent |
| Outcome dashboard | SQLite | forever | Operator-facing audit log |
| Audit JSONs | filesystem | forever (60d for RAG) | Past-RCA semantic memory |

---

## 9. Cost path (single RCA, agent path)

```
embed query  $0.00003   ─── RAG
PG search    ~5ms       ───
              │
              ▼
agent iter 0 input tokens (~30K)    $0.075
   tool dispatch (free)
agent iter 1 input tokens (~15K)    $0.038
agent iter 2 input tokens (~10K)    $0.025
output tokens total (~2K)           $0.030
                                    ──────
                                    ~$0.17 baseline
+
schema retry (if fired)             +$0.05
gate retry (if fired)               +$0.05 - $0.10
                                    ──────
                                    $0.20 — $0.35 typical
```

Production observed mean: **$0.30 / RCA**. Cost cap = $5 (panic
killswitch, never hit in normal traffic).

---

## 10. Deploy pipeline (how code lands in production)

```
1. laptop:          edit jenkins_rca/* or docops/* or prompts/*
2. laptop:          git commit + push to feature/jenkins-rca-agent-RAG-LANG
3. EC2 cron (2h):   bbctl-sync.sh  fires:
                    ├─ self-pull bbctl repo (reset --hard origin/<branch>)
                    ├─ pull jenkins_pipeline / InfraComposer reference repos
                    ├─ if docops/ tree SHA changed → re-index RAG docops
                    ├─ always → index-audits (cheap with dedup)
                    └─ restart jenkins-rca systemd unit
4. Service:         loads new code + reloads prompts + reads new RAG state
5. Next RCA:        uses the deployed change
```

No manual SSH-and-restart needed for code changes; the cron handles
it. For urgent fixes, paste the sync script manually on EC2 instead
of waiting for the 2h tick.

---

## 11. Reading order for a new engineer

1. **`README.md`** — what this service does, 1 page
2. **This file (`workflow.md`)** — end-to-end flow
3. **`RAG_and_LangGraph.md`** — both engines explained
4. **`bbctlrca.md`** — operational details (Jenkins integration,
   EC2 layout, secrets)
5. **`cli_commands_RAG.md`** — RAG operator cheat sheet
6. **`docops/MAP.md`** — which doc owns which concept
7. **Code, in this order:**
   - `jenkins_rca/main.py` — FastAPI entry + route dispatch
   - `jenkins_rca/classifier.py` + `classifier_rules.yml` — first regex pass
   - `jenkins_rca/agent.py` — the long file; primer + agent loop + gates
   - `jenkins_rca/gates.py` — LangGraph nodes
   - `jenkins_rca/rag.py` — RAG client + indexer
   - `jenkins_rca/tool_schemas.py` — every tool the LLM can call
   - `jenkins_rca/agent_dispatch.py` — tool-name → function map

Skip on first read: `llm.py` one-shot path (legacy fallback), 
`window.py` (log slicing), `sanitize.py` (token redaction), 
`auth.py` (HMAC webhook).
