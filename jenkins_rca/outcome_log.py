"""Per-RCA outcome logger — SQLite table for measurement-before-ship.

Each RCA append one row capturing:
  - identification (job, build, ts)
  - LLM mechanics (iters, tool_calls, tokens, cost)
  - result (error_class, failed_stage, root_cause snippet)
  - footprint (files_read, aws_apis, runbooks)
  - failure_signals — deterministic red-flag tally for the run
  - trace_path — pointer to the full trace file
  - quality / notes — empty at insert, filled later by manual review

Failure-signal events that agent.py appends as they happen:
  dup_call_warning            — tool-call cache returned 1st-repeat soft warning
  dup_call_rejected           — tool-call cache returned 2nd+ repeat ERROR
  text_tool_calls_rescue      — LLM wrote tool_calls as TEXT, rescue triggered
  voluntary_bail_rescue       — DEPRECATED (phase 10 reclassified as normal
                                JSON FINALIZE step, no longer emitted)
  force_final_cost_cap        — cost cap fired forced-final
  force_final_wall_clock      — wall-clock cap fired forced-final
  force_final_iter_cap        — MAX_TOOL_CALLS cap fired forced-final
  evidence_validator_dropped  — DEPRECATED (phase 10 removed validator)
  evidence_snippet_hallucinated — DEPRECATED (phase 10 removed validator)
  value_validator_substituted — DEPRECATED (phase 10 removed validator)
  file_not_found_in_chain     — repo_read_file/github_read_file returned error
  final_json_parse_failed     — final_text could not be parsed
  low_evidence_count          — final RCA had < 3 evidence items
  runbook_evidence_dropped    — ≥1 evidence entry cited docs/runbooks/<X>.md but
                                read_runbook(X) was never fetched (returned not-found);
                                fabricated runbook snippet dropped server-side
  hallucinated_file_evidence  — ≥1 evidence entry cited a repo file that either
                                (a) snippet-fill could not read (file/range missing)
                                or (b) the LLM never opened via repo_read_file in
                                this run. Entry dropped from evidence[]. Hard gate
                                added May 2026 after Build 15 Stagger Scaling case
                                where LLM invented `vars/discoverBlueTargetGroup.groovy`.
  hallucinated_snippet        — ≥1 evidence entry's snippet contained quoted-string
                                literals NOT present in the actual file content.
                                Entry dropped from evidence[].
  malformed_final_schema      — final JSON parsed as a dict but missing required
                                RCA keys (summary / failed_stage / error_class /
                                root_cause / evidence / suggested_fix). One
                                schema-completion retry fired; signal records the
                                degraded emission regardless of retry outcome.
                                Observed when LLM emits a single suggested_command
                                object as the entire final response.
  ultimatum_gate_triggered    — Phase 4 gate fired. Either the runbook for a
                                MANDATORY_RUNBOOK_CLASSES class was never fetched,
                                OR the class is jenkins_agent_offline and the
                                Action block lacks PRIMARY/SECONDARY split. One
                                tool+finalize retry pair fired (~$0.10 worst
                                case); signal records the gate trigger
                                regardless of whether the retry produced a
                                conforming RCA.
  compliance_status_hallucination — Final RCA claimed class=compliance and
                                Action/root_cause asserted the Jira ticket status
                                is rejected ("not in allowed list" / "is not
                                acceptable" / "must be one of"), BUT the
                                pre-fetched `jira.tickets` block in the primer
                                shows the actual status IS one of the allowed
                                values (READY FOR RELEASE / HOT FIX). ULTIMATUM
                                gate fired with explicit "re-read pre-fetched
                                state + re-classify" instruction. Stagger Prod+1
                                build 5225 case (MPB-1279 READY FOR RELEASE,
                                Gradle daemon crashed in Build stage, classifier
                                misrouted to compliance due to info banners).
  server_substituted_slave_id — Phase 7 auto-substitution: log mentioned
                                slave-N AND a suggested_command had
                                `<slave-instance-id>` placeholder; server
                                called jenkins_node_info(slave) + substituted
                                the real instance_id into every offending cmd.
                                Rationale field annotated with what was
                                substituted so the operator sees provenance.
  server_dropped_unresolvable_slave_cmd — Same path but
                                jenkins_node_info returned no instance_id
                                (Jenkins node API down OR slave not registered
                                anymore). The placeholder cmd was DROPPED from
                                suggested_commands entirely — better to ship
                                fewer commands than ship an unusable one.

Queryable via jenkins_rca/cli_outcomes.py.

Storage: /var/cache/jenkins-rca/outcomes.sqlite (single file, alongside
diskcache.db). Schema is auto-created on first write. Safe to delete
+ re-init at any time.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


CACHE_DIR = Path(os.environ.get("JENKINS_RCA_CACHE_DIR", "/var/cache/jenkins-rca"))
DB_PATH = CACHE_DIR / "outcomes.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    job             TEXT    NOT NULL,
    build           INTEGER NOT NULL,
    service         TEXT,
    model           TEXT,
    error_class     TEXT,
    failed_stage    TEXT,
    iters           INTEGER,
    tool_calls      INTEGER,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    root_cause      TEXT,
    files_read      TEXT,        -- JSON array
    aws_apis        TEXT,        -- JSON array
    runbooks        TEXT,        -- JSON array
    failure_signals TEXT,        -- JSON array
    trace_path      TEXT,
    quality         TEXT,        -- correct|partial|wrong|null
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcomes_ts          ON outcomes(ts);
CREATE INDEX IF NOT EXISTS idx_outcomes_class       ON outcomes(error_class);
CREATE INDEX IF NOT EXISTS idx_outcomes_quality     ON outcomes(quality);
"""


def _conn() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.executescript(_SCHEMA)
    return conn


def log(
    job: str,
    build: int,
    service: str | None,
    model: str | None,
    rca: dict,
    iters: int,
    tool_calls: int,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    files_read: list[str],
    aws_apis: list[str],
    runbooks: list[str],
    failure_signals: list[str],
    trace_path: str | None,
) -> None:
    """Insert one outcome row. Best-effort — never raises."""
    try:
        conn = _conn()
        conn.execute(
            """
            INSERT INTO outcomes (
                ts, job, build, service, model, error_class, failed_stage,
                iters, tool_calls, tokens_in, tokens_out, cost_usd,
                root_cause, files_read, aws_apis, runbooks,
                failure_signals, trace_path, quality, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                int(time.time()), job, int(build), service, model,
                (rca.get("error_class") or "")[:80],
                (rca.get("failed_stage") or "")[:120],
                iters, tool_calls, tokens_in, tokens_out, round(cost_usd, 4),
                (rca.get("root_cause") or "")[:800],
                json.dumps(files_read)[:4000],
                json.dumps(aws_apis)[:2000],
                json.dumps(runbooks)[:1000],
                json.dumps(failure_signals)[:1000],
                trace_path or "",
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Never let logging break an RCA. Print to stderr; move on.
        print(f"[outcome_log] insert failed: {e}", file=sys.stderr, flush=True)
