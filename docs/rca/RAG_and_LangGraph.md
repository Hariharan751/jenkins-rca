# RAG + LangGraph — design + runtime guide

The jenkins-rca service combines two engines:

- **RAG** (Retrieval-Augmented Generation) — Postgres + pgvector
  semantic index over runbooks, job_flows, docs, and past audit
  JSONs. Surfaces "we've seen this before, here's what fixed it"
  context to the LLM at iter 0 of every RCA.
- **LangGraph** — declarative state graph that runs after the LLM
  emits final JSON. Five gate nodes enforce schema completeness,
  mandatory-runbook fetch, jenkins_agent_offline PRIMARY/SECONDARY
  framing, and compliance-status anti-hallucination. Gates can
  trigger a single corrective re-iter to fix issues without an
  operator round-trip.

This doc covers both — what they are, how they're wired, how to
operate them, and the cost model.

---

## 1. Two-line architecture

```
Jenkins build fails
  ↓ webhook
classifier (regex) → primer (jira + jenkins.node + RAG inject)
  ↓                                                ↑
agent loop (OpenAI function-calling + tools) ←─────┘
  ↓ final JSON
LangGraph gates (schema / runbook / framing / compliance / reiter)
  ↓
validator hard gates (drop _error / non-files_read / hallucinated snippet / placeholder cmd)
  ↓
server-side slave-ID substitution (jenkins_node_info fallback)
  ↓
final JSON → audit/<request_id>.json on disk → response
```

---

## 2. RAG layer

### 2a. What's indexed

| `source_type` | What | Count |
|---|---|---|
| `runbook`    | `docops/runbooks/*.md` — one per error class | ~14 |
| `job_flow`   | `docops/job_flows/*.md` — one per pipeline   | ~9  |
| `doc`        | `docops/*.md` — MAP.md + golden index        | ~2  |
| `audit`      | `/var/log/jenkins-rca/<request_id>.json` — past RCAs | grows |

### 2b. Storage

Postgres 16 + pgvector (HNSW, cosine). Three tables:

- `rca_chunks` — `id`, `source_type`, `source_id`, `chunk_idx`,
  `chunk_text`, `embedding VECTOR(1536)`, `meta JSONB`,
  `content_hash`. HNSW index on `embedding` for fast NN; GIN on
  `meta` for filter-before-rank.
- `query_emb_cache` — `query_hash → embedding`, 24h TTL. Skips
  re-embedding repeat log windows.
- `retrieval_cache` — `query_hash → top_k_ids`, 2h TTL. Skips
  re-running the vector search for back-to-back retries.

### 2c. Embeddings

OpenAI `text-embedding-3-small` (1536-dim, $0.02/1M tokens).
Unit-norm output → cosine similarity is correct. Switch model via
`JENKINS_RCA_EMBED_MODEL` env (changes schema dim — needs migration).

### 2d. Chunking

Markdown-aware: H2 splits, 2000-char ceiling per chunk, 200-char
overlap between adjacent chunks. Skips frontmatter. Content-hash
dedup — re-indexing the same file is a no-op.

### 2e. Auto-inject at iter 0

Both the agent path (`jenkins_rca/agent.py:_rag_inject_for_agent`) and
the one-shot path (`jenkins_rca/llm.py`) auto-inject top-k results
into the prompt as a `## retrieved.rag` block:

```
## retrieved.rag (top-k matches — CANDIDATES, verify via
read_runbook / read_doc before citing in evidence)
- [0.812] runbook/runbooks/jenkins_agent_offline.md
    The Jenkins build agent (a slave node, e.g. `slave-4`) lost
    connectivity to the Jenkins master mid-step. ...
- [0.708] audit/0adc29e4-01ac-43de-a67f-df31106acd29.json
    summary: Gradle build daemon disappeared during the Build stage,
    indicating a JVM OOM issue. ...
- ...
```

Selectivity tuning (R3.1):
- **Known class**: pull from `runbook` + `audit` sources only.
  Agent path adds runbook here because it doesn't get the runbook
  via CLASS_DOCS like one-shot does.
- **Unknown class**: pull from all three (runbook + doc + audit).

Query anchoring (R3.1): the search query is the last `Error:` /
`Exception:` / `FAIL:` / `Caused by:` line + 500 chars of context,
NOT the whole 30K-char log window. text-embedding-3-small's
precision drops quickly as the query gets noisier, so a focused
slice gives better top-k.

### 2f. Past-RCA semantic memory

Every RCA produces an audit JSON at `/var/log/jenkins-rca/<request_id>
.json` via `jenkins_rca/audit.py:record`. The 2-hour cron (`infra/
scripts/bbctl-sync.sh`) calls `python -m jenkins_rca.rag index-audits`
which embeds each audit's `summary` + `root_cause` +
`suggested_commands` as a chunk with `source_type=audit`.

Future RCAs hitting similar errors get those past audits as top
hits — typical score 0.70-0.72 for matching slave-bounce / Gradle
crash cases, well above runbook hits (0.40-0.60).

Stale records (>60 days, override via `JENKINS_RCA_RAG_AUDIT_MAX_DAYS`)
are skipped to keep the corpus relevant.

### 2g. CLI

```bash
python -m jenkins_rca.rag index-docops   # re-index docops/
python -m jenkins_rca.rag index-audits   # re-index /var/log/jenkins-rca/
python -m jenkins_rca.rag search '<query>' [k]  # semantic search
python -m jenkins_rca.rag diag           # 6-stage health check + smoke
python -m jenkins_rca.rag reset --yes    # DELETE all chunks + caches
python -m jenkins_rca.rag embed '<text>' # smoke single embed
```

Auto-loads secrets from AWS Secrets Manager if `JENKINS_RCA_PG_PASSWORD`
not in env. See `cli_commands_RAG.md` for full operator playbook.

---

## 3. LangGraph layer

### 3a. Why graphs, not nested if/else

The ULTIMATUM gate stack has 4 checks (schema-complete /
runbook-fetched / primary-secondary / compliance-hallucination) +
1 shared corrective-reiter node. Imperative if/elif chains across
~200 lines of `agent.py` were hard to extend (adding gate #5 meant
threading a new branch through every existing check).

LangGraph turns each gate into a node + each "if condition,
re-iter" into a conditional edge. Adding gate #N+1 = `add_node` +
`add_conditional_edges`. Zero churn on existing gates.

### 3b. Graph shape

```
START
  ↓
check_schema (missing required keys? → backfill defaults)
  ↓
check_runbook_fetched (mandatory class + runbook not in read_runbooks?)
  ↓ trigger ──→ corrective_reiter
  ↓                    ↓
check_primary_secondary (jenkins_agent_offline + missing PRIMARY/SECONDARY?)
  ↓ trigger ──→ corrective_reiter
  ↓                    ↓
check_compliance_hallucination (compliance + status claim contradicts pre-fetched jira?)
  ↓ trigger ──→ corrective_reiter
  ↓
END

corrective_reiter loops back to check_runbook_fetched when
retry_budget > 0, else routes to END (caps total node visits at
recursion_limit=25).
```

### 3c. State schema

`UltimatumState` TypedDict, threaded through every node:

| Field             | Purpose                                                       |
|---|---|
| `rca`             | Parsed RCA dict — mutated by nodes (backfill, retry result)   |
| `error_class`     | Classifier-emitted class                                      |
| `read_runbooks`   | Set of runbook names fetched during the iter loop             |
| `messages`        | OpenAI message history (mutated when gates append corrections)|
| `client`          | OpenAI client for retry calls                                 |
| `model`           | Model id (default `gpt-4o`)                                   |
| `tools`           | Full function-calling tool list (TOOLS)                       |
| `tool_dispatch`   | Async callable (name, args) → result_str — owned by agent.py  |
| `build_meta`      | For failed_stage default backfill                             |
| `failure_signals` | Accumulated signals (drives audit + dashboard)                |
| `retry_budget`    | Remaining gate-triggered retries (default 1, ~$0.10 each)     |
| `tokens_in/out`   | Accumulated for cost tracking                                 |
| `tool_call_count` | Running counter incremented per dispatched call               |

### 3d. The 4 gate nodes

1. **`_check_schema`** — verifies the parsed RCA has all required
   keys (summary, failed_stage, error_class, root_cause, evidence,
   suggested_fix). Backfills safe defaults so downstream gates see
   a complete object. Emits `malformed_final_schema`.

2. **`_check_runbook_fetched`** — for classes in
   `MANDATORY_RUNBOOK_CLASSES` (17 of 18 classifier classes), verify
   the LLM called `read_runbook(class)` during the iter loop. If
   not, set `_gate_reason` so the corrective node forces a
   re-iter where the runbook content is in scope.

3. **`_check_primary_secondary`** — `jenkins_agent_offline` only.
   Verifies the Action block contains both `PRIMARY` and `SECONDARY`
   tokens. A code-only fix won't prevent the next slave bounce; an
   infra-only fix won't prevent the next NotSerializableException.
   Runbook template requires both.

4. **`_check_compliance_hallucination`** — compliance only. Detects
   the pattern "status X not in allowed list" + cross-checks against
   pre-fetched `jira.tickets` showing X IS in the allowed list.
   Forces re-classification via runbook + log inspection. Catches
   the Stagger Prod+1 build 5225 Gradle-misclassified-as-compliance
   regression.

### 3e. The corrective_reiter node

Shared correction path. When any gate sets `_gate_reason`:

1. Append the reason to `messages` as a user-role correction
2. Run one tool-using LLM iter (LLM can call `read_runbook` etc.)
3. Dispatch every tool call via `state["tool_dispatch"]`, feed
   results back into messages
4. Force-final-JSON with `response_format=json_object`
5. Parse + replace `state["rca"]` on success
6. Decrement `retry_budget`; if 0, future gate triggers route the
   graph to END instead of looping

Cost: one retry pair ≈ $0.10 worst case.

### 3f. Lazy import + degrade contract

`jenkins_rca/gates.py:build_gate_graph()` lazy-imports langgraph. If
the package isn't installed, returns None and `agent.py` falls back
to the imperative gate stack (preserved in the same file for that
reason). The RAG branch — which doesn't have langgraph — runs the
same logic with no behavior change. This lets the RAG branch stay
as a fallback target if the LangGraph path ever regresses.

### 3g. CLI smoke

```bash
python -m jenkins_rca.gates
# → "langgraph available, gate graph compiles"
# → nodes: check_schema → check_runbook_fetched →
#          check_primary_secondary → check_compliance_hallucination → END
# → mandatory runbook classes: [...17 classes...]
```

---

## 4. How they chain together

End-to-end sequence for a single RCA request:

```
1. POST /v1/rca {"job": "...", "build": N, "deep": true}
   │
2. classifier (jenkins_rca/classifier.py)
   │   regex over log_window → error_class hint
   │
3. _build_primer (agent.py)
   │   async parallel:
   │   ├─ service.lookup(service)           [sync, ~ms]
   │   ├─ jira_prefetch_for_agent(log)      [API, ~500ms when key in log]
   │   ├─ jenkins_node_prefetch_for_agent   [API, ~200ms when slave-N in log]
   │   └─ rag_inject_for_agent(log, class)  [embed + PG vector search, ~300ms]
   │   ↓
   │   System message = `rca_common.md` + `rca_agent_system.md` + primer blocks
   │
4. agent loop (agent.py:run_agent)
   │   iter 0: LLM emits parallel tool calls (read_runbook, repo_read_file,
   │           list_job_flows, aws_describe, jira_get_ticket, etc.)
   │   iter 1+: drill into helpers based on iter 0 results
   │   target ≤ 3 iters total
   │   ↓
   │   final JSON
   │
5. schema-completion gate (one retry if schema incomplete)
   │
6. LangGraph ULTIMATUM gates (gates.py:build_gate_graph)
   │   check_schema → check_runbook_fetched → check_primary_secondary →
   │   check_compliance_hallucination → END
   │   any gate that trips → corrective_reiter (one retry pair) → loop back
   │
7. Validator hard gates (agent.py)
   │   _fill_repo_snippets → drop _error → _filter_fake_repo_evidence →
   │   _filter_hallucinated_snippets → _drop_unfetched_runbook_evidence
   │
8. Server-side slave-ID substitution (Phase 7)
   │   If log mentions slave-N AND any cmd has `<slave-instance-id>`:
   │     try jenkins_node_info(slave-N)
   │     → real id  → substitute into every cmd
   │     → no id    → drop cmd + append ops-contact fallback
   │
9. value_validator (port 8080 / /admin/version / gps.log substitutions)
   │
10. audit.record(payload) → /var/log/jenkins-rca/<request_id>.json
    │
11. outcome_log.log(...) → /var/cache/jenkins-rca/outcomes.sqlite
    │
12. Response → operator
```

---

## 5. Class coverage (May 2026)

| Class                     | Classifier | Runbook | AGENT_CLASSES | MANDATORY_RUNBOOK |
|---|---|---|---|---|
| compliance                | ✓ | ✓ | ✓ | ✓ |
| build_tool_crash          | ✓ | ✓ | ✓ | ✓ |
| canary_fail               | ✓ | ✓ | ✓ | ✓ |
| canary_script_error       | ✓ | ✓ | ✓ | ✓ |
| health_check              | ✓ | ✓ | ✓ | ✓ |
| terraform                 | ✓ | ✓ | ✓ | ✓ |
| stale_tf_state            | ✓ | ✓ | ✓ | ✓ |
| aws_limit                 | ✓ | ✓ | ✓ | ✓ |
| config_validation         | ✓ | ✓ | ✓ | ✓ |
| jenkins_agent_offline     | ✓ | ✓ | ✓ | ✓ |
| java_runtime              | ✓ | ✓ | ✓ | ✓ |
| scm                       | ✓ | ✓ | ✓ | ✓ |
| parse_error               | ✓ | ✓ | ✓ | ✓ |
| timeout                   | ✓ | ✓ | ✓ | ✓ |
| network                   | ✓ | ✓ | ✓ | ✓ |
| dependency                | ✓ | ✓ | ✓ | ✓ |
| ssm                       | ✓ | ✓ | ✓ | ✓ |
| unknown                   | ✓ | ✓ | — | — |

17/18 classes route through the agent + LangGraph gate path; only
`unknown` stays in the one-shot fallback by design (no specific
runbook to enforce).

---

## 6. Cost model

| Operation                            | Approx cost          |
|---|---|
| Embed one query (anchor slice)       | $0.00003 (negligible)|
| Embed one chunk (re-index)           | $0.00010             |
| Re-index full docops corpus          | $0.07 (~230 chunks)  |
| Re-index 100 new audits              | $0.10                |
| One RCA — agent path, gates pass     | $0.20 — $0.30        |
| One RCA — one gate triggered retry   | +$0.05 — $0.10       |
| One RCA — schema-completion retry    | +$0.05               |
| Vector search (HNSW)                 | ~5ms                 |
| Postgres total disk footprint        | ~50MB for ~500 chunks|

text-embedding-3-small is the cost floor — most budget goes to
gpt-4o tokens for the agent loop itself.

---

## 7. Operating procedures

### 7a. Deploy a docops/ change

```
# laptop
git add docops/runbooks/<class>.md
git commit -am "docs(<class>): <change>"
git push

# next cron tick (≤ 2h) on EC2 — automatic:
#   bbctl-sync.sh self-pulls bbctl repo
#   detects docops/ tree SHA changed → re-indexes RAG
#   restarts jenkins-rca

# OR force immediately on EC2:
sudo bash /home/ubuntu/project/Jenkins-rca/infra/scripts/bbctl-sync.sh
```

### 7b. Force a fresh RCA (bypass dedup cache)

```
# wipe DB-level retrieval cache (24h query + 2h retrieval)
python -m jenkins_rca.rag reset --yes
python -m jenkins_rca.rag index-docops
python -m jenkins_rca.rag index-audits
```

### 7c. Add a new gate

1. Write `_check_<name>(state)` in `gates.py` (returns `{}` or
   `{"failure_signals": [...], "_gate_reason": "..."}`)
2. Add `g.add_node("<name>", _check_<name>)` in `build_gate_graph`
3. Add `g.add_conditional_edges("<name>", _route_after_gate, {...})`
4. Insert into the linear chain at the right position
5. Update `jenkins_rca/outcome_log.py` failure_signals vocab

### 7d. Add a new error class

1. New regex rule in `classifier_rules.yml` — place BEFORE generic
   classes (java_runtime, unknown)
2. New runbook at `docops/runbooks/<class>.md` — follow template
   shape (what-this-means / detect-signals / drill-plan / Action
   template / common pitfalls / output schema)
3. Add to `jenkins_rca/main.py:AGENT_CLASSES` set
4. Add to `MANDATORY_RUNBOOK_CLASSES` in both `jenkins_rca/agent.py`
   AND `jenkins_rca/gates.py` (keep them in sync)
5. Re-index: `python -m jenkins_rca.rag index-docops`

### 7e. Investigate "gate fired too often" alerts

```
# All RCAs in last 24h that hit ultimatum_gate_triggered
sqlite3 /var/cache/jenkins-rca/outcomes.sqlite "
  SELECT ts, job, build, error_class, failure_signals
  FROM outcomes
  WHERE failure_signals LIKE '%ultimatum_gate%'
    AND ts > strftime('%s','now') - 86400
  ORDER BY ts DESC LIMIT 20;
"

# If a specific class trips often, the runbook + prompt rule probably
# need clearer enforcement language. Edit the runbook + re-run the
# affected build to verify the gate doesn't trip.
```

---

## 8. Why this architecture (design notes)

### Custom OpenAI loop, not full LangChain

LangChain's higher-level `AgentExecutor` abstraction takes over tool
dispatch, retry semantics, and prompt caching — features we already
have in `agent.py` with earned recovery logic (tool dedup,
cost/wall/iter caps, text-tool-calls rescue, JSON FINALIZE retry,
chain-walk verification). Rewriting 1500+ lines of recovery code
as LangChain runnables would risk regression for zero behavior gain.

LangGraph is used ONLY for the post-parse gate stack where the
declarative-graph shape genuinely helps (4+ gates with conditional
re-iter routing). Everything else stays in the custom loop.

### RAG, not fine-tuned LLM

Runbooks change frequently (new pipeline failures, new pitfalls).
Fine-tuning a base model takes hours and locks in stale doctrine.
RAG re-indexes in 30 seconds on every change, no model retrain
needed. text-embedding-3-small is cheap enough that re-indexing
daily is trivial.

### Postgres, not Pinecone / Weaviate

pgvector lives in the same Postgres we already operate. No new
service, no new credentials path, no new backup workflow. HNSW
recall is identical to Pinecone for k≤10. We have ~230 chunks; an
external vector DB would be massively overprovisioned.

### Past-RCA audit as RAG source

Operators kept asking "wait, didn't we see this exact failure last
month?" — answer was always "let me grep journalctl". Audit
indexing replaces the grep with semantic search across every past
RCA. When a Gradle daemon crash recurs, the LLM gets the 3 most
similar past RCAs in primer + can quote their fixes directly.

### Server-side slave-ID substitution

The LLM kept emitting `<slave-instance-id>` despite the prompt rule
+ the jenkins_node_info tool being available. Belt-and-suspenders:
prompt + server gate. Three layers — primer pre-fetch, prompt
MANDATORY rule, server-side substitution-or-drop-or-fallback.
Operator never sees a `<placeholder>` again.

---

## 9. Roadmap

Already shipped (Phases 1-7):
- ✓ Phase 1: docops dedup (kill duplicate runbooks)
- ✓ Phase 2: `rca_common.md` extract shared prompt rules
- ✓ Phase 3: RAG auto-inject in agent path
- ✓ Phase 4: ULTIMATUM gate (imperative)
- ✓ Phase 5: docops/MAP.md ownership matrix
- ✓ Phase 6: LangGraph gates migration
- ✓ Phase 7: 4 new runbooks + real-ID derivation + audit RAG

Deferred:
- LangGraph Postgres checkpointer (resume RCA after restart)
- LangSmith tracing (visual graph + per-node debug)
- Self-eval judge gate (gpt-4o-mini "does Action match template?")
- Hybrid BM25 + vector search (pgvector + pg_trgm)
- Streaming intermediate state to UI (LangGraph astream_events)
- Async POST → request_id → GET polling
- Eval harness (10 golden builds, nightly regression)
- Parallel `unknown` class subgraph (fan-out to rag + repo +
  recent_commits)

---

## 10. Files

| Path | Purpose |
|---|---|
| `jenkins_rca/rag.py`        | RAG client + indexer + CLI                   |
| `jenkins_rca/rag_schema.sql`| Postgres + pgvector schema                   |
| `jenkins_rca/gates.py`      | LangGraph StateGraph + 4 gate nodes          |
| `jenkins_rca/agent.py`      | OpenAI function-calling loop + validator gates|
| `jenkins_rca/llm.py`        | One-shot path (Gemini + OpenAI)              |
| `jenkins_rca/audit.py`      | RCA persistence (JSON per request_id)        |
| `jenkins_rca/outcome_log.py`| SQLite outcome table + failure_signals vocab |
| `infra/scripts/bbctl-sync.sh` | Cron: git pull + RAG re-index + restart  |
| `infra/scripts/rag-postgres-install.sh` | One-shot PG + pgvector setup   |
| `prompts/rca_common.md`   | Shared rules (override signals, evidence, BBCTL) |
| `prompts/rca_agent_system.md` | Agent-path method (drill, narration, iter) |
| `prompts/rca_system.md`   | One-shot-path method (compliance modes, canary) |
| `docops/runbooks/`        | 18 class drill plans                          |
| `docops/job_flows/`       | 9 per-pipeline orientation docs               |
| `docops/MAP.md`           | Ownership matrix                              |
| `docops/jenkins_pipelines_golden.md` | Cross-pipeline index                |
