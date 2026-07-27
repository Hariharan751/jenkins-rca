"""ULTIMATUM gate orchestration via LangGraph (Phase 6).

Branch-only feature on `feature/jenkins-rca-agent-RAG-LANG`. Replaces the
nested if/elif gate stack in `agent.py` (lines ~800-1100 on the RAG
branch) with a declarative state graph. Each gate becomes a node;
conditional edges route between them.

Why a graph here:
  - The gate stack has 5+ checks today (schema-complete, runbook-fetched,
    primary/secondary framing for jenkins_agent_offline, compliance
    hallucination, evidence-validator). Adding gate #6 imperatively
    means another `if _ultimatum_reason is None:` branch.
  - Conditional edges + a single `END` sink make the routing explicit.
  - Future gates land as `add_node` + `add_conditional_edges` — no
    touching the surrounding control flow.

What stays in agent.py (NOT migrated):
  - The OpenAI function-calling iter loop (tool dedup, cost/wall caps,
    text-tool-calls rescue, JSON FINALIZE retry, chain-walk verification
    injection). Those aren't state-machine patterns; they're tight
    recovery code in the hot path.
  - RAG auto-inject in `_build_primer` (pre-loop, no state).
  - The validator drops (`_filter_fake_repo_evidence`,
    `_filter_hallucinated_snippets`). Those run AFTER all gates as a
    final integrity pass — wrapping them in a graph node adds nothing.

Lazy import contract: this module imports `langgraph` lazily; if not
installed, `build_gate_graph()` returns `None` and the caller in
agent.py falls back to the imperative gate stack from the RAG branch.
Lets the RAG-only branch still work — gates.py is additive infra.

Usage from agent.py:
    from . import gates as ultimatum_gates
    graph = ultimatum_gates.build_gate_graph()
    if graph is None:
        # langgraph not installed → use the imperative stack
        ...legacy_gate_chain(rca, ...)
    else:
        state = ultimatum_gates.UltimatumState(
            rca=rca, error_class=error_class,
            read_runbooks=read_runbooks, messages=messages,
            client=client, model=model, ...,
        )
        out = graph.invoke(state)
        rca = out["rca"]
        failure_signals.extend(out.get("failure_signals", []))
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

try:
    from langgraph.graph import StateGraph, END
    from typing_extensions import TypedDict
    _HAS_LANGGRAPH = True
except Exception:
    StateGraph = None  # type: ignore
    END = "__end__"   # sentinel
    TypedDict = dict  # type: ignore
    _HAS_LANGGRAPH = False


# Same set as agent.py — keep them in sync. Future: move this to
# `jenkins_rca/constants.py` and import from there.
MANDATORY_RUNBOOK_CLASSES = {
    "jenkins_agent_offline",
    "canary_fail",
    "canary_script_error",
    "health_check",
    "compliance",
    "terraform",
    "stale_tf_state",
    "aws_limit",
    "config_validation",
    "build_tool_crash",
    # Phase-7: runbooks added for these classes; enforce parity with
    # the rest of the mandatory set.
    "scm",
    "parse_error",
    "java_runtime",
    "timeout",
    "network",
    "dependency",
    "ssm",
}

_RCA_REQUIRED_KEYS = (
    "summary", "failed_stage", "error_class", "root_cause",
    "evidence", "suggested_fix",
)


# ─── State schema ─────────────────────────────────────────────────────

if _HAS_LANGGRAPH:
    class UltimatumState(TypedDict, total=False):  # type: ignore[misc]
        """State threaded through the gate graph.

        rca              — parsed RCA dict (mutated in-place by nodes)
        error_class      — classifier-emitted class (string)
        read_runbooks    — set of runbook names fetched during the loop
        messages         — OpenAI message history (mutated when gates
                           append correction messages + retry result)
        client           — OpenAI client for retry calls
        model            — model id (e.g. "gpt-4o")
        tools            — function-calling tools list so the
                           corrective-reiter iter can let the LLM call
                           read_runbook etc. before finalizing
        tool_dispatch    — async callable (name, args) → result_str.
                           Owned by agent.py; passed in so gates.py
                           stays independent of dispatcher internals.
        build_meta       — for failed_stage default backfill
        failure_signals  — accumulated signals to log
        retry_budget     — remaining gate-triggered retries (cost cap)
        tokens_in        — accumulated input tokens
        tokens_out       — accumulated output tokens
        tool_call_count  — running counter (incremented on each dispatch)
        """
        rca:             dict
        error_class:     str
        read_runbooks:   set
        messages:        list
        client:          Any
        model:           str
        tools:           list
        tool_dispatch:   Any
        build_meta:      dict
        failure_signals: list
        retry_budget:    int
        tokens_in:       int
        tokens_out:      int
        tool_call_count: int


# ─── Node implementations ─────────────────────────────────────────────
#
# Each node MUST take the full state, MUST return a dict of fields to
# update. Returning {} means "no changes". Returning {"failure_signals":
# [...]} APPENDS to the existing list (LangGraph merges with operator.add
# when annotated; here we manage it manually since dict merge replaces).

def _check_schema(state: dict) -> dict:
    """Gate 1 — final JSON has all required RCA keys.

    If missing, backfill defaults so downstream gates see a valid
    object. (The schema-completion RETRY itself stays in agent.py as
    a one-shot LLM call before reaching the gate graph; this node only
    verifies + backfills.)
    """
    rca = state["rca"]
    missing = [k for k in _RCA_REQUIRED_KEYS if k not in rca]
    if not missing:
        return {}
    sigs = state.get("failure_signals", [])[:] + ["malformed_final_schema"]
    bm = state.get("build_meta") or {}
    defaults = {
        "summary":      "Agent did not emit a summary.",
        "failed_stage": bm.get("detected_failed_stage", "—"),
        "error_class":  state.get("error_class") or "unknown",
        "root_cause":   "Agent did not emit root_cause prose.",
        "evidence":     [],
        "suggested_fix": {
            "Finding": "Agent did not emit a structured fix.",
            "Action":  "Re-run with deep:true or inspect agent stderr logs.",
            "Verify":  "",
        },
    }
    for k, v in defaults.items():
        if k not in rca:
            rca[k] = v
    if "suggested_commands" not in rca:
        rca["suggested_commands"] = []
    return {"rca": rca, "failure_signals": sigs}


def _check_runbook_fetched(state: dict) -> dict:
    """Gate 2 — mandatory runbook fetched for class.

    For classes in `MANDATORY_RUNBOOK_CLASSES`, require that
    `read_runbook('<class>')` was called during the loop. If not, the
    edge router will send the state through `_corrective_reiter` with
    a runbook-fetch correction message before re-emitting.
    """
    cls = state["rca"].get("error_class") or state.get("error_class")
    if cls in MANDATORY_RUNBOOK_CLASSES and cls not in state.get("read_runbooks", set()):
        sigs = state.get("failure_signals", [])[:] + ["ultimatum_gate_triggered"]
        return {
            "failure_signals": sigs,
            "_gate_reason": (
                f"You did not read the runbook for `{cls}`. Call "
                f"`read_runbook('{cls}')` now, then re-emit the FULL "
                f"RCA JSON applying its drill plan + action template. "
                f"The runbook is the authoritative recipe for this "
                f"class — without it your Action block is missing the "
                f"structure operators depend on."
            ),
        }
    return {}


def _check_primary_secondary(state: dict) -> dict:
    """Gate 3 — jenkins_agent_offline Action must split PRIMARY/SECONDARY.

    A code-only fix won't prevent the next slave bounce; an infra-only
    fix won't prevent the next NotSerializableException. Both required.
    """
    cls = state["rca"].get("error_class") or state.get("error_class")
    if cls != "jenkins_agent_offline":
        return {}
    sf = state["rca"].get("suggested_fix") or {}
    action = ""
    if isinstance(sf, dict):
        action = (sf.get("Action") or "")
    elif isinstance(sf, str):
        action = sf
    upper = action.upper()
    if "PRIMARY" in upper and "SECONDARY" in upper:
        return {}
    sigs = state.get("failure_signals", [])[:] + ["ultimatum_gate_triggered"]
    return {
        "failure_signals": sigs,
        "_gate_reason": (
            "Class is `jenkins_agent_offline` but your Action block "
            "does not split into PRIMARY (agent health: investigate "
            "the slave that bounced, restart agent, check infra) and "
            "SECONDARY (pipeline-code hardening: refactor the helper "
            "to drop non-Serializable retained objects). Both are "
            "required by the runbook template. Re-emit the FULL RCA "
            "JSON with both PRIMARY and SECONDARY sections in Action."
        ),
    }


def _check_compliance_hallucination(state: dict) -> dict:
    """Gate 4 — compliance class must agree with pre-fetched Jira state.

    If the LLM's Action says "status not in allowed list" while the
    primer's jira.tickets block shows the status IS in the allowed
    set {READY FOR RELEASE, HOT FIX}, force a re-read + re-classify.
    """
    cls = state["rca"].get("error_class") or state.get("error_class")
    if cls != "compliance":
        return {}
    sf = state["rca"].get("suggested_fix") or {}
    root = state["rca"].get("root_cause") or ""
    action = (sf.get("Action") or "") if isinstance(sf, dict) else (
        sf if isinstance(sf, str) else "")
    combined = (root + "\n" + action).lower()
    suspect = any(p in combined for p in [
        "not in the allowed list", "not in allowed list",
        "is not acceptable", "is not in", "not in [",
        "must be one of",
    ])
    if not suspect:
        return {}
    # Look in the messages history for jira.tickets with an allowed status
    jira_ok_status = False
    for m in state.get("messages", []) or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content") or ""
        if not isinstance(content, str):
            continue
        if "jira.tickets" in content and (
            '"READY FOR RELEASE"' in content or '"HOT FIX"' in content or
            "'READY FOR RELEASE'" in content or "'HOT FIX'" in content
        ):
            jira_ok_status = True
            break
    if not jira_ok_status:
        return {}
    sigs = state.get("failure_signals", [])[:] + ["compliance_status_hallucination"]
    return {
        "failure_signals": sigs,
        "_gate_reason": (
            "Your Action / root_cause says the Jira ticket status is "
            "NOT in the allowed list — but the pre-fetched "
            "`jira.tickets` block in the primer shows the ticket "
            "status IS one of {READY FOR RELEASE, HOT FIX} (which "
            "ARE the allowed values per the compliance runbook Mode "
            "3). Re-read the `## jira.tickets` JSON block in the "
            "system message, find the actual `status` field value, "
            "and re-emit the RCA. The classifier hint was "
            "`compliance` because the log contains `Compliance:` "
            "info banners — those are POSITIVE status messages "
            "(passing builds emit them), NOT failure signals. Look "
            "at the BOTTOM of the log for the actual fatal line and "
            "re-classify. Most common true cause when compliance "
            "passed: `build_tool_crash`, `dependency`, "
            "`java_runtime`, or `unknown`."
        ),
    }


def _corrective_reiter(state: dict) -> dict:
    """Shared correction node — fires the gate-triggered retry pair.

    Appends `_gate_reason` to the message history, runs ONE tool-using
    iter (LLM may call read_runbook etc.), dispatches any tool calls
    via `state["tool_dispatch"]`, feeds results back, then forces a
    final-JSON emission. Updates state["rca"] with the parsed retry
    output; leaves the old RCA in place on any error.

    Honors `retry_budget` — skips when 0 to bound cost (~$0.10/pair).
    """
    import asyncio  # local import (node may run in a fresh thread)
    reason = state.get("_gate_reason")
    budget = state.get("retry_budget", 0)
    if not reason or budget <= 0:
        return {"_gate_reason": None}
    messages = (state.get("messages") or [])[:]
    client = state.get("client")
    model = state.get("model")
    tools = state.get("tools") or []
    dispatch = state.get("tool_dispatch")
    if client is None or model is None:
        return {"_gate_reason": None}

    messages.append({"role": "user", "content": reason})
    out: dict[str, Any] = {
        "retry_budget":    budget - 1,
        "_gate_reason":    None,
        "tokens_in":       state.get("tokens_in", 0),
        "tokens_out":      state.get("tokens_out", 0),
        "tool_call_count": state.get("tool_call_count", 0),
        "read_runbooks":   set(state.get("read_runbooks") or set()),
    }
    try:
        # Tool iter (LLM may call read_runbook etc.). Only pass tools
        # when both tools + dispatcher are available — otherwise just
        # ask for content.
        kwargs: dict[str, Any] = {
            "model": model, "messages": messages, "temperature": 0.1,
        }
        if tools and dispatch is not None:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        retry = client.chat.completions.create(**kwargs)
        out["tokens_in"]  += retry.usage.prompt_tokens
        out["tokens_out"] += retry.usage.completion_tokens
        retry_msg = retry.choices[0].message

        if retry_msg.tool_calls and dispatch is not None:
            messages.append({
                "role":     "assistant",
                "content":  retry_msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments}}
                    for tc in retry_msg.tool_calls
                ],
            })
            for tc in retry_msg.tool_calls:
                out["tool_call_count"] += 1
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                # Dispatcher may be sync or async; await coroutines.
                result = dispatch(tc.function.name, args)
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_running_loop()
                        # We're inside an event loop already — schedule
                        # nested via run_until_complete on a new loop
                        # would deadlock. Use asyncio.run on a fresh
                        # task in this case is also unsafe. Best path:
                        # run synchronously via asyncio.run in a worker
                        # only when no loop. Otherwise expect the
                        # caller to have provided a sync wrapper.
                        result = asyncio.run_coroutine_threadsafe(
                            result, loop).result(timeout=60)
                    except RuntimeError:
                        # No running loop — drive directly
                        result = asyncio.run(result)
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result if isinstance(result, str)
                                    else json.dumps(result, default=str),
                })
                if tc.function.name == "read_runbook":
                    rb = args.get("name")
                    if rb and isinstance(result, str) and "not found" not in result:
                        out["read_runbooks"].add(rb)
        elif retry_msg.content:
            messages.append({"role": "assistant",
                             "content": retry_msg.content})

        # Force-final
        messages.append({"role": "user", "content": (
            "Now emit the FULL updated RCA JSON applying the runbook's "
            "drill plan + action template. Required keys: summary, "
            "failed_stage, error_class, root_cause, evidence, "
            "suggested_fix (Finding/Action/Verify), suggested_commands. "
            "Return ONLY the JSON object."
        )})
        final = client.chat.completions.create(
            model=model, messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        out["tokens_in"]  += final.usage.prompt_tokens
        out["tokens_out"] += final.usage.completion_tokens
        text = final.choices[0].message.content or ""
        from .agent import _parse_final_json
        new = _parse_final_json(text)
        if isinstance(new, dict):
            out["rca"] = new
            out["messages"] = messages
    except Exception:
        # Best-effort retry; on any error leave the original RCA + signal.
        pass
    return out


# ─── Conditional-edge routers ─────────────────────────────────────────

def _route_after_gate(state: dict) -> str:
    """If the previous gate set `_gate_reason`, route to the corrective
    re-iter node. Otherwise continue to the next gate."""
    return "corrective_reiter" if state.get("_gate_reason") else "next"


def _route_after_reiter(state: dict) -> str:
    """After the corrective-reiter node runs, decide whether to loop
    back through the gate chain (when budget remains AND the retry
    succeeded in updating the rca) or terminate the graph.

    Without this conditional edge the graph hits a tight loop when a
    gate keeps firing on the same condition after the retry: e.g.
    compliance_hallucination triggers, retry runs but LLM doesn't
    fix the claim, gate fires again, retry already exhausted budget
    → returns {_gate_reason: None}, but the next gate check sees
    the same RCA and re-sets _gate_reason → back to reiter → ...
    LangGraph default recursion_limit=25 would catch this, but we
    saw a runaway to 10007 in production logs because budget=0 makes
    reiter a no-op that doesn't break the cycle.

    Cap behavior:
      - retry_budget == 0 → END (give up; signal stays in
        failure_signals so the operator sees the un-fixed gate)
      - retry_budget > 0 → loop back to check_runbook_fetched so
        downstream gates re-validate the updated RCA
    """
    if state.get("retry_budget", 0) <= 0:
        return "done"
    return "loop_back"


# ─── Graph construction ───────────────────────────────────────────────

def build_gate_graph() -> Optional[Any]:
    """Construct the LangGraph StateGraph. Returns None when langgraph
    is not installed — the caller should fall back to the imperative
    gate stack in agent.py."""
    if not _HAS_LANGGRAPH:
        return None

    g = StateGraph(UltimatumState)
    g.add_node("check_schema",                   _check_schema)
    g.add_node("check_runbook_fetched",          _check_runbook_fetched)
    g.add_node("check_primary_secondary",        _check_primary_secondary)
    g.add_node("check_compliance_hallucination", _check_compliance_hallucination)
    g.add_node("corrective_reiter",              _corrective_reiter)

    g.set_entry_point("check_schema")

    # Sequential gate chain. After each gate, route to either the
    # correction node (which loops back to the SAME gate to re-verify
    # after retry) or to the next gate.
    g.add_conditional_edges(
        "check_schema", _route_after_gate,
        {"corrective_reiter": "corrective_reiter",
         "next":              "check_runbook_fetched"},
    )
    g.add_conditional_edges(
        "check_runbook_fetched", _route_after_gate,
        {"corrective_reiter": "corrective_reiter",
         "next":              "check_primary_secondary"},
    )
    g.add_conditional_edges(
        "check_primary_secondary", _route_after_gate,
        {"corrective_reiter": "corrective_reiter",
         "next":              "check_compliance_hallucination"},
    )
    g.add_conditional_edges(
        "check_compliance_hallucination", _route_after_gate,
        {"corrective_reiter": "corrective_reiter",
         "next":              END},
    )
    # After corrective_reiter, conditionally route: loop back to
    # check_runbook_fetched when retry_budget remains (so downstream
    # gates re-validate the updated RCA), else END to avoid the tight
    # loop seen on Stagger Prod+1 build 5225 + create-quick-infra 42
    # when gpt-4o emits a retry that still fails the same gate.
    g.add_conditional_edges(
        "corrective_reiter", _route_after_reiter,
        {"loop_back": "check_runbook_fetched", "done": END},
    )

    return g.compile()


# ─── Smoke test (CLI) ─────────────────────────────────────────────────

def _smoke() -> int:
    """`python -m jenkins_rca.gates` — confirms langgraph imported and the
    graph compiles. Doesn't exercise the LLM retry path (that requires
    real client + model)."""
    if not _HAS_LANGGRAPH:
        print("langgraph NOT installed — gates.py will return None and "
              "agent.py will fall back to the imperative gate stack")
        return 1
    graph = build_gate_graph()
    if graph is None:
        print("graph build returned None — should not happen when "
              "langgraph IS installed; check gates.py imports")
        return 1
    print("langgraph available, gate graph compiles")
    print(f"  nodes: check_schema → check_runbook_fetched → "
          f"check_primary_secondary → check_compliance_hallucination → END")
    print(f"  mandatory runbook classes: {sorted(MANDATORY_RUNBOOK_CLASSES)}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_smoke())
