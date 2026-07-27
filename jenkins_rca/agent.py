"""Agent-mode RCA: OpenAI function-calling loop with iteration + cost cap.

When the classifier picks a "deep" class (compliance / canary_* /
health_check / parse_error / unknown / scm) we delegate to this module
instead of the one-shot path in `llm.py`.

The agent gets a tool palette and works the failure backwards from the
Jenkins job config → entrypoint pipeline file → failed stage body →
implementation functions. It keeps going until it either identifies the
file+line that caused the error or hits the budget.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from . import mcp_tools
from . import jenkins as jenkins_api
from . import agent_dispatch
from . import outcome_log
from . import tool_schemas
from . import jira as jira_api


# Phase 3 caps — see docs/rca/agent_mode_migration_plan.md.
# LLM decides when to stop. These are runaway/billing safety nets only.
MAX_TOOL_CALLS = int(os.environ.get("JENKINS_RCA_MAX_TOOL_CALLS", "25"))
COST_CAP_USD = float(os.environ.get("JENKINS_RCA_COST_CAP_USD", "5.0"))
WALL_CLOCK_SEC = int(os.environ.get("JENKINS_RCA_WALL_CLOCK_SEC", "180"))
# Per-tool-result cap. Lower = less context-bloat per iteration. Older
# results are also elided (see TRIM_HISTORY_AFTER) so this is mainly the
# CURRENT iteration's read budget.
PER_TOOL_RESULT_CAP = 1500   # was 3000; agent rarely needs more than ~50 lines
# After this many iterations, elide older tool-result bodies (keep the
# tool-call shell intact so the LLM still sees what was asked). Cuts the
# replay weight that drives input-token cost.
TRIM_HISTORY_AFTER = 1       # was 2; tighter trim, only keep last iter full

# Per-model pricing in USD per 1M tokens. Used for cost cap enforcement
# AND audit-record cost reporting. Update when OpenAI changes rates.
# Unknown models fall through to gpt-4o pricing (conservative — over-
# bills slightly rather than under-bills, so cost cap stays effective).
_MODEL_PRICING = {
    "gpt-4o":        {"in":  2.50, "out": 10.00},
    "gpt-4o-mini":   {"in":  0.15, "out":  0.60},
    "gpt-4.1":       {"in":  2.00, "out":  8.00},
    "gpt-4.1-mini":  {"in":  0.40, "out":  1.60},
    "gpt-5":         {"in":  3.00, "out": 15.00},   # approximate — verify before prod use
    "gpt-5-mini":    {"in":  0.50, "out":  2.00},   # approximate — verify before prod use
    "o1":            {"in": 15.00, "out": 60.00},
    "o1-mini":       {"in":  1.10, "out":  4.40},
    "o3-mini":       {"in":  1.10, "out":  4.40},
}


def _pricing_for(model: str) -> tuple[float, float]:
    """(input_per_token, output_per_token) for the given model name.
    Falls back to gpt-4o pricing if unknown."""
    p = _MODEL_PRICING.get(model, _MODEL_PRICING["gpt-4o"])
    return p["in"] / 1_000_000, p["out"] / 1_000_000


# Default agent model. Override per-RCA via JENKINS_RCA_MODEL env var.
#
# gpt-5 is preferred for its verbatim recall + reasoning, BUT requires
# project-level access on the OpenAI dashboard. If you see a 403
# PermissionDeniedError mentioning "does not have access to model
# `gpt-5`", enable it under your OpenAI project → Limits → Models.
#
# Until access is granted, default to gpt-4o which most projects have
# by default and which still gives better verbatim recall than gpt-4.1.
# Fallback chain (manual): gpt-5 → gpt-4o → gpt-4.1 → gpt-4o-mini.
#   sudo systemctl set-environment JENKINS_RCA_MODEL=gpt-5
#   sudo systemctl restart jenkins-rca
_DEFAULT_MODEL = os.environ.get("JENKINS_RCA_MODEL", "gpt-4o")

# Back-compat: keep the old module-level constants pointing at the
# default-model pricing so any external import doesn't break. Active
# code in run_agent now reads pricing per-call via _pricing_for().
INPUT_USD_PER_TOKEN, OUTPUT_USD_PER_TOKEN = _pricing_for(_DEFAULT_MODEL)


def _log(msg: str) -> None:
    print(f"[agent] {msg}", file=sys.stderr, flush=True)


# Chain-walk self-verification prompt. Injected ONCE when LLM first stops
# calling tools (before FORCE_FINAL_PROMPT). LLM reviews its own tool history,
# decides if any function calls / libraryResource references it saw were never
# followed to their vars/ or resources/ implementation files, and calls them if
# so. Universal — works for any job type / error class without hardcoding files.
_CHAIN_VERIFY_PROMPT = (
    "Before finalizing, verify your chain-walk is complete:\n"
    "1. Look at every function call you saw in files you read — e.g., "
    "`deployProdPlusOne(...)`, `createRuleForProdPlusOne(...)`, any "
    "`libraryResource 'X'`, any `deploy(...)` or helper step.\n"
    "2. Did you follow EACH call to its `vars/<name>.groovy` implementation "
    "file via repo_read_file?\n"
    "3. Did you follow EACH `libraryResource 'scripts/...'` reference to "
    "`resources/scripts/...` via repo_read_file? "
    "(Scripts only — skip config/template files like config.json, *.yml, "
    "*.conf, fluent-bit.conf, etc. Those are data files, not scripts.)\n"
    "4. Is there any function or script you cited in evidence or reasoning "
    "that you never actually read?\n"
    "5. For each repo evidence entry you plan to emit: is (line_end - line_start) > 15? "
    "If yes — that is your READ WINDOW, not evidence. Re-read the file content you saw "
    "and identify the 1-5 specific lines that directly show the failure (e.g. the function "
    "call that triggers the failed action, the exit condition, the resource declaration). "
    "Set line_start + line_end to THOSE lines only. A range of 80 lines in evidence means "
    "you haven't identified the cause yet.\n\n"
    "If YES to any of the above — fix NOW before writing the final answer.\n"
    "If chain complete AND all evidence entries cite ≤15 lines — emit your FINAL JSON answer."
)

# Forced final-answer prompt. Used both for "tool budget exhausted" and
# "cost cap reached" paths. The explicit schema + "JSON object only, no
# markdown" guard rails are necessary because gpt-4o sometimes emits a
# markdown report ("### Summary\n...") when its prior tool call errored —
# the response_format=json_object constraint alone hasn't been enough.
_FORCE_FINAL_PROMPT = (
    "Stop calling tools. Emit your FINAL answer NOW as a single JSON object "
    "(NOT markdown, NOT ###headings — ONLY a JSON object that parses with "
    "json.loads). Schema:\n"
    "{\n"
    '  "summary": "string",\n'
    '  "failed_stage": "string",\n'
    '  "error_class": "compliance|canary_fail|canary_script_error|health_check|aws_limit|parse_error|java_runtime|scm|network|dependency|ssm|timeout|unknown",\n'
    '  "root_cause": "string with citations from files you read",\n'
    '  "evidence": [\n'
    '    /* REPO-FILE evidence: emit coordinates ONLY; server fills snippet from disk. */\n'
    '    {"source": "jenkins_pipeline/<file>|InfraComposer/<file>", "line_start": 1, "line_end": 1},\n'
    '    /* NON-repo evidence: snippet must be verbatim from a tool result you saw. */\n'
    '    {"source": "jenkins_log|build_meta|jira:<KEY>|aws:<resource>|docs/runbooks/<name>.md", "snippet": "verbatim string"}\n'
    '  ],\n'
    '  "suggested_fix": "string OR {Finding,Action,Verify}",\n'
    '  "suggested_commands": [{"cmd": "string", "tier": "safe|restricted", "rationale": "string"}],\n'
    '  "confidence": 0.0,\n'
    '  "needs_deeper": false\n'
    "}\n"
    "If a tool errored earlier, that's fine — use the context you already "
    "have (primer + earlier tool results) to compose the JSON.\n"
    "\n"
    "USE THE EVIDENCE YOU HAVE — but you MUST open at least one source file "
    "via `repo_read_file` before finalizing (see system-prompt cross-check "
    "rule). Examples:\n"
    "  • `MissingMethodException: No signature of method: X.call(...) is "
    "applicable for argument types: (...). Possible solutions: "
    "call(A,B,C)` — the log NAMES the fix, but you still MUST open the "
    "caller (mapped from WorkflowScript:<line> via get_jenkins_job_config) "
    "AND `vars/X.groovy` to confirm the signature. Cite BOTH file:line "
    "entries in evidence.\n"
    "  • Stack trace with file:line in a file you READ — cite "
    "`<repo>/<file>:<line>` exactly.\n"
    "  • `Health Status: unhealthy` + you read healthy.sh — cite poll-loop "
    "line and explain the timeout.\n"
    "\n"
    "OPERATOR-FACING LANGUAGE — banned terms in `summary` / `root_cause` / "
    "`suggested_fix`:\n"
    "  ✗ \"agent budget\", \"tool calls\", \"iterations\", \"implementation "
    "site not reached\", \"could not be reached within the tool budget\", "
    "\"my budget\", \"my reasoning\"\n"
    "  ✓ Write as if a senior SRE diagnosed it from the log — concrete, "
    "operator-actionable. Mention `vars/JiraDetails.groovy`, file paths, "
    "line numbers, real signatures from the log.\n"
    "\n"
    "ONLY use `needs_deeper: true` if BOTH (a) you did not read any "
    "relevant repo file AND (b) the log itself does not contain a clear "
    "exception type with a usable hint. If the log spells out the answer, "
    "`needs_deeper` is wrong — set it `false`.\n"
    "\n"
    "Output the JSON object only — no prose before or after."
)


def _load_prompt(name: str) -> str:
    """Read a prompt file. When loading `rca_system.md` or
    `rca_agent_system.md`, prepend `rca_common.md` (Phase 2 dedup —
    shared rules live in common, path-specific in body)."""
    base = Path(__file__).parent.parent / "prompts"
    p = base / name
    if not p.exists():
        return ""
    body = p.read_text()
    if name in ("rca_system.md", "rca_agent_system.md"):
        common = base / "rca_common.md"
        if common.exists():
            return common.read_text() + "\n\n---\n\n" + body
    return body


# ---------------------------------------------------------------------------
# Tool definitions exposed to the LLM (OpenAI function-calling schema)
# ---------------------------------------------------------------------------

TOOLS = tool_schemas.TOOLS  # 21 tools — see jenkins_rca/tool_schemas.py


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

import inspect as _inspect


async def _dispatch_tool(name: str, args: dict, ctx: dict) -> str:
    """Invoke the local helper for a tool the LLM asked to call.

    Resolution order:
      1. Special cases that need per-RCA `ctx` (Jenkins creds,
         service-lookup result formatting, etc.).
      2. Generic agent_dispatch.TOOL_DISPATCH lookup — sync or async
         callables are both supported; coroutines are awaited.

    Returns a string (JSON-serialised when the tool returns a dict/list).
    On any exception returns `tool error: <ExceptionType>: <msg>` so the
    LLM gets a useful failure instead of a 500 crashing the loop.
    """
    try:
        # ── Special cases (need ctx or output massaging) ─────────────
        if name == "get_jenkins_job_config":
            cfg = await jenkins_api.get_job_config(
                args["job"], ctx["jenkins_url"], ctx["jenkins_auth"],
            )
            # Drop the giant raw_xml from the response shown to the LLM —
            # the structured fields are what it needs to pick a script path.
            cfg = {k: v for k, v in cfg.items() if k != "raw_xml"}
            return json.dumps(cfg, indent=2)
        if name == "service_lookup":
            return json.dumps(mcp_tools.service_lookup(args["service"]), indent=2)

        # ── Generic dispatch via agent_dispatch.TOOL_DISPATCH ────────
        fn = agent_dispatch.TOOL_DISPATCH.get(name)
        if fn is None:
            return f"unknown tool: {name}"
        result = fn(**args)
        if _inspect.iscoroutine(result):
            result = await result
        # Serialise structured returns; strings pass through.
        if isinstance(result, (dict, list)):
            return json.dumps(result, indent=2, default=str)
        if result is None:
            return "null"
        return str(result)
    except Exception as e:
        return f"tool error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def run_agent(
    *,
    api_key: str,
    job: str,
    build: int,
    service: str,
    build_meta: dict,
    log_window: str,
    error_class: str,
    initial_tool_ctx: str,
    jenkins_url: str,
    jenkins_auth: tuple,
    model: str | None = None,
) -> dict:
    """Run a function-calling agent until it emits final RCA JSON or hits caps.

    `initial_tool_ctx` is the same pre-computed context block used by the
    one-shot path (service.lookup, source.trace, docs.<class>.md, jira.tickets,
    etc.). We feed it to the agent as a primer so cheap classes still get
    instant grounding without burning tool calls.

    `model` defaults to the JENKINS_RCA_MODEL env var (or gpt-4.1). Cost cap
    uses per-model pricing so swapping to a cheaper / more expensive model
    just shifts the iteration ceiling at the dollar bound.
    """
    if model is None:
        model = _DEFAULT_MODEL
    in_per_tok, out_per_tok = _pricing_for(model)
    _log(f"model={model} input=${in_per_tok*1_000_000:.2f}/M output=${out_per_tok*1_000_000:.2f}/M")

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    system = _load_prompt("rca_agent_system.md")
    primer = await _build_primer(job, build, service, error_class, build_meta,
                                 log_window, initial_tool_ctx)
    # Concatenate primer into the system message so OpenAI's automatic prompt
    # caching can reuse the prefix across iterations (cached tokens billed at
    # ~50% rate). The user message stays short — just kicks off the trace.
    system_full = system + "\n\n" + primer

    # User message varies by mode. Option C demands the LLM USE tools (don't
    # short-circuit from the primer). Legacy mode discouraged re-fetching
    # because all data was pre-fetched into the primer.
    if os.environ.get("JENKINS_RCA_FORCE_AGENT_MODE"):
        _user_kickoff = (
            "Begin the RCA. The primer above contains ONLY log_window, "
            "build_meta, and service.lookup. You MUST call tools to fetch "
            "everything else (pipeline source via repo_read_file, drill plan "
            "via read_runbook, Jira fields via jira_get_ticket, AWS state via "
            "aws_describe_*, etc.). Follow the mandatory pipeline cross-check. "
            "Emit final JSON only after you can name a concrete cause."
        )
    else:
        _user_kickoff = (
            "Begin the trace. Use the primer above. Don't re-fetch what's "
            "already there. Cite repo evidence at IMPLEMENTATION lines."
        )
    messages = [
        {"role": "system", "content": system_full},
        {"role": "user", "content": _user_kickoff},
    ]

    # Optional prompt dump for debugging.
    # Enable: sudo systemctl set-environment JENKINS_RCA_DEBUG_PROMPT=1
    # Reads:  cat /tmp/jenkins-rca-last-prompt.txt
    if os.environ.get("JENKINS_RCA_DEBUG_PROMPT"):
        try:
            with open("/tmp/jenkins-rca-last-prompt.txt", "w") as _f:
                _f.write("=== MODEL ===\n" + model + "\n\n")
                _f.write("=== MODE ===\nagent\n\n")
                _f.write("=== SYSTEM MESSAGE (includes primer) ===\n" + system_full + "\n\n")
                _f.write("=== INITIAL USER MESSAGE ===\n" + messages[1]["content"] + "\n\n")
                _f.write("=== TOOLS SCHEMA ===\n" + json.dumps(TOOLS, indent=2) + "\n")
        except Exception as _e:
            _log(f"prompt dump failed: {_e}")

    # Optional full-transcript dump (each iter's request, response, tool
    # calls, tool results) — for manager-grade audit / training data.
    # Enable: JENKINS_RCA_DEBUG_TRACE=1
    #
    # Two files written each RCA:
    #   * /tmp/jenkins-rca-last-trace.txt           — latest only (convenience)
    #   * /tmp/jenkins-rca-trace-<job>-<build>.txt  — per-build copy (history)
    # Reads:  cat /tmp/jenkins-rca-trace-create-quick-infra-devops-test-15.txt
    _trace_enabled = bool(os.environ.get("JENKINS_RCA_DEBUG_TRACE"))
    _trace_path = "/tmp/jenkins-rca-last-trace.txt"
    _safe_job = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(job))
    _per_build_path = f"/tmp/jenkins-rca-trace-{_safe_job}-{build}.txt"
    _trace_paths = [_trace_path, _per_build_path]
    # Cap per-build history at 50 files; delete oldest beyond cap.
    if _trace_enabled:
        try:
            import glob as _glob
            _olds = sorted(_glob.glob("/tmp/jenkins-rca-trace-*.txt"),
                           key=lambda p: os.path.getmtime(p))
            for _old in _olds[:-50]:
                try:
                    os.unlink(_old)
                except OSError:
                    pass
        except Exception:
            pass
    if _trace_enabled:
        try:
            for _p in _trace_paths:
                with open(_p, "w") as _f:
                    _f.write(f"=== AGENT TRACE — job={job} build={build} service={service} model={model} ===\n\n")
                    _f.write("=== INITIAL SYSTEM MESSAGE (truncated; full in /tmp/jenkins-rca-last-prompt.txt) ===\n")
                    _f.write(system_full[:4000] + ("\n…[truncated]\n" if len(system_full) > 4000 else "\n"))
                    _f.write("\n=== INITIAL USER MESSAGE ===\n")
                    _f.write(messages[1]["content"] + "\n\n")
        except Exception as _e:
            _log(f"trace init failed: {_e}")
            _trace_enabled = False

    def _trace(label: str, body: str) -> None:
        if not _trace_enabled:
            return
        for _p in _trace_paths:
            try:
                with open(_p, "a") as _f:
                    _f.write(f"\n--- {label} ---\n{body}\n")
            except Exception:
                pass

    def _fmt_request_payload(msgs: list, kwargs: dict, hide_system: bool = False) -> str:
        """Render the exact OpenAI request payload for trace logs.

        Truncates per-message content to keep file size sane — full
        unredacted prompt + tool schemas live in /tmp/jenkins-rca-last-prompt.txt.
        When hide_system=True (iter > 0), elides the giant system message
        body since it's already dumped in full at the top of the trace.
        Tool-result messages are also capped tighter — body is already
        in the corresponding `ITER N TOOL #X` block above.
        """
        PER_MSG_CHARS = 1500
        TOOL_MSG_CHARS = 200   # tool results already dumped in ITER N TOOL #X
        lines = []
        lines.append(f"model: {kwargs.get('model')}")
        lines.append(f"temperature: {kwargs.get('temperature')}")
        if "response_format" in kwargs:
            lines.append(f"response_format: {kwargs['response_format']}")
        if "tools" in kwargs:
            tool_names = [t.get("function", {}).get("name", "?") for t in kwargs["tools"]]
            lines.append(f"tools: {tool_names}")
        if "tool_choice" in kwargs:
            lines.append(f"tool_choice: {kwargs['tool_choice']}")
        lines.append(f"messages ({len(msgs)} total):")
        for i, m in enumerate(msgs):
            role = m.get("role", "?")
            content = m.get("content") or ""
            if role == "system" and hide_system:
                lines.append(f"  [{i}] role=system [omitted — see INITIAL SYSTEM MESSAGE at top]")
                continue
            cap = TOOL_MSG_CHARS if role == "tool" else PER_MSG_CHARS
            if len(content) > cap:
                content = content[:cap] + f"\n…[truncated, +{len(m.get('content',''))-cap} chars; full body in ITER N TOOL block above]"
            extras = []
            if m.get("tool_calls"):
                extras.append(f"tool_calls={[(tc['function']['name'], tc['function']['arguments'][:200]) for tc in m['tool_calls']]}")
            if m.get("tool_call_id"):
                extras.append(f"tool_call_id={m['tool_call_id']}")
            if m.get("name"):
                extras.append(f"name={m['name']}")
            extra_str = (" " + " ".join(extras)) if extras else ""
            lines.append(f"  [{i}] role={role}{extra_str}")
            if content:
                lines.append(f"      content: {content}")
        return "\n".join(lines)

    # Per-iter accounting for the SUMMARY block prepended at end of trace.
    # Each entry: dict(iter, in, out, finish, reasoning, tools_chosen[]).
    _iter_log: list[dict] = []
    # Deterministic failure-signal tally fed to outcome_log at end of run.
    # See outcome_log.py header for the canonical signal vocabulary.
    _failure_signals: list[str] = []
    # Opt-in raw OpenAI response dump. Structured (content + tool_calls) is
    # already shown — the raw model_dump JSON is duplicate noise unless you
    # are debugging openai-client behavior. Set JENKINS_RCA_RAW_DUMP=1 to keep.
    _emit_raw_dump = bool(os.environ.get("JENKINS_RCA_RAW_DUMP"))

    ctx = {"jenkins_url": jenkins_url, "jenkins_auth": jenkins_auth}
    total_in = total_out = 0
    tool_call_count = 0
    final_text = None
    # Track every successful repo_read_file call so we can validate the
    # final evidence array against it (post-parse hallucination guard).
    # Key: "<repo>/<path>" — line range is ignored, any read counts.
    read_files: set[str] = set()
    # Track runbook names that were actually fetched and returned content
    # (non-"not found"). Used to drop evidence citing unfetched runbooks.
    read_runbooks: set[str] = set()
    # Chain-walk self-verification gate. Injected once when LLM first stops
    # voluntarily. Prevents finalize until LLM has checked its own chain.
    _chain_verify_done: bool = False
    # Dedup map: (tool_name, sorted_args_json) -> (iter, cached_result, hit_count).
    # 1st repeat → return cached + soft warning.
    # 2nd+ repeat → return ERROR ONLY, no data, so the LLM is forced to
    # change strategy. gpt-4.1 has been observed ignoring the soft warning
    # and re-issuing the same call 3-4 times (build 5177: precheck.groovy
    # called 4× same args), each time burning ~10K input tokens on the
    # full conversation re-send.
    tool_call_cache: dict[tuple[str, str], tuple[int, str, int]] = {}

    # Phase 3: wall-clock cap so Jenkins post-block doesn't hang on a
    # runaway loop. LLM-driven stopping is the normal path; this is
    # a last-resort safety net.
    _loop_start = time.monotonic()

    for iteration in range(MAX_TOOL_CALLS + 1):
        cost_so_far = total_in * in_per_tok + total_out * out_per_tok
        _wall_elapsed = time.monotonic() - _loop_start
        if _wall_elapsed >= WALL_CLOCK_SEC:
            _log(f"wall-clock cap hit at {_wall_elapsed:.1f}s — forcing final answer")
            messages.append({"role": "user", "content": _FORCE_FINAL_PROMPT})
            _cap_kwargs = {
                "model": model, "messages": messages,
                "response_format": {"type": "json_object"}, "temperature": 0.1,
            }
            _trace("WALL-CLOCK FORCED FINAL REQUEST",
                   _fmt_request_payload(messages, _cap_kwargs))
            response = client.chat.completions.create(**_cap_kwargs)
            total_in += response.usage.prompt_tokens
            total_out += response.usage.completion_tokens
            final_text = response.choices[0].message.content
            _trace("WALL-CLOCK FORCED FINAL RESPONSE",
                   f"prompt_tokens={response.usage.prompt_tokens} "
                   f"completion_tokens={response.usage.completion_tokens}\n"
                   f"content={(final_text or '')[:1500]}")
            _failure_signals.append("force_final_wall_clock")
            break
        if cost_so_far >= COST_CAP_USD:
            _log(f"cost cap hit at ${cost_so_far:.4f} — forcing final answer")
            messages.append({
                "role": "user",
                "content": _FORCE_FINAL_PROMPT,
            })
            _cap_kwargs = {
                "model": model, "messages": messages,
                "response_format": {"type": "json_object"}, "temperature": 0.1,
            }
            _trace("COST-CAP FORCED FINAL REQUEST",
                   _fmt_request_payload(messages, _cap_kwargs))
            response = client.chat.completions.create(**_cap_kwargs)
            total_in += response.usage.prompt_tokens
            total_out += response.usage.completion_tokens
            final_text = response.choices[0].message.content
            _trace("COST-CAP FORCED FINAL RESPONSE",
                   f"prompt_tokens={response.usage.prompt_tokens} "
                   f"completion_tokens={response.usage.completion_tokens}\n"
                   f"content={(final_text or '')[:1500]}")
            _failure_signals.append("force_final_cost_cap")
            break

        # On the final iteration, force JSON-only output (no more tools)
        force_final = iteration == MAX_TOOL_CALLS
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
        }
        if force_final:
            kwargs["response_format"] = {"type": "json_object"}
            messages.append({
                "role": "user",
                "content": _FORCE_FINAL_PROMPT,
            })
            _failure_signals.append("force_final_iter_cap")
        else:
            kwargs["tools"] = TOOLS
            kwargs["tool_choice"] = "auto"

        _trace(f"ITER {iteration} REQUEST",
               f"force_final={force_final} cost_so_far=${cost_so_far:.4f} "
               f"messages_count={len(messages)} tokens_so_far={total_in}+{total_out}\n"
               + _fmt_request_payload(messages, kwargs, hide_system=(iteration > 0)))
        response = client.chat.completions.create(**kwargs)
        total_in += response.usage.prompt_tokens
        total_out += response.usage.completion_tokens
        msg = response.choices[0].message
        _resp_body = (
            f"prompt_tokens={response.usage.prompt_tokens} "
            f"completion_tokens={response.usage.completion_tokens}\n"
            f"finish_reason={response.choices[0].finish_reason}\n"
            f"reasoning={(msg.content or '').strip()[:1500] or '(none)'}\n"
            f"tool_calls={[(tc.function.name, tc.function.arguments) for tc in (msg.tool_calls or [])]}"
        )
        if _emit_raw_dump:
            try:
                _raw_resp = json.dumps(response.model_dump(), indent=2, default=str)
            except Exception as _e:
                _raw_resp = f"[model_dump failed: {_e}]"
            _RESP_CAP = 12000
            _resp_body += (
                f"\n--- raw OpenAI response (model_dump, {len(_raw_resp)} chars) ---\n"
                f"{_raw_resp[:_RESP_CAP]}"
                + (f"\n…[truncated, +{len(_raw_resp) - _RESP_CAP} more chars]"
                   if len(_raw_resp) > _RESP_CAP else "")
            )
        _trace(f"ITER {iteration} RESPONSE", _resp_body)
        # Per-iter accounting for the SUMMARY block.
        _iter_log.append({
            "iter": iteration,
            "in": response.usage.prompt_tokens,
            "out": response.usage.completion_tokens,
            "finish": response.choices[0].finish_reason,
            "reasoning": (msg.content or "").strip(),
            "tools_chosen": [
                (tc.function.name, tc.function.arguments)
                for tc in (msg.tool_calls or [])
            ],
        })

        if force_final or not msg.tool_calls:
            final_text = msg.content or ""
            # Text-tool-calls rescue: when gpt-4.1 imitates the system
            # prompt's narration example LITERALLY and writes
            # `tool_calls: - functions.foo: ...` as TEXT inside the
            # content field instead of using the function-calling API.
            # Symptoms: msg.tool_calls is None, content contains
            # patterns like "tool_calls:" or "functions.<name>".
            # Re-prompt with stronger instruction to use the actual
            # function-calling mechanism, no response_format constraint
            # so it can return tool_calls structured field.
            _looks_like_text_tool_calls = (
                ("tool_calls:" in final_text or "functions." in final_text)
                and not _parse_final_json(final_text)
            )
            if not force_final and _looks_like_text_tool_calls:
                _failure_signals.append("text_tool_calls_rescue")
                _log("LLM wrote tool_calls as text in content; re-prompting "
                     "to use the function-calling API")
                messages.append({
                    "role": "user",
                    "content": (
                        "STOP. You wrote tool_calls as TEXT inside the "
                        "content field. That does NOT invoke any tools. "
                        "To actually call tools, use the OpenAI "
                        "function-calling API: emit the structured "
                        "tool_calls array, NOT prose that mentions them. "
                        "Retry now. content should be a one-sentence "
                        "reasoning string (prose), tool_calls should be "
                        "your actual function invocations."
                    ),
                })
                _retry_kwargs = {
                    "model": model, "messages": messages,
                    "tools": TOOLS, "tool_choice": "required",
                    "temperature": 0.1,
                }
                _trace("TEXT-TOOL-CALLS RESCUE REQUEST",
                       _fmt_request_payload(messages, _retry_kwargs))
                retry = client.chat.completions.create(**_retry_kwargs)
                total_in += retry.usage.prompt_tokens
                total_out += retry.usage.completion_tokens
                _trace("TEXT-TOOL-CALLS RESCUE RESPONSE",
                       f"prompt_tokens={retry.usage.prompt_tokens} "
                       f"completion_tokens={retry.usage.completion_tokens}\n"
                       f"content={(retry.choices[0].message.content or '')[:1500]}\n"
                       f"tool_calls={[(tc.function.name, tc.function.arguments) for tc in (retry.choices[0].message.tool_calls or [])]}")
                _rescue_msg = retry.choices[0].message
                if _rescue_msg.tool_calls:
                    # Replace the bad assistant message with the rescued one
                    # and continue the loop normally — don't break out.
                    msg = _rescue_msg
                    # Fall through to the normal tool-call execution path.
                else:
                    # Rescue also failed; give up gracefully on this iter.
                    final_text = _rescue_msg.content or final_text
                    break

            # If text-tool-calls rescue produced real tool_calls, skip the
            # rest of the bail block and fall through to execution.
            if msg.tool_calls:
                pass  # fall through past the `if` block below
            else:
                # Voluntary-bail rescue: when the LLM stops emitting
                # tool_calls mid-loop (it thinks it has enough info), this
                # path was NOT bound by response_format=json_object — so
                # the LLM is free to dump a markdown report
                # ("### Summary\n...") which trips the fallback stub. If
                # that happens, do ONE retry with the JSON constraint +
                # force-final prompt. Adds ~$0.05 in worst case but
                # rescues the otherwise-wasted 6 tool calls.
                if not force_final and _parse_final_json(final_text) is None:
                    # Chain-walk self-verification gate: injected ONCE before
                    # the first finalize attempt. LLM checks its own tool
                    # history for unread vars/ / resources/ files and calls
                    # them if needed. Universal — no hardcoded file lists.
                    if not _chain_verify_done:
                        _chain_verify_done = True
                        _log("LLM stopped — injecting chain-walk verification "
                             "before finalizing")
                        messages.append({
                            "role": "user",
                            "content": _CHAIN_VERIFY_PROMPT,
                        })
                        continue  # re-enter loop; LLM may call more tools

                    # JSON FINALIZE STEP — normal flow, not a failure.
                    # When LLM stops calling tools (signals "I am done")
                    # it sometimes emits the answer as markdown headings
                    # because response_format=json_object was not on the
                    # tool-iter call (it would prevent tool calls). We
                    # re-issue with json_object enforced so the FINAL
                    # answer is guaranteed to be parseable JSON.
                    # No failure_signal — this is part of the contract,
                    # not an emergency rescue.
                    _log("LLM finalising — re-prompting with "
                         "response_format=json_object to enforce JSON output")
                    messages.append({"role": "user", "content": _FORCE_FINAL_PROMPT})
                    _retry_kwargs = {
                        "model": model, "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": 0.1,
                    }
                    _trace("JSON FINALIZE REQUEST",
                           _fmt_request_payload(messages, _retry_kwargs))
                    retry = client.chat.completions.create(**_retry_kwargs)
                    total_in += retry.usage.prompt_tokens
                    total_out += retry.usage.completion_tokens
                    final_text = retry.choices[0].message.content
                    _trace("JSON FINALIZE RESPONSE",
                           f"prompt_tokens={retry.usage.prompt_tokens} "
                           f"completion_tokens={retry.usage.completion_tokens}\n"
                           f"content={(final_text or '')[:1500]}")
                break

        # Append assistant message + execute each tool call
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })
        for tc in msg.tool_calls:
            tool_call_count += 1
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            _log(f"iter {iteration} tool#{tool_call_count}: {tc.function.name}({list(args)})")

            # Tool-call dedup with escalating strictness:
            #   1st repeat → return cached + soft warning (current behaviour)
            #   2nd+ repeat → return ERROR ONLY, no data
            # The escalation forces the LLM to change strategy. gpt-4.1
            # has been observed ignoring soft warnings and re-issuing the
            # same call 3-4 times in a row, each iter burning ~10K input
            # tokens on the full conversation resend.
            fingerprint = (tc.function.name, json.dumps(args, sort_keys=True))
            if fingerprint in tool_call_cache:
                prev_iter, prev_result, hit_count = tool_call_cache[fingerprint]
                hit_count += 1
                tool_call_cache[fingerprint] = (prev_iter, prev_result, hit_count)
                _log(f"  → DUP #{hit_count}: same call ran in iter {prev_iter}")
                if hit_count == 1:
                    _failure_signals.append("dup_call_warning")
                    result = (
                        f"[DUP_CALL #1: this exact call was already executed "
                        f"in iter {prev_iter}. Result was:]\n{prev_result}\n"
                        f"[end of cached result — try a DIFFERENT query or read "
                        f"a DIFFERENT file; repeating the same call wastes "
                        f"the budget.]"
                    )
                else:
                    _failure_signals.append("dup_call_rejected")
                    result = (
                        f"ERROR: repeated tool call rejected. You have called "
                        f"{tc.function.name}({json.dumps(args)}) {hit_count + 1} "
                        f"times. No new data will be returned. CHANGE STRATEGY:\n"
                        f"  - Call a DIFFERENT path / arg combination, OR\n"
                        f"  - Use DIFFERENT line range (start/end != "
                        f"{args.get('start', '?')}, {args.get('end', '?')}), OR\n"
                        f"  - Move on to emit final JSON with the data you "
                        f"already have. The cached result from iter "
                        f"{prev_iter} is still in the message history above; "
                        f"re-read it instead of re-fetching."
                    )
            else:
                result = await _dispatch_tool(tc.function.name, args, ctx)
                # Cap each tool result so a runaway grep doesn't blow the
                # window. SKIP cap for authored/curated reads:
                #   - read_runbook: small (≤10 KB), runbook tail often holds
                #     MANDATORY rules that the cap would silently chop off
                #     (build 46 v2: DescribeTargetGroups MANDATORY rule sits
                #     at byte ~3 KB; old 1500-char cap hid it → LLM skipped
                #     the call → no port-mismatch detection).
                #   - repo_read_file / github_read_file: LLM already picks a
                #     tight start/end line range, so double-capping by bytes
                #     is just silent loss.
                # Untrusted/large outputs (grep, search, aws describe, ...)
                # keep the cap.
                _skip_cap = tc.function.name in (
                    "read_runbook", "read_doc", "read_job_flow",
                    "repo_read_file", "github_read_file",
                )
                if not _skip_cap and len(result) > PER_TOOL_RESULT_CAP:
                    result = result[:PER_TOOL_RESULT_CAP] + "\n…[truncated]"
                tool_call_cache[fingerprint] = (iteration, result, 0)

            # Track repo_read_file paths so the final evidence array can
            # be validated against actual reads. Only track on a successful
            # (non-error) read so failed paths don't get whitelisted.
            if tc.function.name == "repo_read_file":
                repo = args.get("repo")
                path = args.get("path")
                if repo and path and not result.startswith(("error:", "[error", "ERROR:")):
                    read_files.add(f"{repo}/{path}")
                else:
                    _failure_signals.append("file_not_found_in_chain")
            elif tc.function.name == "github_read_file":
                if result.startswith(("error:", "[error", "ERROR:")):
                    _failure_signals.append("file_not_found_in_chain")
            elif tc.function.name == "read_runbook":
                rb_name = args.get("name")
                if rb_name and "not found" not in result:
                    read_runbooks.add(rb_name)

            _result_str = result if isinstance(result, str) else str(result)
            _DUMP_CAP = 8000
            _trace(
                f"ITER {iteration} TOOL #{tool_call_count} {tc.function.name}",
                f"args={json.dumps(args)}\n"
                f"result_len={len(_result_str)} chars\n"
                f"result=\n{_result_str[:_DUMP_CAP]}"
                + (f"\n…[truncated, +{len(_result_str) - _DUMP_CAP} more chars]"
                   if len(_result_str) > _DUMP_CAP else "")
                + ("\n[NOTE: tool returned empty string]" if not _result_str else ""),
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        # Trim history: elide tool-result bodies from older iterations to
        # cut the token-replay cost. Keep the tool-call shells so the LLM
        # still sees the question + the fact it got answered.
        _elide_old_tool_results(messages, current_iter=iteration,
                                keep_recent=TRIM_HISTORY_AFTER)

    # Final JSON parse — tolerate markdown code fences (LLMs sometimes wrap
    # despite response_format=json_object). Log the raw text on failure so
    # the actual model output is visible in journalctl for debugging.
    rca = _parse_final_json(final_text)

    # Schema-completeness gate. _parse_final_json accepts ANY dict, including
    # degenerate ones (e.g. LLM emits a single `{cmd, tier, rationale}` and
    # nothing else — observed on Build 15 Stagger Scaling 2nd run). When the
    # parsed dict is missing required RCA keys, do ONE retry with an explicit
    # schema-reminder prompt + response_format=json_object so the LLM emits
    # the full RCA object. Adds ~$0.05 in worst case.
    _RCA_REQUIRED = ("summary", "failed_stage", "error_class", "root_cause",
                     "evidence", "suggested_fix")
    if rca is not None:
        _missing = [k for k in _RCA_REQUIRED if k not in rca]
        if _missing:
            _failure_signals.append("malformed_final_schema")
            _log(f"final JSON missing required RCA keys: {_missing} — "
                 f"issuing schema-completion retry")
            messages.append({
                "role": "user",
                "content": (
                    "STOP. The JSON you emitted is missing required RCA "
                    f"keys: {', '.join(_missing)}. Emit the FULL RCA JSON "
                    "object now, matching the schema in the system prompt. "
                    "Required keys: summary, failed_stage, error_class, "
                    "root_cause, evidence (array of {source, snippet} or "
                    "{source, line_start, line_end} for repo files), "
                    "suggested_fix (object with Finding/Action/Verify), "
                    "suggested_commands (array). Return ONLY the JSON "
                    "object, starting with { and ending with }."
                ),
            })
            _retry_kwargs = {
                "model": model, "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }
            _trace("SCHEMA COMPLETION RETRY REQUEST",
                   _fmt_request_payload(messages, _retry_kwargs))
            try:
                retry = client.chat.completions.create(**_retry_kwargs)
                total_in += retry.usage.prompt_tokens
                total_out += retry.usage.completion_tokens
                _retry_text = retry.choices[0].message.content
                _trace("SCHEMA COMPLETION RETRY RESPONSE",
                       f"prompt_tokens={retry.usage.prompt_tokens} "
                       f"completion_tokens={retry.usage.completion_tokens}\n"
                       f"content={(_retry_text or '')[:1500]}")
                _rca_retry = _parse_final_json(_retry_text)
                if _rca_retry is not None:
                    rca = _rca_retry
                    final_text = _retry_text
            except Exception as _e:
                _log(f"schema completion retry failed: {_e}")

    if rca is None:
        _failure_signals.append("final_json_parse_failed")
        _log("agent did not emit valid JSON; falling back to error stub")
        _log(f"raw final_text (first 800 chars): {(final_text or '')[:800]!r}")
        rca = {
            "summary": "Agent failed to emit valid JSON.",
            "failed_stage": build_meta.get("detected_failed_stage", "—"),
            "error_class": error_class,
            "root_cause": "Agent loop did not produce a parseable RCA JSON.",
            "evidence": [],
            "suggested_fix": "Re-run with deep:true or inspect agent stderr logs.",
            "suggested_commands": [],
            "confidence": 0.0,
            "needs_deeper": True,
        }

    # Backfill any STILL-missing required keys with safe defaults so the
    # outer serializer + UI never crash on a partial object. The schema-
    # completion retry above is the primary fix; this is the last-resort
    # safety net (retry might also have produced an incomplete object).
    _defaults = {
        "summary": "Agent did not emit a summary.",
        "failed_stage": build_meta.get("detected_failed_stage", "—"),
        "error_class": error_class,
        "root_cause": (final_text or "")[:400] or
                      "Agent did not emit root_cause prose.",
        "evidence": [],
        "suggested_fix": {
            "Finding": "Agent did not emit a structured fix.",
            "Action": "Re-run with deep:true or inspect agent stderr logs.",
            "Verify": "",
        },
    }
    for _k, _v in _defaults.items():
        if _k not in rca:
            rca[_k] = _v
    if "suggested_commands" not in rca:
        rca["suggested_commands"] = []

    # Phase 4 — ULTIMATUM gate.
    # Phase 6 (RAG-LANG branch) — when langgraph is available, delegate
    # to the StateGraph in `gates.py`. Same gates, same retry behavior,
    # but routing is declarative (easier to add gate #N+1). When
    # langgraph is NOT installed, fall through to the imperative stack
    # below — identical to the RAG-branch behavior.
    try:
        from . import gates as _gates
        _gate_graph = _gates.build_gate_graph()
    except Exception as _e:
        _gate_graph = None
        _log(f"gates.py import skipped: {_e}")

    _skip_imperative_gates = False
    if _gate_graph is not None:
        # Async dispatcher wrapper — gates._corrective_reiter calls
        # this for any tool the LLM emits during the gate-triggered
        # retry. Bound to the current request's `ctx` so dispatcher
        # state (read_files, runbook reads) flows through.
        async def _gate_dispatch(name: str, args: dict) -> str:
            return await _dispatch_tool(name, args, ctx)

        _state_in = {
            "rca":             rca,
            "error_class":     error_class,
            "read_runbooks":   set(read_runbooks),
            "messages":        messages,
            "client":          client,
            "model":           model,
            "tools":           TOOLS,
            "tool_dispatch":   _gate_dispatch,
            "build_meta":      build_meta,
            "failure_signals": list(_failure_signals),
            "retry_budget":    1,
            "tokens_in":       0,
            "tokens_out":      0,
            "tool_call_count": 0,
        }
        try:
            # recursion_limit=25 caps total node visits. With 4 gate
            # nodes + 1 reiter + conditional edges, a healthy run uses
            # ≤ 10 visits. Setting low prevents the 10007 runaway seen
            # on the create-quick-infra 42 case when the LLM kept
            # producing retries that re-tripped the same gate. The
            # _route_after_reiter edge already breaks the loop when
            # retry_budget=0; this cap is belt-and-suspenders.
            _state_out = _gate_graph.invoke(
                _state_in, config={"recursion_limit": 25},
            )
            rca               = _state_out.get("rca", rca)
            messages[:]       = _state_out.get("messages", messages)
            read_runbooks    |= set(_state_out.get("read_runbooks") or set())
            total_in         += _state_out.get("tokens_in", 0)
            total_out        += _state_out.get("tokens_out", 0)
            tool_call_count  += _state_out.get("tool_call_count", 0)
            for _s in (_state_out.get("failure_signals") or []):
                if _s not in _failure_signals:
                    _failure_signals.append(_s)
            _skip_imperative_gates = True
            _log(f"gates(LangGraph): ran OK, "
                 f"final_class={rca.get('error_class')}")
        except Exception as _e:
            _log(f"gates(LangGraph) invocation failed, fall through to "
                 f"imperative stack: {_e}")
            _skip_imperative_gates = False
    # Phase 4 — END (LangGraph). When _skip_imperative_gates is False,
    # the original imperative stack below runs unchanged (RAG-branch
    # parity). When True, _final_class is forced to a sentinel so no
    # imperative check matches and no second retry fires.

    _MANDATORY_RUNBOOK_CLASSES = {
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
        # Added Phase-7 (May 2026): runbooks now exist for these
        # classes; enforce the same "must read the runbook before
        # finalizing" gate so the Action template is applied.
        "scm",
        "parse_error",
        "java_runtime",
        "timeout",
        "network",
        "dependency",
        "ssm",
    }
    # Phase 6 — when the LangGraph gates already ran, force-skip the
    # imperative checks (sentinel class never matches any gate).
    _final_class = (
        None if _skip_imperative_gates
        else (rca.get("error_class") or error_class)
    )
    _ultimatum_reason = None
    # Compliance hallucination check (added 2026-05-22 after Stagger
    # Prod+1 build 5225 regression). When class=compliance, the LLM
    # must ground its claim in the pre-fetched `jira.tickets` state.
    # Detect the most common hallucination: claiming a status is NOT
    # in the allowed list when the pre-fetched ticket status IS in the
    # allowed list. Heuristic: if root_cause or Action mentions both
    # the ticket key AND a phrase like "status ... not in" / "not in
    # the allowed list" / "is not acceptable" / "must be one of" AND
    # the pre-fetched ticket has a status that's in {READY FOR
    # RELEASE, HOT FIX}, force a retry with the pre-fetched state
    # surfaced explicitly.
    if _final_class == "compliance" and _ultimatum_reason is None:
        _sf = rca.get("suggested_fix") or {}
        _root = (rca.get("root_cause") or "")
        _action_text = (_sf.get("Action") or "") if isinstance(_sf, dict) else (
            _sf if isinstance(_sf, str) else "")
        _combined = (_root + "\n" + _action_text).lower()
        _suspect_phrases = [
            "not in the allowed list",
            "not in allowed list",
            "is not acceptable",
            "is not in", "not in [",
            "must be one of",
        ]
        _has_suspect = any(p in _combined for p in _suspect_phrases)
        # Check primer for a jira.tickets block with a status that's a
        # known-good "READY FOR RELEASE" or "HOT FIX" value. The primer
        # was concatenated into the system message at iter 0; grep the
        # message history rather than re-fetching.
        _jira_ok_status = False
        for _m in messages:
            if not isinstance(_m, dict):
                continue
            _content = _m.get("content") or ""
            if not isinstance(_content, str):
                continue
            if "jira.tickets" in _content and (
                '"READY FOR RELEASE"' in _content or
                '"HOT FIX"' in _content or
                "'READY FOR RELEASE'" in _content or
                "'HOT FIX'" in _content
            ):
                _jira_ok_status = True
                break
        if _has_suspect and _jira_ok_status:
            _failure_signals.append("compliance_status_hallucination")
            _ultimatum_reason = (
                "Your Action / root_cause says the Jira ticket status "
                "is NOT in the allowed list — but the pre-fetched "
                "`jira.tickets` block in the primer shows the ticket "
                "status IS one of {READY FOR RELEASE, HOT FIX} (which "
                "ARE the allowed values per the compliance runbook "
                "Mode 3). Re-read the `## jira.tickets` JSON block in "
                "the system message, find the actual `status` field "
                "value, and re-emit the RCA. The classifier hint was "
                "`compliance` because the log contains `Compliance:` "
                "info banners — those are POSITIVE status messages "
                "(passing builds emit them), NOT failure signals. "
                "Look at the BOTTOM of the log for the actual fatal "
                "line and re-classify. Most common true cause when "
                "compliance passed: `build_tool_crash` (Gradle daemon "
                "disappeared), `dependency`, `java_runtime`, or "
                "`unknown`."
            )
    if _ultimatum_reason is None and _final_class in _MANDATORY_RUNBOOK_CLASSES and _final_class not in read_runbooks:
        _ultimatum_reason = (
            f"You did not read the runbook for `{_final_class}`. "
            f"Call `read_runbook('{_final_class}')` now, then re-emit "
            f"the FULL RCA JSON applying its drill plan + action "
            f"template. The runbook is the authoritative recipe for "
            f"this class — without it your Action block is missing "
            f"the structure operators depend on."
        )
    elif _final_class == "jenkins_agent_offline":
        _action = ""
        _sf = rca.get("suggested_fix") or {}
        if isinstance(_sf, dict):
            _action = (_sf.get("Action") or "")
        elif isinstance(_sf, str):
            _action = _sf
        _action_upper = _action.upper()
        if "PRIMARY" not in _action_upper or "SECONDARY" not in _action_upper:
            _ultimatum_reason = (
                "Class is `jenkins_agent_offline` but your Action "
                "block does not split into PRIMARY (agent health: "
                "investigate the slave that bounced, restart agent, "
                "check infra) and SECONDARY (pipeline-code hardening: "
                "refactor the helper to drop non-Serializable retained "
                "objects). Both are required by the runbook template "
                "— a code-only fix won't prevent the next bounce, and "
                "an infra-only fix won't prevent the next "
                "NotSerializableException. Re-emit the FULL RCA JSON "
                "with both PRIMARY and SECONDARY sections in Action."
            )
    if _ultimatum_reason is not None:
        _failure_signals.append("ultimatum_gate_triggered")
        _log(f"ultimatum gate: {_ultimatum_reason[:200]}")
        messages.append({"role": "user", "content": _ultimatum_reason})
        # Allow ONE tool call iteration first (so LLM can fetch the
        # runbook), then force-final. Keep cost bounded.
        _retry_kwargs = {
            "model": model, "messages": messages,
            "tools": TOOLS, "tool_choice": "auto",
            "temperature": 0.1,
        }
        _trace("ULTIMATUM GATE — FETCH",
               _fmt_request_payload(messages, _retry_kwargs))
        try:
            retry = client.chat.completions.create(**_retry_kwargs)
            total_in += retry.usage.prompt_tokens
            total_out += retry.usage.completion_tokens
            _retry_msg = retry.choices[0].message
            # If LLM called a tool (likely read_runbook), execute it
            # and feed the result back, then ask for the final JSON.
            if _retry_msg.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": _retry_msg.content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name,
                                      "arguments": tc.function.arguments}}
                        for tc in _retry_msg.tool_calls
                    ],
                })
                for tc in _retry_msg.tool_calls:
                    tool_call_count += 1
                    try:
                        _args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        _args = {}
                    _result = await _dispatch_tool(tc.function.name, _args, ctx)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _result,
                    })
                    # Update read_runbooks set if this was a successful fetch
                    if tc.function.name == "read_runbook":
                        _rb_name = _args.get("name")
                        if _rb_name and "not found" not in _result:
                            read_runbooks.add(_rb_name)
            # Now force-final the JSON
            messages.append({
                "role": "user",
                "content": (
                    "Now emit the FULL updated RCA JSON applying the "
                    "runbook's drill plan + action template. Required "
                    "keys: summary, failed_stage, error_class, "
                    "root_cause, evidence, suggested_fix "
                    "(Finding/Action/Verify), suggested_commands. "
                    "Return ONLY the JSON object."
                ),
            })
            _final_kwargs = {
                "model": model, "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }
            _trace("ULTIMATUM GATE — FINALIZE",
                   _fmt_request_payload(messages, _final_kwargs))
            final2 = client.chat.completions.create(**_final_kwargs)
            total_in += final2.usage.prompt_tokens
            total_out += final2.usage.completion_tokens
            _final2_text = final2.choices[0].message.content
            _trace("ULTIMATUM GATE — FINALIZE RESPONSE",
                   f"prompt_tokens={final2.usage.prompt_tokens} "
                   f"completion_tokens={final2.usage.completion_tokens}\n"
                   f"content={(_final2_text or '')[:1500]}")
            _rca_final2 = _parse_final_json(_final2_text)
            if _rca_final2 is not None:
                # Backfill required keys again in case the retry was
                # incomplete.
                for _k, _v in _defaults.items():
                    if _k not in _rca_final2:
                        _rca_final2[_k] = _v
                if "suggested_commands" not in _rca_final2:
                    _rca_final2["suggested_commands"] = []
                rca = _rca_final2
                final_text = _final2_text
        except Exception as _e:
            _log(f"ultimatum gate retry failed: {_e}")

    # Phase-10 (revised May 2026): annotate-only stayed too lenient.
    # Build 15 Stagger Scaling case had evidence citing
    # `vars/discoverBlueTargetGroup.groovy` — a file that doesn't exist;
    # snippet-fill set `_error: file read failed` but the entry stayed in
    # the final JSON, polluting evidence and burying the one real
    # citation. New policy: SNIPPET-FILL still runs as a schema contract,
    # then HARD-DROP entries that are objectively invalid:
    #   (a) evidence with `_error` field after snippet-fill — file/range
    #       was unreadable, the citation is broken regardless of intent
    #   (b) repo-source entry whose `<repo>/<path>` is NOT in `read_files`
    #       — LLM cited a file it never opened via repo_read_file (and
    #       therefore cannot have verified)
    #   (c) repo-source entry whose snippet contains quoted-string
    #       literals that do NOT appear in the actual file content
    # Each drop emits a failure_signal so the audit log + dashboard see
    # it. We do NOT substitute, we do NOT rewrite — broken entries are
    # removed, leaving the LLM's real, verifiable citations in place.
    rca["evidence"] = _fill_repo_snippets(rca.get("evidence", []))
    # (a) drop _error entries from snippet-fill
    _before = len(rca["evidence"])
    rca["evidence"] = [e for e in rca["evidence"]
                       if not (isinstance(e, dict) and e.get("_error"))]
    _err_drops = _before - len(rca["evidence"])
    if _err_drops:
        _failure_signals.append("hallucinated_file_evidence")
        _log(f"  evidence: dropped {_err_drops} entry(ies) with _error field "
             f"(unreadable file/range — LLM cited fictional path)")
    # (b) drop repo-source entries citing files never opened via repo_read_file
    _before = len(rca["evidence"])
    rca["evidence"] = _filter_fake_repo_evidence(rca["evidence"], read_files)
    if len(rca["evidence"]) < _before:
        _failure_signals.append("hallucinated_file_evidence")
    # (c) drop entries with snippets that don't match file content
    rca["evidence"], _snip_drops = _filter_hallucinated_snippets(
        rca["evidence"], read_files
    )
    if _snip_drops:
        _failure_signals.append("hallucinated_snippet")
    # (d) drop runbook citations for runbooks never fetched
    rca["evidence"], _rb_drops = _drop_unfetched_runbook_evidence(
        rca["evidence"], read_runbooks
    )
    if _rb_drops:
        _failure_signals.append("runbook_evidence_dropped")
    if len(rca.get("evidence", []) or []) < 3:
        _failure_signals.append("low_evidence_count")

    # Phase 7 — Real-ID auto-substitution for slave placeholders.
    # The prompt rule asks the LLM to call `jenkins_node_info` before
    # emitting `<slave-instance-id>` in a cmd. When the LLM ignores
    # the rule (observed on Build 15 retry), substitute server-side:
    # grep `slave-\d+` from the log_window, resolve via
    # jenkins.get_node_info, swap the placeholder in every cmd. If
    # the lookup returns no instance_id, DROP the offending cmd
    # entirely (server-side enforcement of "no placeholder ever
    # ships to operator").
    try:
        import re as _re_phase7
        # Find slave name in log window. Most common shapes:
        # `slave-4 seems to be removed`, `Running on slave-4`.
        _slave_match = _re_phase7.search(
            r"\bslave-\d+\b|\bagent-\w+\b", log_window[-30000:] or ""
        )
        _has_slave_placeholder = any(
            isinstance(c, dict)
            and "<slave-instance-id>" in (c.get("cmd") or "")
            for c in (rca.get("suggested_commands") or [])
        )
        if _slave_match and _has_slave_placeholder:
            from . import jenkins as _jenkins_api_phase7
            slave_name = _slave_match.group(0)
            _log(f"Phase-7 cmd-substitution: log mentions {slave_name}, "
                 f"final RCA has <slave-instance-id> placeholder — "
                 f"calling jenkins_node_info to substitute")
            try:
                info = await _jenkins_api_phase7.get_node_info(
                    slave_name, jenkins_url, jenkins_auth,
                )
            except Exception as _e:
                info = {"error": str(_e)}
            real_id = info.get("instance_id") if isinstance(info, dict) else None
            new_cmds = []
            for _c in (rca.get("suggested_commands") or []):
                if not isinstance(_c, dict):
                    new_cmds.append(_c)
                    continue
                _cmd = _c.get("cmd") or ""
                if "<slave-instance-id>" not in _cmd:
                    new_cmds.append(_c)
                    continue
                if real_id:
                    _new = dict(_c)
                    _new["cmd"] = _cmd.replace("<slave-instance-id>",
                                               real_id)
                    _existing_rationale = (_new.get("rationale") or "")
                    _new["rationale"] = (
                        f"[server resolved slave={slave_name} → "
                        f"{real_id} via jenkins_node_info] "
                        f"{_existing_rationale}"
                    ).strip()
                    new_cmds.append(_new)
                    _failure_signals.append("server_substituted_slave_id")
                else:
                    # Lookup failed — drop the placeholder cmd + add a
                    # fallback ops-action so the operator still has SOME
                    # next-step instead of an empty suggested_commands.
                    _failure_signals.append("server_dropped_unresolvable_slave_cmd")
                    _log(f"  dropped cmd (no real_id for {slave_name}): "
                         f"{_cmd[:120]!r}")
            # #1 fallback: if at least one cmd was dropped AND no cmds
            # survived AND the slave couldn't be resolved → append a
            # diagnostic ops-contact cmd so the suggested_commands
            # array isn't empty.
            if (not real_id and
                    "server_dropped_unresolvable_slave_cmd" in _failure_signals
                    and not new_cmds):
                _cause = ""
                if isinstance(info, dict):
                    _cause = (
                        info.get("offline_cause")
                        or info.get("error")
                        or "node not registered with Jenkins"
                    )
                new_cmds.append({
                    "cmd": (
                        f"# Contact #devops with this name. The Jenkins "
                        f"node API returned no instance_id for "
                        f"`{slave_name}` ({_cause}). Operator must look "
                        f"up the EC2 instance via the AWS console (tag "
                        f"`jenkins-node` = `{slave_name}`) or restart "
                        f"the agent process on the slave host."
                    ),
                    "tier": "safe",
                    "rationale": (
                        f"[server fallback: jenkins_node_info({slave_name}) "
                        f"unresolved] Slave node is gone from Jenkins; "
                        f"agent has no path to derive its EC2 instance ID. "
                        f"Operator-led discovery required."
                    ),
                })
            rca["suggested_commands"] = new_cmds
    except Exception as _e:
        _log(f"Phase-7 slave-substitution skipped: {_e}")

    # Detect hallucinated/placeholder IDs in suggested_commands +
    # auto-bump tier for state-mutating terraform commands. Phase-10
    # policy: do NOT rewrite cmd strings, but annotating rationale
    # surfaces the issue to the operator (rationale is the operator-
    # visible context, not the literal command). Tier correction is a
    # categorization fix, not a value substitution.
    _cmd_signals = _check_suggested_commands(rca.get("suggested_commands", []))
    _failure_signals.extend(_cmd_signals)

    rca["tokens_used"] = {"input": total_in, "output": total_out}
    rca["agent_tool_calls"] = tool_call_count
    rca["files_read"] = sorted(read_files)
    _log(f"done. tool_calls={tool_call_count} tokens={total_in}+{total_out} read_files={len(read_files)}")
    _final_cost = total_in * in_per_tok + total_out * out_per_tok
    _trace("FINAL OUTPUT",
           f"tool_calls={tool_call_count} tokens={total_in}+{total_out} "
           f"cost=${_final_cost:.4f} "
           f"files_read={sorted(read_files)}\n"
           f"final_text=\n{(final_text or '')[:3000]}")

    # Prepend a compact RCA SUMMARY block at the top of the trace file so
    # readers don't have to scroll 1500 lines to learn what happened. Body
    # iter-by-iter detail stays untouched below.
    if _trace_enabled:
        _prepend_summary_header(
            _trace_paths, _iter_log, sorted(read_files), tool_call_count,
            total_in, total_out, _final_cost, rca,
        )

    # Per-RCA outcome row for measurement-before-ship analysis. Derive
    # aws_apis / runbooks from the iter log (already structured). Failure
    # signals are deterministic events appended at occurrence — see the
    # vocabulary in outcome_log.py header. value_validator_substituted is
    # NOT yet recorded here: that validator runs in main.py AFTER
    # run_agent returns, so it appends its own signal via the rca dict.
    _aws_apis: list[str] = []
    _runbooks: list[str] = []
    for _entry in _iter_log:
        for _name, _args_json in _entry["tools_chosen"]:
            try:
                _args = json.loads(_args_json) if isinstance(_args_json, str) else _args_json
            except Exception:
                _args = {}
            if _name == "aws_describe":
                _aws_apis.append(f"{_args.get('service','?')}.{_args.get('operation','?')}")
            elif _name == "read_runbook":
                _runbooks.append(_args.get("name", "?"))
    # Surface signals on the rca dict so main.py can append validator
    # signals + re-log if it wants. Idempotent if main.py re-runs log.
    rca["failure_signals"] = list(dict.fromkeys(_failure_signals))
    outcome_log.log(
        job=job, build=int(build), service=service, model=model, rca=rca,
        iters=len(_iter_log), tool_calls=tool_call_count,
        tokens_in=total_in, tokens_out=total_out, cost_usd=_final_cost,
        files_read=sorted(read_files), aws_apis=_aws_apis, runbooks=_runbooks,
        failure_signals=rca["failure_signals"],
        trace_path=(_per_build_path if _trace_enabled else None),
    )

    return rca


def _prepend_summary_header(trace_paths: list[str], iter_log: list[dict],
                            files_read: list[str], tool_calls: int,
                            tok_in: int, tok_out: int, cost: float,
                            rca: dict) -> None:
    """Build the SUMMARY block and splice it after the trace header line.

    Each iter shows: tool count, finish_reason, reasoning narration,
    and the tool calls the LLM chose (compact one-per-line). Tail lists
    files_read / runbooks / aws_apis derived from the iter log so the
    reader sees the full surgical-read footprint at a glance.
    """
    aws_apis: list[str] = []
    runbooks: list[str] = []
    for entry in iter_log:
        for name, args_json in entry["tools_chosen"]:
            try:
                args = json.loads(args_json) if isinstance(args_json, str) else args_json
            except Exception:
                args = {}
            if name == "aws_describe":
                svc = args.get("service", "?")
                op = args.get("operation", "?")
                aws_apis.append(f"{svc}.{op}")
            elif name == "read_runbook":
                runbooks.append(args.get("name", "?"))

    cause = (rca.get("root_cause") or "")[:200] if isinstance(rca, dict) else ""
    cls = (rca.get("error_class") or "?") if isinstance(rca, dict) else "?"

    lines = []
    lines.append("=== RCA SUMMARY ===")
    lines.append(
        f"iters={len(iter_log)}  tool_calls={tool_calls}  "
        f"tokens={tok_in}+{tok_out}  cost=${cost:.4f}  "
        f"result={cls}"
    )
    if cause:
        lines.append(f"cause: {cause}")
    lines.append("")
    for entry in iter_log:
        tc_count = len(entry["tools_chosen"])
        header = (
            f"Iter {entry['iter']} [{tc_count} tool{'s' if tc_count != 1 else ''}, "
            f"tokens={entry['in']}+{entry['out']}, finish={entry['finish']}]"
        )
        lines.append(header)
        reasoning = entry["reasoning"]
        if reasoning:
            # First 240 chars of reasoning narration, single line
            r = reasoning.replace("\n", " ").strip()[:240]
            lines.append(f"  reasoning: {r}")
        for name, args_json in entry["tools_chosen"]:
            try:
                args = json.loads(args_json) if isinstance(args_json, str) else args_json
            except Exception:
                args = args_json
            # Compact tool arg display per tool type
            if name == "repo_read_file":
                display = f"{args.get('repo','?')}/{args.get('path','?')} [{args.get('start','?')}-{args.get('end','?')}]"
            elif name == "aws_describe":
                display = f"{args.get('service','?')}.{args.get('operation','?')} @ {args.get('aws_account','?')}/{args.get('aws_region','?')}"
            elif name == "read_runbook":
                display = args.get("name", "?")
            elif name == "get_jenkins_job_config":
                display = args.get("job", "?")
            elif name in ("jira_get_ticket", "jira_search"):
                display = str(args)[:120]
            elif name.startswith("github_"):
                display = str(args)[:120]
            elif name in ("repo_search", "repo_find_function", "repo_recent_commits"):
                display = str(args)[:120]
            else:
                display = str(args)[:120]
            lines.append(f"  → {name}({display})")
        lines.append("")
    lines.append(f"files_read[{len(files_read)}]: {files_read}")
    lines.append(f"aws_apis[{len(aws_apis)}]: {aws_apis}")
    lines.append(f"runbooks[{len(runbooks)}]: {runbooks}")
    lines.append("=== END SUMMARY ===")
    lines.append("")
    summary = "\n".join(lines)

    for p in trace_paths:
        try:
            with open(p) as f:
                body = f.read()
            # Splice after the first === AGENT TRACE === line so the run header stays first.
            nl = body.find("\n")
            if nl == -1:
                new_body = summary + "\n" + body
            else:
                new_body = body[: nl + 1] + "\n" + summary + body[nl + 1:]
            with open(p, "w") as f:
                f.write(new_body)
        except Exception:
            pass


_REPO_PREFIXES = ("jenkins_pipeline/", "InfraComposer/")


def _fill_repo_snippets(evidence: list) -> list:
    """Server-side snippet filler — the schema contract for repo-file
    evidence is {source: '<repo>/<file>', line_start, line_end}. LLM
    emits coordinates only; this function reads the file from disk
    and injects the verbatim text as `snippet`.

    Why it exists:
      The LLM cannot hallucinate code it does not write. By forcing
      the LLM to emit ONLY coordinates and letting the server pull
      the bytes, snippet text is guaranteed to be a literal slice of
      the cited file at the cited line range.

    Rules:
      - Operates on evidence items whose `source` starts with
        'jenkins_pipeline/' or 'InfraComposer/'. Non-repo evidence
        is passed through unchanged (LLM still emits `snippet`
        verbatim for those, per the system-prompt schema).
      - Reads the file from the local clone under
        $JENKINS_RCA_REPOS_DIR (defaults to /home/ubuntu/project/Jenkins-rca/repos).
      - Strips the optional `:<line>` suffix from `source` if the
        LLM left one — we treat it as legacy and replace with the
        line range fields below.
      - If `line_start` / `line_end` are missing or invalid, leaves
        the item unchanged so the operator can see the malformed
        emission. We do NOT silently swallow LLM mistakes; signal
        them in the response.
      - If the file cannot be read, leaves the item unchanged and
        records an `_error` field in the entry.
    """
    if not isinstance(evidence, list):
        return evidence
    repos_dir = Path(os.environ.get("JENKINS_RCA_REPOS_DIR", "/home/ubuntu/project/Jenkins-rca/repos"))
    out = []
    for item in evidence:
        if not isinstance(item, dict):
            out.append(item)
            continue
        src = (item.get("source") or "").strip()
        if not any(src.startswith(p) for p in _REPO_PREFIXES):
            out.append(item)
            continue
        # Strip legacy :<line> suffix; line range comes from explicit fields.
        path_part = src.rsplit(":", 1)[0] if ":" in src else src
        try:
            repo, sub = path_part.split("/", 1)
        except ValueError:
            out.append(item)
            continue
        line_start = item.get("line_start")
        line_end = item.get("line_end")
        # Normalise integer-shaped strings if the LLM stringified them.
        try:
            line_start = int(line_start) if line_start is not None else None
            line_end = int(line_end) if line_end is not None else None
        except (TypeError, ValueError):
            line_start = line_end = None
        if line_start is None or line_end is None or line_start < 1 or line_end < line_start:
            item = dict(item)
            item.setdefault("_error", "missing or invalid line_start/line_end")
            out.append(item)
            continue
        try:
            file_path = repos_dir / repo / sub
            lines = file_path.read_text(errors="replace").splitlines()
        except Exception as e:
            item = dict(item)
            item["_error"] = f"file read failed: {e}"
            out.append(item)
            continue
        # Clamp to file bounds; snippet is the joined slice with line numbers
        # prefixed (matches the format repo_read_file uses, so the LLM and
        # operator see the same rendering).
        a = max(1, line_start)
        b = min(len(lines), line_end)
        if b < a:
            item = dict(item)
            item["_error"] = "line range out of file bounds"
            out.append(item)
            continue
        snippet = "\n".join(f"{i}: {lines[i - 1]}" for i in range(a, b + 1))
        new = dict(item)
        # Normalise: keep source as bare repo/file (no :<line>), expose
        # both the requested range AND the actual filled snippet.
        new["source"] = path_part
        new["line_start"] = a
        new["line_end"] = b
        new["snippet"] = snippet
        out.append(new)
    return out


def _drop_unfetched_runbook_evidence(evidence: list, read_runbooks: set[str]) -> tuple[list, int]:
    """Drop evidence entries citing docs/runbooks/<name>.md that were never fetched.

    If read_runbook(name) returned "not found", the LLM has no tool-result
    to quote from — any snippet in those entries is fabricated. Drop them.
    Non-runbook sources pass through unchanged.
    """
    if not isinstance(evidence, list):
        return evidence, 0
    kept = []
    dropped = 0
    for item in evidence:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        src = (item.get("source") or "").strip()
        if not src.startswith("docs/runbooks/"):
            kept.append(item)
            continue
        name = src[len("docs/runbooks/"):]
        if name.endswith(".md"):
            name = name[:-3]
        if name in read_runbooks:
            kept.append(item)
        else:
            dropped += 1
            _log(f"  evidence: dropped unfetched runbook cite source={src!r} (runbook was not found)")
    return kept, dropped


# Hallucinated/placeholder ID patterns commonly emitted by the LLM in
# suggested_commands when the agent could not derive a real ID from
# tool calls. Each one is a giveaway that the LLM made up an ID rather
# than failing safely (the latter is the desired behavior — see Option
# 0 in the system prompt: when aws_describe returns NotFound, propose
# a pipeline re-run instead of fabricating IDs to delete/import).
_HALLUCINATED_ID_PATTERNS = [
    # Generic angle-bracket placeholder: catches <arn>, <tg_id>,
    # <instance-id>, <real-arn-from-aws_describe>, <your-account-id>,
    # <existing-id>, etc. Any <word-chars-and-dashes> inside angle
    # brackets in a cmd field is suspicious — the LLM didn't fill it.
    re.compile(r"<[A-Za-z][\w./-]*>"),
    # Specific fake-but-plausible numeric patterns
    re.compile(r"\b1234567[89]?0?123456\b"),                            # 1234567890123456 sequential
    re.compile(r"\b1234abcd\w*\b", re.IGNORECASE),                      # 1234abcd... fake hex
    re.compile(r"\b(?:0{12,}|f{12,})\b", re.IGNORECASE),                # all-zeros, all-f
    re.compile(r"\b(?:placeholder|EXAMPLE|XXXXXXXX|YOURACCOUNT|YOUR_ACCOUNT)\b", re.IGNORECASE),
    re.compile(r"\b(?:abcdef[0-9a-f]{6,}|123456[0-9a-f]{6,})\b"),       # fake hex digests
    re.compile(r"\bi-(?:0{8,}|1234567)\b"),                             # i-0000... / i-1234567
]


# Terraform commands that mutate state — must always be tier=restricted.
_TF_RESTRICTED_RE = re.compile(
    r"\bterraform\s+(import|state\s+\w+|apply|destroy|taint|untaint|workspace\s+(delete|new))\b",
    re.IGNORECASE,
)


def _check_suggested_commands(commands: list) -> list[str]:
    """Validate + annotate suggested_commands in-place.

    Returns a list of failure_signals discovered. Mutates each command
    dict to:
      - Bump `tier` to "restricted" for state-mutating terraform cmds
        (categorization correction — these are not "safe").
      - Prepend a WARNING to `rationale` when the cmd contains a
        hallucinated/placeholder ID. Does NOT touch the `cmd` field
        (Phase-10 policy: never silently substitute LLM output values).
    """
    signals: list[str] = []
    if not isinstance(commands, list):
        return signals
    for entry in commands:
        if not isinstance(entry, dict):
            continue
        cmd = entry.get("cmd", "")
        if not isinstance(cmd, str):
            continue

        # 1. Hallucinated ID detection
        for pat in _HALLUCINATED_ID_PATTERNS:
            m = pat.search(cmd)
            if m:
                signals.append("hallucinated_id_in_command")
                rationale = entry.get("rationale", "")
                warn = (
                    f"[VALIDATOR WARN: cmd contains placeholder/likely-"
                    f"hallucinated value '{m.group(0)}' — verify before "
                    f"running; the agent could not derive the real ID]"
                )
                entry["rationale"] = f"{warn} {rationale}".strip()
                break

        # 2. Terraform state-mutating tier bump
        if _TF_RESTRICTED_RE.search(cmd) and entry.get("tier") != "restricted":
            signals.append("tier_autobumped_terraform_restricted")
            entry["tier"] = "restricted"

    return signals


def _filter_fake_repo_evidence(evidence: list, read_files: set[str]) -> list:
    """Drop evidence entries whose source is a repo path not actually read.

    A repo-path source looks like `jenkins_pipeline/vars/foo.groovy:42`.
    Strip the `:<line>` part; if `<repo>/<path>` is NOT in `read_files`,
    the LLM made it up — drop the entry. Non-repo sources
    (`jenkins_log`, `jira.tickets`, `build_meta`) pass through unchanged.
    """
    if not isinstance(evidence, list):
        return evidence
    kept = []
    for item in evidence:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        src = (item.get("source") or "").strip()
        if not any(src.startswith(p) for p in _REPO_PREFIXES):
            kept.append(item)
            continue
        # Strip the :<line> suffix if present
        path_part = src.rsplit(":", 1)[0] if ":" in src else src
        if path_part in read_files:
            kept.append(item)
        else:
            _log(f"  evidence validator: dropped fake cite source={src!r} "
                 f"(not in read_files)")
    return kept


_QUOTED_STRING_RE = re.compile(
    r"(?<!\\)(?:'([^'\n]{3,80})'|\"([^\"\n]{3,80})\")"
)


def _filter_hallucinated_snippets(evidence: list, read_files: set[str]) -> tuple[list, int]:
    """Drop evidence whose snippet text isn't actually in the cited file.

    LLM sometimes invents snippets that look like real Groovy/Java
    code (matching the file's TOPIC) but the literal characters do
    not appear in the file. The file-path check in
    _filter_fake_repo_evidence misses this because the file path is
    real — only the snippet content is fabricated.

    Validation strategy — quoted string literals as the anchor:
      For each evidence entry whose source is `<repo>/<path>[:line]`:
        1. Extract single-quoted and double-quoted string literals
           (3-80 chars) from the snippet.
        2. For each quoted literal, check whether the LITERAL
           characters (with quotes) appear in the file content.
        3. If ANY quoted literal in the snippet is NOT present in
           the file → snippet is hallucinated. Drop the entry.
        4. If the snippet has no quoted literals (rare for code),
           fall back to a 30-char window match: at least one 30-char
           run from the snippet (whitespace-normalised) must appear
           in the file content.
      Non-repo sources (jenkins_log, aws:..., etc.) pass through.

    Returns (filtered_evidence, dropped_count).
    """
    if not isinstance(evidence, list):
        return evidence, 0

    # Cache file contents we read so multiple evidence entries for the
    # same file only hit disk once.
    file_cache: dict[str, str] = {}
    repos_dir = Path(os.environ.get("JENKINS_RCA_REPOS_DIR", "/home/ubuntu/project/Jenkins-rca/repos"))

    def _load_file(repo_path: str) -> str | None:
        if repo_path in file_cache:
            return file_cache[repo_path]
        try:
            parts = repo_path.split("/", 1)
            if len(parts) != 2:
                return None
            repo, sub = parts
            p = repos_dir / repo / sub
            content = p.read_text(errors="replace")
            file_cache[repo_path] = content
            return content
        except Exception:
            return None

    def _snippet_matches_file(snippet: str, content: str) -> bool:
        if not snippet or not content:
            return True   # cannot verify — allow
        # 1. Quoted string literal check (strict).
        quoted_pairs = _QUOTED_STRING_RE.findall(snippet)
        quoted_literals = []
        for s_match, d_match in quoted_pairs:
            literal = s_match or d_match
            if not literal:
                continue
            # Skip pure placeholder tokens
            if literal.strip() in {"...", "…", ""}:
                continue
            quoted_literals.append(literal)
        if quoted_literals:
            # All non-placeholder literals must appear in file content.
            for lit in quoted_literals:
                if lit not in content:
                    return False
            return True
        # 2. No quoted literals — fall back to 30-char window match.
        s = re.sub(r"\s+", " ", snippet).strip()
        c_norm = re.sub(r"\s+", " ", content)
        if len(s) <= 30:
            return s in c_norm
        for i in range(0, len(s) - 30 + 1, 5):
            if s[i:i + 30] in c_norm:
                return True
        return False

    kept = []
    dropped = 0
    for item in evidence:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        src = (item.get("source") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if not any(src.startswith(p) for p in _REPO_PREFIXES):
            kept.append(item)
            continue
        path_part = src.rsplit(":", 1)[0] if ":" in src else src
        if path_part not in read_files:
            # Already would-be-dropped by _filter_fake_repo_evidence.
            kept.append(item)
            continue
        content = _load_file(path_part)
        if content is None:
            # Can't verify — keep, don't penalise on infra issues.
            kept.append(item)
            continue
        if _snippet_matches_file(snippet, content):
            kept.append(item)
        else:
            dropped += 1
            _log(f"  snippet validator: dropped fabricated cite "
                 f"source={src!r} snippet={snippet[:120]!r}")
    return kept, dropped


def _parse_final_json(text: str | None) -> dict | None:
    """Tolerantly parse the agent's final response.

    Handles three real-world shapes:
      1. Pure JSON object — `json.loads` directly.
      2. Markdown-wrapped JSON — ```json\n{...}\n``` or ```\n{...}\n```.
      3. Trailing/leading prose — find the largest `{...}` substring.
    """
    if not text:
        return None
    s = text.strip()
    # 1. Direct parse
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    # 2. Strip code fences
    if s.startswith("```"):
        inner = s[3:].lstrip()
        # optional 'json' tag
        if inner.lower().startswith("json"):
            inner = inner[4:].lstrip()
        if inner.endswith("```"):
            inner = inner[:-3].rstrip()
        try:
            v = json.loads(inner)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    # 3. Pull out first `{...}` block by brace matching
    first = s.find("{")
    last = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = s[first:last + 1]
        try:
            v = json.loads(candidate)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    return None


def _elide_old_tool_results(messages: list, *, current_iter: int, keep_recent: int) -> None:
    """Replace tool-result bodies from iterations older than (current - keep_recent)
    with a short placeholder. Preserves the assistant→tool message structure so
    OpenAI still threads the tool_call_id chain, just drops the heavy content.

    Each agent iteration appends 1 assistant message + N tool messages. We
    walk the message list, and for any `role==tool` whose position is older
    than the cutoff, swap its content for `[elided to save tokens — see
    earlier reasoning]`.

    Cheap heuristic: each iteration's tool messages are clustered after an
    assistant message. We count assistant messages backwards and elide any
    tool messages that belong to assistant turns older than the cutoff.
    """
    if current_iter < keep_recent:
        return
    # Walk backwards, counting assistant turns. Once we've passed
    # `keep_recent` assistant turns, elide subsequent (older) tool messages.
    assistant_seen = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            assistant_seen += 1
            continue
        if role == "tool" and assistant_seen >= keep_recent:
            if not (m.get("content") or "").startswith("[elided"):
                m["content"] = "[elided to save tokens — see earlier reasoning]"


async def _build_primer(
    job: str, build: int, service: str, error_class: str,
    build_meta: dict, log_window: str, initial_tool_ctx: str,
) -> str:
    """Compose the static primer block that becomes part of the system message.

    Two modes:

    Option C (JENKINS_RCA_FORCE_AGENT_MODE=1) — MINIMAL primer:
      Boot-pack blocks + (since 2026-05-22) jira.tickets pre-fetch when
      ticket keys are present in log. LLM MUST use tools to fetch
      everything else. error_class hint and detected_failed_stage are
      dropped so the LLM classifies + identifies the failed stage from
      the log markers itself.

    Legacy mode (default) — full primer with resolved values + pre-fetched
    blocks. Kept until the Option C path is verified end-to-end and the
    JENKINS_RCA_FORCE_AGENT_MODE default flips to on.

    Async since Jira pre-fetch (and any future API enrichment) requires
    awaiting the underlying HTTP calls.
    """
    # Pre-fetch Jira tickets ANY time keys appear in log. Closes the
    # "compliance hallucination" hole: LLM was emitting `status X not
    # in [X, Y]` without checking the real ticket. With jira.tickets
    # pre-injected, LLM sees the canonical status field + can't claim
    # the ticket is misconfigured when it isn't.
    # Stagger Prod+1 build 5225 case (MPB-1279 was in READY FOR RELEASE
    # but LLM said it wasn't in allowed list).
    jira_block = await _jira_prefetch_for_agent(log_window)

    # Phase 7 #2 — Pre-fetch Jenkins node info when log mentions a
    # slave label. Saves the LLM a tool call AND closes the
    # placeholder hole: with `## jenkins.node` already in scope at
    # iter 0, the LLM has no excuse to ship `<slave-instance-id>`.
    # Both server gate + LLM cooperation now share the same canonical
    # resolution path.
    node_block = await _jenkins_node_prefetch_for_agent(log_window)

    if os.environ.get("JENKINS_RCA_FORCE_AGENT_MODE"):
        # MINIMAL primer — strict boot-pack only.
        _det = build_meta.get("detected_failed_stage", "—")
        parts = [
            "## build_meta",
            f"- job: {job}",
            f"- build: {build}",
            f"- service: {service}",
            f"- result: {build_meta.get('result', '—')}",
            f"- url: {build_meta.get('url', '—')}",
            "",
        ]
        # failed_stage is the one fact we resolve deterministically (locator
        # exit-point line → enclosing `[Pipeline] { (X) }` marker). Even in
        # minimal mode we hand it over: self-identifying the stage from the log
        # made the LLM grab `Running prechecks for stage: X` phase labels of
        # EARLIER phases that actually passed (build #5627: `prodPlusOne`).
        if _det and _det != "—":
            parts += [
                "## VERIFIED failed_stage (deterministic — do not contradict)",
                f"The pipeline aborted inside the `{_det}` stage. Your "
                f"`failed_stage` MUST be `{_det}`. Do NOT lift a stage name "
                "from a `Running prechecks for stage: X` line — that labels the "
                "START of a phase, not the stage that failed.",
                "",
            ]
        parts += [
            "## service.lookup",
            "```json",
            json.dumps(mcp_tools.service_lookup(service), indent=2),
            "```",
            "",
            "## log_window (sanitized — tail of build log, error lives at the bottom)",
            "```",
            log_window[-30000:],  # keep END (where Error: line lives), not start
            "```",
        ]
        if jira_block:
            parts.append("")
            parts.append(jira_block)
        if node_block:
            parts.append("")
            parts.append(node_block)
        # Phase 3 (RAG agent auto-inject) — append top-k semantic matches
        # from the docops + audit corpus.
        rag_block = _rag_inject_for_agent(log_window, error_class)
        if rag_block:
            parts.append("")
            parts.append(rag_block)
        return "\n".join(parts)

    # Legacy primer (full pre-fetched context).
    detected = build_meta.get("detected_failed_stage", "—")
    parts = [
        "## Build context",
        f"- job: {job}",
        f"- build: {build}",
        f"- service: {service}",
        f"- classifier hint: {error_class}",
        f"- detected_failed_stage: {detected}",
        "",
        "## VERIFIED failed_stage (deterministic — do not contradict)",
        f"The pipeline aborted inside the `{detected}` stage. This was computed "
        "by locating the actual exit-point line in the raw log and walking up to "
        "its enclosing `[Pipeline] {{ (<stage>) }}` marker. Your `failed_stage` "
        f"MUST be `{detected}` unless it is `—` (unknown). Do NOT use a stage "
        "name lifted from a `Running prechecks for stage: X` line — that is an "
        "env/phase label logged at the START of a phase, NOT the stage that "
        "failed, and earlier phases often PASS before a later one aborts."
        if detected and detected != "—" else
        "## failed_stage\n(stage could not be determined deterministically — "
        "identify it from the `[Pipeline] { (<stage>) }` marker enclosing the "
        "actual error line, NOT from a `Running prechecks for stage:` label.)",
        "",
        "## RESOLVED VALUES — substitute these VERBATIM in suggested_fix and commands",
        "Pulled from the pre-fetched context blocks below. NEVER write `<placeholder>`, `<log_path>`, `<port>`, etc.",
        _format_resolved_values(initial_tool_ctx),
        "",
        "## Pre-fetched context (do not re-fetch — already in scope)",
        initial_tool_ctx or "(no pre-fetched context)",
        "",
        "## Log window (sanitized)",
        "```",
        log_window[-30000:],  # primer cap — keep END (where Error: line lives)
        "```",
    ]
    if jira_block:
        parts.append("")
        parts.append(jira_block)
    if node_block:
        parts.append("")
        parts.append(node_block)
    # Phase 3 — RAG auto-inject (legacy primer mode too)
    rag_block = _rag_inject_for_agent(log_window, error_class)
    if rag_block:
        parts.append("")
        parts.append(rag_block)
    return "\n".join(parts)


async def _jenkins_node_prefetch_for_agent(log_window: str) -> str:
    """Pre-fetch Jenkins node info when log mentions a slave / agent
    label (e.g. `slave-4`, `agent-prod-1`). Returns a
    `## jenkins.node` markdown block with the API response, or "" if
    no slave found / lookup failed.

    Why pre-fetch: the LLM keeps emitting `bbctl shell
    <slave-instance-id>` despite the prompt rule + the
    jenkins_node_info tool being available. Pre-injecting the
    resolved instance_id at iter 0 means the LLM has the real value
    in context and the placeholder is genuinely without excuse.
    Server gate (Phase 7) is the safety net; this is the primary
    fix.

    Returns "" on any failure (Jenkins env missing, API down, no
    slave in log). Never raises.
    """
    if not log_window:
        return ""
    try:
        import re as _re_node
        m = _re_node.search(r"\bslave-\d+\b|\bagent-[\w-]+\b",
                            log_window[-30000:])
        if not m:
            return ""
        slave_name = m.group(0)
        base = os.environ.get("JENKINS_RCA_JENKINS_URL", "").rstrip("/")
        user = os.environ.get("JENKINS_RCA_JENKINS_USER", "")
        token = os.environ.get("JENKINS_RCA_JENKINS_TOKEN", "")
        if not (base and user and token):
            return ""
        info = await jenkins_api.get_node_info(slave_name, base, (user, token))
        if not isinstance(info, dict):
            return ""
        return (
            f"## jenkins.node (API-fetched ground truth — USE "
            f"`instance_id` in any `bbctl shell` / `aws_describe` "
            f"command, NEVER `<slave-instance-id>`)\n"
            f"```json\n{json.dumps(info, indent=2, default=str)}\n```"
        )
    except Exception as _e:
        _log(f"  jenkins-node pre-fetch skipped: {_e}")
        return ""


async def _jira_prefetch_for_agent(log_window: str) -> str:
    """Pre-fetch Jira ticket(s) for any Jira keys (e.g. MPB-1279,
    FMSCAT-5887) appearing in the log. Returns a `## jira.tickets`
    markdown block with the API response, or "" when no keys or fetch
    fails.

    Why pre-fetch (not let the LLM call jira_get_ticket itself):
      - The LLM has been observed to skip the call AND hallucinate
        the ticket state. Stagger Prod+1 build 5225 case: LLM claimed
        MPB-1279 was in a wrong status without fetching, even though
        the log explicitly showed `Jira status: READY FOR RELEASE`
        and the runbook accepts that status.
      - Pre-injecting canonical fields (status, sign-off SHA, assignee,
        fix versions) makes the LLM's "status check failed" claim
        verifiable against pre-fetched state. Any claim contradicting
        the pre-fetched data is a clear hallucination signal.

    Returns "" on any failure (no keys, Jira unreachable, no API
    credentials). Never raises.
    """
    if not log_window:
        return ""
    try:
        keys = jira_api.extract_tickets(log_window)
        if not keys:
            return ""
        tickets = await jira_api.fetch_all(keys)
        if not tickets:
            return ""
        return (
            "## jira.tickets (API-fetched ground truth — REJECT any "
            "claim that contradicts these field values)\n"
            "```json\n"
            f"{json.dumps(tickets, indent=2, default=str)}\n"
            "```"
        )
    except Exception as _e:
        _log(f"  jira pre-fetch (agent) skipped: {_e}")
        return ""


def _rag_inject_for_agent(log_window: str, error_class: str | None) -> str:
    """Compose a `## retrieved.rag` block for the agent primer.

    Mirrors the one-shot path's logic in llm.py but with a key
    difference: for KNOWN class, pull from `runbook` + `audit` source
    types (NOT audit-only). One-shot returns audit-only because the
    LLM also gets the full runbook content via the CLASS_DOCS mapping;
    the agent path has no equivalent — it only sees the runbook if it
    explicitly calls `read_runbook(class)` mid-loop. Auto-injecting
    runbook chunks here gives the agent up-front grounding so the
    drill plan + action template aren't a "maybe I should fetch it"
    decision the LLM frequently skips.

    Returns "" on any failure (RAG offline, PG down, module missing).
    """
    if not log_window:
        return ""
    try:
        from . import rag as _rag
        # Anchor the query on the fatal log line (R3.1 — sharper than
        # the whole 30K-char log window).
        import re as _re
        anchor = _re.search(
            r"(Error:|Exception:|FAIL:|FAILURE:|Caused by:).{0,500}",
            log_window[-15000:],
            _re.MULTILINE | _re.DOTALL,
        )
        q = anchor.group(0) if anchor else log_window[:6000]
        if not q.strip():
            return ""
        known = error_class and error_class != "unknown"
        # Agent path: include RUNBOOK in retrieval set for known class
        # (unlike one-shot which gets runbook via CLASS_DOCS).
        src_types = (["runbook", "audit"] if known
                     else ["runbook", "doc", "audit"])
        hits = _rag.search(
            q, k=5,
            source_types=src_types,
            error_class=error_class if known else None,
        )
        if not hits:
            return ""
        header = (
            "## retrieved.rag (top-k matches — CANDIDATES, verify via "
            "read_runbook / read_doc before citing in evidence)"
        )
        lines = [header]
        for h in hits:
            meta = h.get("meta") or {}
            klass = meta.get("error_class") or ""
            head = (
                f"- [{h['score']:.3f}] "
                f"{h['source_type']}/{h['source_id']}"
                f"{(' class=' + klass) if klass else ''}"
            )
            # Trim each chunk to ~1KB so 5 hits stay ~5KB total.
            body = h["chunk_text"][:1000].replace("\n", " ")
            lines.append(head)
            lines.append(f"    {body}…")
        return "\n".join(lines)
    except Exception as _e:
        _log(f"  rag auto-inject (agent) skipped: {_e}")
        return ""


def _format_resolved_values(initial_tool_ctx: str) -> str:
    """Extract the resolved health_check.target / .service_config JSON blocks
    from the pre-fetched context and republish them at the top of the primer
    in a single, easy-to-spot section. Pure heuristic regex over the existing
    `## health_check.target` and `## health_check.service_config` markdown
    blocks emitted by `_build_tool_context` — no new fetches.
    """
    import re as _re
    out = []
    for header in ("health_check.target", "health_check.service_config"):
        m = _re.search(
            rf"## {_re.escape(header)}\s*\n```json\s*\n(.*?)\n```",
            initial_tool_ctx or "",
            flags=_re.DOTALL,
        )
        if m:
            out.append(f"### {header}\n```json\n{m.group(1)}\n```")
    if not out:
        return "(no resolved values block found — primer will rely on service.lookup directly)"
    return "\n\n".join(out)
