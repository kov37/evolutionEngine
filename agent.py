"""The consumer, not the bootstrapper: takes a real task instead of a
curriculum goal, and works it with the full tool registry. Not a curriculum
goal itself — same tier as harness.py, hand-written orchestration code.

Defaults to evolutionEngine/workspace like harness.py, but --project can
point the whole toolbelt (kernel tools + every confined graduated tool) at
any real directory — see kernel/sandbox.py for how that confinement works.
"""

import argparse
import json
import os
import re
import signal
import time
import urllib.request
import urllib.error
from types import SimpleNamespace

from ollama import Client, chat

import action_governor
import adaptive_budget
import escalation_governor
import kernel.io_tools as io_tools
import kernel.memory as memory
import lifecycle_policy
import message_compaction
import progress_governor
import risk_layer
import sidecar
import status_report
import structured_state
import task_contract
import transaction_buffer
import validation_contract
import worker
import working_state
from lifecycle_fsm import LifecycleFSM, LifecycleState
from kernel.exec_tools import active_background_handles, cleanup_background_processes, restart_background, stop_process
from dispatch import _call_key, dispatch_tool_calls
from kernel.control import TASK_STATE, approve_task, finish_task
from kernel.sandbox import get_root, set_root
from novelty_context import NoveltyContext
from registry import load_registry

MODEL = "qwen3.6:35b-mlx"
ITERATION_BUDGET = 20
CHAT_TIMEOUT_SECONDS = 180
REPAIR_TURN_BUDGET = 3
# Two read-only turns are enough to establish a target in a normal workspace.
# A third turn is a measurable convergence tax for small/medium actors, so
# the governor must switch to an executable progress surface sooner.
ORIENTATION_TURN_BUDGET = 2

# These aliases are exposed only when the novelty progress gate has detected
# that the actor is replaying a temporary validation-helper edit.  Keeping the
# product scope in the tool name removes an ambiguity that a prompt warning
# alone cannot reliably resolve for a small model: the actor still gets a
# normal mutation operation, but the engine can replace the generic mutation
# surface with one whose contract explicitly names the required target plane.
PRODUCT_MUTATION_TOOLS = frozenset({"patch_product_file", "write_product_file"})


class ChatTimeoutError(TimeoutError):
    """The acting model stopped responding within one bounded turn."""


def _json_message(message):
    if isinstance(message, dict):
        out = dict(message)
    elif hasattr(message, "model_dump"):
        out = message.model_dump(exclude_none=True)
    else:
        out = {"role": getattr(message, "role", "assistant"),
               "content": getattr(message, "content", "")}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            out["tool_calls"] = []
            for call in tool_calls:
                function = getattr(call, "function", None)
                if isinstance(call, dict):
                    function = call.get("function", {})
                    name = function.get("name", "")
                    arguments = function.get("arguments", {})
                else:
                    name = getattr(function, "name", "")
                    arguments = getattr(function, "arguments", {})
                out["tool_calls"].append({
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                })
    if out.get("role") == "tool" and "tool_name" in out:
        out["name"] = out.pop("tool_name")
    # OpenAI-compatible servers represent function arguments as a JSON
    # string in assistant tool-call messages.  Some providers return that
    # string, while the local response adapter naturally parses it to a dict
    # for dispatch.  Re-serialize at the transport boundary so the next
    # request is valid for both strict and permissive servers.
    for call in out.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict) and not isinstance(function.get("arguments"), str):
            function["arguments"] = json.dumps(function.get("arguments") or {}, ensure_ascii=False)
    return out


def _normalize_llama_messages(raw_messages):
    """Adapt the engine's append-only history to strict chat templates.

    This is deliberately transport-local: the engine can retain its richer
    history while a backend repairs only representational issues required by
    its model template.  Mistral's template requires user/assistant turns to
    alternate, with tool results immediately following an assistant tool call.
    """
    messages = [_json_message(m) for m in raw_messages]
    system_messages = [m for m in messages if m.get("role") == "system"]
    system_text = str(system_messages[0].get("content", "")).strip() if system_messages else ""
    transient_system = "\n\n".join(
        str(m.get("content", "")) for m in system_messages[1:]
    ).strip()
    messages = [m for m in messages if m.get("role") != "system"]

    # Preserve the stable system role and prefix. The native template supports
    # one leading system message; moving it into the first user turn would
    # invalidate llama.cpp's prefix KV cache on every transient update.
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})

    # Later system messages are transient summaries/interventions. Keep them
    # at the changing tail instead of rewriting the stable prefix.
    if transient_system and messages:
        tail = messages[-1]
        if tail.get("role") in {"user", "tool"}:
            tail["content"] = str(tail.get("content", "")) + \
                "\n\n[TRANSIENT INSTRUCTIONS]\n" + transient_system
        elif tail.get("role") == "assistant" and not tail.get("tool_calls"):
            messages.append({"role": "user", "content":
                             "[TRANSIENT INSTRUCTIONS]\n" + transient_system})

    normalized = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        if role == "user" and normalized and normalized[-1].get("role") == "tool":
            # The generic loop may append a completion/repair instruction
            # after a tool result. Mistral's template requires the next turn
            # to be assistant, so carry that instruction with the tool result
            # rather than emitting an invalid user-after-tool transition.
            normalized[-1]["content"] = (
                str(normalized[-1].get("content", ""))
                + "\n\n[FOLLOW-UP INSTRUCTION]\n"
                + str(message.get("content", ""))
            )
            continue
        if role == "user" and normalized and normalized[-1].get("role") == "user":
            # Multiple transient user nudges are one user turn to Mistral's
            # alternating-role checker.
            previous = normalized[-1]
            previous["content"] = str(previous.get("content", "")) + "\n\n" + str(message.get("content", ""))
            continue
        if role == "tool" and (not normalized or normalized[-1].get("role") != "assistant"):
            # An intervention/compaction boundary must never leave an orphan
            # tool result. Preserve its evidence as a user-visible result.
            message = {
                "role": "user",
                "content": "[ORPHANED TOOL RESULT]\n" + str(message.get("content", "")),
            }
            if normalized and normalized[-1].get("role") == "user":
                normalized[-1]["content"] += "\n\n" + message["content"]
                continue
        normalized.append(message)
    return normalized


class PromptBudgetError(RuntimeError):
    """The request cannot fit even after safe transcript reduction."""


def _llama_root(base_url):
    root = base_url.rstrip("/")
    return root[:-3].rstrip("/") if root.endswith("/v1") else root


def _llama_token_count(base_url, messages, timeout_seconds, extra_payload=None):
    """Ask llama.cpp for the exact token count of the normalized prompt."""
    measured = {"messages": messages}
    if extra_payload is not None:
        measured["extra"] = extra_payload
    payload = {"content": json.dumps(measured, ensure_ascii=False, separators=(",", ":"))}
    request = urllib.request.Request(
        _llama_root(base_url) + "/tokenize",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=min(2.0, timeout_seconds or 2.0)) as response:
        data = json.loads(response.read().decode("utf-8"))
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        raise RuntimeError("llama.cpp /tokenize returned no token list")
    return len(tokens)


def _fit_llama_prompt(base_url, messages, max_tokens, timeout_seconds, extra_payload=None):
    """Measure and reduce the prompt before sending it to llama.cpp."""
    context_tokens = _context_window_tokens("llama-cpp", base_url)
    response_reserve = max(256, int(max_tokens or 0))
    prompt_budget = max(1_024, context_tokens - response_reserve - 256)
    tokenize_root = _llama_root(base_url)
    if tokenize_root in _TOKENIZE_UNAVAILABLE_BASE_URLS:
        return messages, None, prompt_budget
    try:
        prompt_tokens = _llama_token_count(base_url, messages, timeout_seconds, extra_payload)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _TOKENIZE_UNAVAILABLE_BASE_URLS.add(tokenize_root)
            print("⚠️ [prompt measurement unavailable] provider has no /tokenize endpoint; "
                  "using bounded context fallback")
        else:
            print(f"⚠️ [prompt measurement unavailable] HTTP {exc.code}: {exc}")
        return messages, None, prompt_budget
    except Exception as exc:
        # The percentage/raw-output bound remains the safe fallback when a
        # provider does not expose exact tokenization.
        print(f"⚠️ [prompt measurement unavailable] {type(exc).__name__}: {exc}")
        return messages, None, prompt_budget
    if prompt_tokens <= prompt_budget:
        return messages, prompt_tokens, prompt_budget

    # Keep the newest tool evidence and replace the oldest tool payloads in
    # place. Re-measure after each replacement because token density varies by
    # file/code content; never guess that characters equal tokens here.
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = str(message.get("content", ""))
        if content.startswith("[pruned tool output"):
            continue
        message["content"] = (
            f"[pruned by exact token budget: {len(content)} chars removed; "
            "use focused reads or current state if needed.]"
        )
        prompt_tokens = _llama_token_count(base_url, messages, timeout_seconds, extra_payload)
        if prompt_tokens <= prompt_budget:
            print(f"🧮 [prompt fit] reduced {prompt_tokens} tokens <= budget {prompt_budget}")
            return messages, prompt_tokens, prompt_budget
    raise PromptBudgetError(
        f"prompt remains {prompt_tokens} tokens after safe tool pruning; "
        f"budget={prompt_budget}, n_ctx={context_tokens}"
    )


def _llama_cpp_chat(*, base_url, timeout_seconds, **kwargs):
    """Call llama-server's OpenAI-compatible endpoint as the actor backend."""
    from ollama import _utils

    messages = _normalize_llama_messages(kwargs.get("messages", []))
    max_tokens = kwargs.get("max_tokens", 1024)
    tool_payload = [json.loads(_utils.convert_function_to_tool(fn).model_dump_json(exclude_none=True))
                    for fn in kwargs.get("tools", [])]
    messages, measured_prompt_tokens, prompt_budget = _fit_llama_prompt(
        base_url, messages, max_tokens, timeout_seconds, extra_payload=tool_payload,
    )
    if measured_prompt_tokens is not None:
        print(f"🧮 [prompt tokens] {measured_prompt_tokens}/{prompt_budget}")

    payload = {
        "model": kwargs["model"],
        "messages": messages,
        "stream": False,
        # Agent turns should be decisive and bounded.  The Ollama path gets
        # these from its model defaults; llama-server otherwise defaults to
        # temperature 0.8 with unlimited generation, which can spend minutes
        # explaining before emitting a tool call.
        "temperature": 0.15,
        "max_tokens": max_tokens,
        "reasoning_format": "none",
        "reasoning_effort": "none",
        "parallel_tool_calls": False,
        "tools": tool_payload,
    }
    # mlx-lm exposes Qwen's hybrid thinking switch as a request-level chat
    # template argument. Send it explicitly instead of relying on the
    # server's startup default, so a provider restart cannot silently change
    # the actor's latency/action profile. `think` remains the generic agent
    # setting; this mapping is isolated at the llama.cpp adapter boundary.
    if kwargs.get("think") is not None:
        payload["chat_template_kwargs"] = {
            "enable_thinking": bool(kwargs["think"]),
        }
    if kwargs.get("tool_choice") is not None:
        payload["tool_choice"] = kwargs["tool_choice"]
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds or None) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"llama.cpp HTTP {exc.code}: {detail}") from exc
    raw = ((data.get("choices") or [{}])[0].get("message") or {})
    calls = []
    for call in raw.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append(SimpleNamespace(function=SimpleNamespace(
            name=fn.get("name", ""), arguments=args)))
    message = SimpleNamespace(role="assistant", content=raw.get("content") or "",
                              tool_calls=calls or None, thinking=None)
    usage = data.get("usage") or {}
    return SimpleNamespace(message=message, prompt_eval_count=usage.get("prompt_tokens", 0),
                           eval_count=usage.get("completion_tokens", 0),
                           prompt_eval_duration=0, eval_duration=0)


def _chat_with_timeout(*, timeout_seconds, backend="ollama",
                       base_url="http://127.0.0.1:8080/v1", **kwargs):
    """Call Ollama without allowing one turn to strand the whole run."""
    if backend == "llama-cpp":
        if timeout_seconds <= 0 or not hasattr(signal, "SIGALRM"):
            return _llama_cpp_chat(base_url=base_url, timeout_seconds=timeout_seconds, **kwargs)

        # urllib's socket timeout is a useful transport guard, but a server
        # can keep a response technically active while generation is stalled.
        # The agent needs a hard wall-clock boundary around the whole adapter
        # call as well, or one model request can strand the FSM indefinitely.
        def _llama_alarm(_signum, _frame):
            raise ChatTimeoutError(f"llama.cpp chat exceeded {timeout_seconds}s")

        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _llama_alarm)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        try:
            return _llama_cpp_chat(base_url=base_url, timeout_seconds=timeout_seconds, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    if timeout_seconds <= 0 or not hasattr(signal, "SIGALRM"):
        return chat(**kwargs)

    # The Ollama Python client uses an httpx transport.  Its transport-level
    # timeout is the reliable boundary; a Python alarm alone cannot interrupt
    # a blocking C/network read (observed in the live action-critic run).
    client = Client(timeout=timeout_seconds)

    def _alarm(_signum, _frame):
        raise ChatTimeoutError(f"acting-model chat exceeded {timeout_seconds}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return client.chat(**kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)

# Level 5 (escalation_governor.py) is meant to be a hard stop — "call
# finish_task now" — but only OFFERS finish_task as the sole allowed tool;
# it never forces compliance. Confirmed live, twice (Run 14 and Run 16b):
# a model that keeps reaching for a different (blocked) tool instead just
# bounces off dispatch.py's rejection message indefinitely, burning the
# rest of the iteration budget without ever actually terminating. Once
# consecutive_no_progress climbs this far past LEVEL_5_THRESHOLD
# (escalation_governor.LEVEL_5_THRESHOLD, currently 8) with zero recovery,
# the harness force-terminates itself rather than waiting on compliance
# that isn't coming.
HARD_STOP_AFTER_TURNS = escalation_governor.LEVEL_5_THRESHOLD + 3

# context_summary_enabled only: how many of the most recent tool-result
# messages stay verbatim in `messages` before being pruned to a short
# pointer (kernel.memory.recall still has the full original). Bounds
# `messages`'s own growth — the exact problem that hit num_ctx's ceiling
# live during testing (97.5% full by iteration 39, zero writes yet).
KEEP_RECENT_RAW_RESULTS = 5

# structured_summary_enabled only: char budget replacing pure call-count
# eligibility for that path. Found live (user-raised concern, Run 14): a
# 50-char directory listing and a 50,000-char file dump both counted as
# "1 of the last 5" under KEEP_RECENT_RAW_RESULTS alone, so eligibility
# tracked call age, not actual context pressure — could fire far too late
# (5 huge results still sitting in context) or too early (5 tiny ones
# evicting something still relevant). Entries are kept newest-first until
# their combined size exceeds this budget; everything older becomes
# prunable, whatever the call count. Still gated by PRUNE_BATCH_SIZE below
# for the same cache-invalidation reason.
KEEP_RECENT_RAW_CHARS = 20_000

# At an intervention point, a short recent tail is more useful than making
# the acting model reprocess the entire exploratory transcript. The durable
# structured state and the novelty ledger remain available to the harness;
# this only changes what the 35B sees for that one action-selection turn.
INTERVENTION_TAIL_MESSAGES = 12


def _intervention_messages(messages, tail=INTERVENTION_TAIL_MESSAGES):
    """Keep the foundation and latest tool trajectory for an action turn."""
    if len(messages) <= tail + 2:
        return list(messages)
    foundation = list(messages[:2])
    recent = list(messages[-tail:])
    return foundation + [
        {"role": "system", "content": (
            "[intervention context reduction] Earlier raw turns are omitted from this action-selection "
            "turn. Use the structured state, novelty critic, and recent tool results below. Take one "
            "concrete next action; do not restart broad exploration."
        )},
    ] + recent


_REPAIR_RECOVERY_TOOLS = frozenset({
    "read_file", "find_files", "search_file", "list_symbols", "grep_dir",
    "list_workspace", "list_dir", "diff_files", "git_diff", "recall",
    "run_tests", "run_command", "run_shell", "process_status", "stop_process",
})


def _is_blocked_repair_action(tool_name, result_content):
    """Return true when a non-mutation repair action was rejected by the engine.

    A normal missing-file error can be useful feedback and deserves one
    corrected attempt.  These markers mean something different: the actor is
    replaying an action that the current lifecycle deliberately removed.  The
    right response is a fresh repair checkpoint, not another model turn with
    the same stale assistant message in view.
    """
    if tool_name not in _REPAIR_RECOVERY_TOOLS:
        return False
    lower = str(result_content or "").lower()
    return any(marker in lower for marker in (
        "is unavailable this turn",
        "not in allowed set",
        "repeated failing call",
        "rejected: repeated",
        "invalid tool call",
    ))


def _repair_checkpoint_messages(
    messages, *, last_repair_packet, mutation_checkpoint=None, state_text=""
):
    """Build a fresh, bounded action context for a stuck product repair.

    Keeping the foundation and the last accepted mutation preserves the code
    the actor may need to patch.  Dropping the old action-selection tail is
    intentional: a stale assistant message such as "I will read the server
    log" can otherwise survive a tool-plane restriction and be emitted again
    even when that tool is no longer offered.
    """
    foundation = list(messages[:2])
    checkpoint = foundation + [{
        "role": "system",
        "content": (
            "[fresh repair checkpoint] The previous repair action was rejected by the engine. "
            "The old action transcript is intentionally omitted. Use only the current failure evidence "
            "and the accepted mutation below; make one concrete product patch now. Do not repeat a "
            "rejected non-mutation action, start a service, run another probe, or return a plan. "
            "If the previous patch was rejected, change its contents and do not repeat it."
        ),
    }]
    if mutation_checkpoint:
        checkpoint.extend(mutation_checkpoint)
    if state_text:
        checkpoint.append({"role": "system", "content": state_text})
    checkpoint.append({
        "role": "system",
        "content": "Latest executable failure evidence:\n" + str(last_repair_packet or "(not available)"),
    })
    checkpoint.append({
        "role": "system",
        "content": (
            "Repair contract: call patch_file or write_file now on the implicated product artifact. "
            "The validation artifact and test files are evidence and must remain unchanged."
        ),
    })
    return checkpoint


def _source_backed_repair_messages(
    messages, *, last_repair_packet, state_text="", inspection_checkpoint=None
):
    """Build a compact repair prompt when the traceback already localized code.

    A source excerpt is sufficient localization evidence. Keeping the entire
    historical tool transcript in front of the actor only increases prompt
    work and lets old validation plans compete with the current failure. This
    checkpoint keeps the stable task foundation, the bounded failure packet,
    and the explicit mutation contract.
    """
    checkpoint = list(messages[:2]) + [{
        "role": "system",
        "content": (
            "[source-backed repair checkpoint] The latest executable failure already identifies the "
            "relevant source location. Use the failure packet below, make one minimal product mutation "
            "with patch_file or write_file, and do not rerun the probe or return a plan first. The "
            "validation artifact and supplied tests are evidence; leave them unchanged."
        ),
    }]
    if state_text:
        checkpoint.append({"role": "system", "content": state_text})
    if inspection_checkpoint:
        checkpoint.append({
            "role": "system",
            "content": (
                "Most recent targeted inspection results (already available; do not reread these files):\n"
                + "\n\n".join(str(item.get("content", "")) for item in inspection_checkpoint)
            )[:5000],
        })
    checkpoint.append({
        "role": "system",
        "content": "Latest executable failure evidence:\n" + str(last_repair_packet or "(not available)")[:2600],
    })
    checkpoint.append({
        "role": "system",
        "content": "Repair contract: call patch_file or write_file now on the implicated product artifact.",
    })
    return checkpoint


def _rejected_mutation_inspection_messages(
    messages, *, last_repair_packet, target_path="", state_text=""
):
    """Build the one-turn inspection checkpoint after a rejected mutation."""
    target = f" on `{target_path}`" if target_path else " on the implicated product file"
    checkpoint = list(messages[:2]) + [{
        "role": "system",
        "content": (
            "[rejected mutation recovery] The previous product patch was rejected because its exact "
            f"search text did not match the file on disk{target}. Use this one turn to read the current "
            "source with a focused inspection tool. Do not patch, validate, finish, browse broadly, or "
            "repeat the rejected search. The next turn will require a fresh mutation or recovery."
        ),
    }]
    if state_text:
        checkpoint.append({"role": "system", "content": state_text})
    checkpoint.append({
        "role": "system",
        "content": "Latest rejected-mutation evidence:\n" + str(last_repair_packet or "(not available)")[:3000],
    })
    return checkpoint


# How many prunable entries must accumulate before a prune batch actually
# runs. Found live: with this at "prune immediately, one at a time" (the
# original design), pruning fired on nearly every single iteration once
# past KEEP_RECENT_RAW_RESULTS — each prune mutates an old `messages` entry
# in place, which invalidates Ollama's prefix cache for everything
# downstream, so pruning THAT often was quietly paying that cost almost
# every call. Batching trades a slightly larger worst-case context
# (KEEP_RECENT_RAW_RESULTS + up to PRUNE_BATCH_SIZE-1 entries momentarily
# unpruned) for a proportional reduction in cache-invalidation frequency.
PRUNE_BATCH_SIZE = 5

# Always bound live raw tool output, including novelty-only runs where the
# optional structured/context summary branches are disabled. This protects the
# actor from a finite provider context (llama.cpp may be configured below
# agent.py's preferred NUM_CTX) without removing tool-call/result messages.
RAW_TOOL_CONTEXT_FRACTION = 0.18
CHARS_PER_TOKEN_ESTIMATE = 4
FALLBACK_CONTEXT_WINDOW_TOKENS = 16_384

# Some OpenAI-compatible local servers expose chat completions but not the
# optional exact-token endpoint. Cache that capability result per server so a
# missing endpoint cannot add one failed HTTP request to every actor turn.
_TOKENIZE_UNAVAILABLE_BASE_URLS = set()


def _context_window_tokens(backend, base_url):
    """Discover the provider window when possible; use a safe fallback."""
    if backend != "llama-cpp":
        return int(NUM_CTX)
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    try:
        request = urllib.request.Request(root + "/props", method="GET")
        with urllib.request.urlopen(request, timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        candidates = [payload.get("n_ctx"), payload.get("n_ctx_train")]
        defaults = payload.get("default_generation_settings") or {}
        candidates.extend([defaults.get("n_ctx"), defaults.get("n_ctx_train")])
        for value in candidates:
            if isinstance(value, int) and value > 0:
                return value
    except Exception:
        pass
    return FALLBACK_CONTEXT_WINDOW_TOKENS


def _live_tool_result_char_budget(backend, base_url):
    context_tokens = _context_window_tokens(backend, base_url)
    budget = int(context_tokens * RAW_TOOL_CONTEXT_FRACTION * CHARS_PER_TOKEN_ESTIMATE)
    return max(4_000, budget), context_tokens


def _bound_live_tool_results(messages, char_budget):
    """Replace old tool output in place once the live raw tail is too large."""
    running = 0
    pruned = []
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = str(message.get("content", ""))
        if content.startswith("[pruned tool output"):
            continue
        if running + len(content) <= char_budget:
            running += len(content)
            continue
        messages[index]["content"] = (
            f"[pruned tool output: {len(content)} chars removed from live context; "
            "use focused reads and current state if this evidence is needed.]"
        )
        pruned.append(len(content))
    if pruned:
        print(f"🗑️  [global tool bound] pruned {len(pruned)} outputs "
              f"({sum(pruned)} chars); live tail budget={char_budget}")
    return pruned

# Re-enabled after an isolated experiment (every other fix left on, only
# this toggled off) showed pruning was NEVER the cause of the exploration-
# avoidance behavior — the same redundant-read pattern persisted with zero
# pruning, and the run additionally exceeded num_ctx with nothing bounding
# it, hitting Ollama's silent context-eviction (worse than pruning's own
# "[pruned...]" pointer, which at least tells the model what happened and
# stays recoverable via recall(N)). Confirmed safe to combine with the
# progress-governor system: the evidence ledger records each tool result at
# call time, BEFORE pruning ever mutates `messages`, so pruning can't affect
# what the governor's stagnation/avoidance signals see — and recall(N)
# stays available even under a level-4 tool restriction specifically so a
# pruned entry's exact text is never actually unreachable.
ENABLE_PRUNING = True

# Chosen for latency, not memory — throughput collapses well before the
# memory ceiling on this hardware (215 tok/s at num_ctx=262144 vs.
# 1,489.8 tok/s at 65536, still the second-best measured point). See
# REFACTORING_LEARNINGS.md findings #19-21.
# Was 32768 (halved from 65536) specifically to make room for a 9b sidecar
# model running concurrently — but structured_summary_enabled mode doesn't
# use that 9b (only the much smaller 4b, for Status judgments), so it never
# needed the smaller ceiling. Restored to 65536 after finding this: the
# reference baseline that solved 11/12 distributions on this exact task
# (naive-baseline-verified-35b, no structured-state machinery at all) ran
# at num_ctx=65536 and reached iteration 102 before being stopped by hand —
# every structured_summary_enabled run tonight, capped at 32768, never got
# close to that much room.
NUM_CTX = 65536

# A transient Ollama-side hiccup (e.g. "XML syntax error... element <function>
# closed by </parameter>", a malformed-tool-call response from the model that
# the server can't parse) must not crash the whole run outright — confirmed
# live, twice, in this project's real history: a first fix (5 retries, 30s
# backoff cap) still wasn't enough on a real overnight run, so the retry
# count and backoff cap here are the values that actually held up, not a
# fresh guess. Exponential, capped, so a genuinely dead server still gives up
# in reasonable time rather than retrying forever.
MAX_CHAT_RETRIES = 20
# A coherent small application may need an implementation file, a client,
# and a dependency manifest before any one of them is runnable. Permit two
# related follow-up mutations, then force validation; this is finite and
# provider/model agnostic.
MUTATION_BATCH_LIMIT = 2


def _terminal_provider_error(error) -> bool:
    """Return true for provider states where retrying cannot help."""
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in (
        "connection refused",
        "connection reset by peer",
        "remote end closed connection without response",
        "remotedisconnected",
        "broken pipe",
        "connection aborted",
        "name or service not known",
        "nodename nor servname",
    ))


def _retryable_provider_disconnect(error, attempt: int) -> bool:
    """Allow one quick retry for a live provider that dropped one response.

    A refused connection or unknown host is terminal.  A server that is
    healthy before and after a single request can briefly close an HTTP
    connection while switching/evicting a model or handling a long request;
    retry that exact class once, but never turn it into an unbounded stall.
    """
    if attempt >= 2:
        return False
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in (
        "remote end closed connection without response",
        "remotedisconnected",
        "connection reset by peer",
    ))


def _is_validation_setup_failure(text: str) -> bool:
    """Return whether a failed check needs command/runner recovery first.

    A direct test module that exits cleanly without running a test is an
    execution-contract failure, not a product failure. Keeping command tools
    available lets the actor select a real runner or explicitly invoke the
    supplied test function without editing the evidence.
    """
    lower = str(text or "").lower()
    if "test module" in lower and "no test evidence" in lower:
        return True
    return any(marker in lower for marker in (
        "could not start", "no such file or directory", "importerror",
        "no tests discovered", "dependency", "permission denied",
        "test module ran as a script", "test runner exited successfully but discovered zero tests",
        # A stopped or unreachable local service is a setup-plane failure.
        # Reopen the process/command surface so the actor can restore the
        # validation target before changing product code.
        "connection refused", "connectionrefusederror", "failed to connect",
        "urlopen error", "server is down",
    ))


def _auto_validation_command(path: str):
    """Return a safe argv command for a newly written validation helper.

    Helpers are explicitly confined below ``.agentic/`` by the validation
    policy. Running one immediately is a deterministic orchestration hook: it
    removes a model turn whose only purpose would be to call the helper the
    actor just created. Unknown extensions are left to the actor because the
    engine cannot infer their interpreter safely.
    """
    if not lifecycle_policy.is_validation_helper_path(path):
        return None
    normalized = str(path).replace("\\", "/").lower()
    if normalized.endswith(".py"):
        return ["python3", path]
    if normalized.endswith((".js", ".cjs")):
        return ["node", path]
    if normalized.endswith(".sh"):
        return ["bash", path]
    return None


def _transaction_window_open(transaction, *, validation_required: bool,
                              repair_required: bool, mutation_batch_remaining: int,
                              pending_product_paths=()) -> bool:
    """Return whether proactive validation may be deferred for related edits.

    A normal single-file repair must retain the fast validate-after-mutation
    hook. Deferral is justified only when the host has already opened a
    transaction, or when an explicit related-mutation batch is still active.
    The predicate is intentionally independent of task names and models.
    """
    if transaction is not None and transaction.active:
        # The decision is made after the current tool results are available.
        # If this turn just added a second distinct product file, the bridge
        # is complete enough to validate immediately instead of spending one
        # more actor turn on a known test command.
        current_files = set(transaction.files)
        current_files.update(str(path) for path in pending_product_paths if path)
        if len(current_files) > 1:
            return False
    return bool(
        (
            transaction is not None
            and transaction.active
            and validation_required
            and repair_required
        )
        or mutation_batch_remaining > 1
    )


def _has_test_artifacts(workspace_listing: str) -> bool:
    """Detect conventional test artifacts without inspecting task semantics."""
    for raw_line in str(workspace_listing or "").splitlines():
        name = raw_line.strip().split(" (", 1)[0].replace("\\", "/")
        leaf = name.rstrip("/").rsplit("/", 1)[-1].lower()
        if leaf in {"test", "tests", "spec", "specs"}:
            return True
        is_test_file = leaf.startswith(("test", "spec")) or bool(
            re.search(r"(?:^|[._-])(test|spec)(?:[._-]|$)", leaf)
        )
        if is_test_file and leaf.endswith((
            ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rb", ".rs", ".php"
        )):
            return True
    return False


def _nearby_python_test_target(path: str, root: str | None = None) -> str | None:
    """Find the nearest conventional Python test target for a changed file.

    This is a structural host check, not task knowledge: it looks only at
    sibling/ancestor names such as ``tests`` and ``test_*.py``. Returning a
    narrow target avoids launching an entire repository suite after every
    edit, while still giving the actor authoritative behavioral evidence
    before it invents version or import probes.
    """
    workspace_root = os.path.realpath(root or get_root())
    raw_path = str(path or "")
    candidate = os.path.realpath(
        raw_path if os.path.isabs(raw_path) else os.path.join(workspace_root, raw_path)
    )
    try:
        if os.path.commonpath((workspace_root, candidate)) != workspace_root:
            return None
    except ValueError:
        return None
    current = candidate if os.path.isdir(candidate) else os.path.dirname(candidate)
    while True:
        try:
            entries = sorted(os.listdir(current))
        except OSError:
            entries = []
        # Prefer a test directory because run_tests can discover its suite
        # without guessing one particular test module.
        for entry in entries:
            full = os.path.join(current, entry)
            if not os.path.isdir(full) or entry.lower() not in {"test", "tests", "spec", "specs"}:
                continue
            try:
                has_python_test = any(
                    name.endswith(".py") and name.lower().startswith(("test", "spec"))
                    for name in os.listdir(full)
                )
            except OSError:
                has_python_test = False
            if has_python_test:
                return os.path.relpath(full, workspace_root)
        # A colocated module is even narrower than a test directory.
        for entry in entries:
            lower = entry.lower()
            if (lower.endswith(".py") and
                    (lower.startswith("test_") or lower.startswith("test") or
                     lower.endswith("_test.py"))):
                return os.path.relpath(os.path.join(current, entry), workspace_root)
        if current == workspace_root:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _has_orientation_evidence(messages) -> bool:
    """Return whether a targeted inspection produced usable evidence."""
    evidence_tools = {"read_file", "find_files", "search_file", "list_symbols"}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        tool_name = message.get("tool_name") or message.get("name")
        if tool_name not in evidence_tools:
            continue
        content = str(message.get("content") or "").strip()
        if content and not content.startswith(("ERROR:", "REJECTED:")):
            # A malformed small-model read request such as ``limit: 0`` still
            # produces a non-empty wrapper ("--- file --- (0 chars) ---").
            # The wrapper is not source evidence; treating it as evidence
            # closes the read surface before the actor has seen the artifact,
            # which creates a deterministic-looking but real paralysis loop.
            if "(0 chars)" in content or "lines 1-0" in content:
                continue
            return True
    return False


def _force_repair_recovery(recovery_mode, repair_required, setup_failure):
    """Return true only when a product mutation is justified by the failure.

    A missing runner, dependency, or test discovery result is an execution
    problem. Forcing an implementation rewrite in that state can destroy a
    correct artifact, so setup recovery remains allowed to select a better
    check instead.
    """
    return bool(recovery_mode and repair_required and not setup_failure)


def _authoritative_gate_restrictions(gate_banned, setup_failure):
    """Keep stale 4B restrictions subordinate to the deterministic plane."""
    banned = set(gate_banned or ())
    if not setup_failure:
        banned -= {"patch_file", "write_file"}
    return banned


def _consume_worker_gate(novelty_action_gate, novelty_context):
    """Consume 4B tool restrictions only when gating is explicitly enabled."""
    if not novelty_action_gate or novelty_context is None:
        return set()
    return novelty_context.consume_gate_restrictions()


def _worker_triage_enabled(novelty_action_critic, novelty_action_gate):
    """Keep synchronous 4B inference off the path unless explicitly requested."""
    return bool(novelty_action_critic or novelty_action_gate)


def _novelty_progress_tool_names(
    novelty_context, *, helper_mutation_blocked=False,
    rejected_mutation_read_pending=False,
):
    """Return the final tool surface when the context ledger requires progress.

    This policy is intentionally computed again at the end of turn assembly.
    Lifecycle and validation policies may narrow or rebuild ``tools_for_call``
    later in the loop; a progress gate that runs only before those policies can
    be silently undone. Once the bounded orientation window is exhausted,
    validation is evidence but not progress, so only mutation or explicit
    completion remains legal. During the orientation window, targeted reads
    remain available unless the same non-mutating action already repeated.
    """
    if novelty_context is None or not novelty_context.requires_progress():
        return None
    if rejected_mutation_read_pending:
        return {"read_file", "search_file", "list_symbols", "grep_dir"}
    names = {"patch_file", "write_file", "finish_task"}
    repeated_action = novelty_context.repeated_validation_loop()
    if novelty_context.recovery_reads_allowed() and not repeated_action:
        names.update({"read_file", "find_files", "search_file"})
    if helper_mutation_blocked:
        # A stale model commonly replays the generic mutation call after its
        # temporary helper was rejected. Replace both generic mutation names
        # with explicit product-scoped aliases. This is stronger than merely
        # removing write_file: the live SymPy trace showed the actor then
        # replayed patch_file with the same helper path.
        names -= {"patch_file", "write_file"}
        names.update(PRODUCT_MUTATION_TOOLS)
    return names


def _progress_tool_call_required(novelty_context, novelty_action_gate, backend, tools):
    """Return whether the provider must emit one currently legal progress tool.

    Tool schemas are not enough when an actor can remember a tool from an
    earlier turn.  Once the host progress gate has narrowed the registry, a
    provider-level required-tool boundary prevents another prose or stale
    inspection turn.  The condition is based only on lifecycle evidence and
    the active tool surface, never on a task or model name.
    """
    if not (novelty_action_gate and novelty_context is not None and backend == "llama-cpp"):
        return False
    if not novelty_context.requires_progress() or not tools:
        return False
    return True


def _stale_tool_names(tool_calls, allowed_names) -> tuple[str, ...]:
    """Return tool names emitted outside the host's current contract."""
    allowed = set(allowed_names or ())
    return tuple(sorted({
        call.function.name for call in (tool_calls or [])
        if call.function.name not in allowed
    }))


def _consume_orientation_recovery_read(
    recovery_active: bool,
    evidence_available: bool,
    recovery_read_used: bool,
    blocked_command_calls,
) -> bool:
    """Count a rejected inspection as the bounded recovery attempt.

    A blocked read is still an attempted recovery action. Leaving the counter
    unchanged lets a model replay the same inspection forever because the
    host never receives a successful read to mark as consumed.
    """
    if recovery_read_used:
        return True
    return bool(recovery_active and evidence_available and blocked_command_calls)


def patch_product_file(path: str, search: str, replace: str) -> str:
    """Patch a product source file during novelty recovery.

    The alias is intentionally only offered after a validation-helper
    mutation has been rejected. Its name is a model-facing contract: the
    target must be the product artifact implicated by the task, never a
    temporary verifier below ``.agentic/``.
    """
    if lifecycle_policy.is_validation_helper_path(path):
        return (
            "REJECTED: patch_product_file accepts product artifacts only; "
            "choose the source file implicated by the failure or call finish_task."
        )
    return io_tools.patch_file(path, search, replace)


def write_product_file(path: str, content: str) -> str:
    """Create or replace a product source file during novelty recovery.

    Temporary validation helpers are deliberately excluded; use an inline
    command for a probe instead of spending the progress turn on a verifier.
    """
    if lifecycle_policy.is_validation_helper_path(path):
        return (
            "REJECTED: write_product_file accepts product artifacts only; "
            "choose the source file implicated by the failure or call finish_task."
        )
    return io_tools.write_file(path, content)


NO_ACTION_TOOL_FORCE_THRESHOLD = 2
FORCED_ACTION_MAX_TOKENS = 4096


def _force_tool_call_after_no_action(no_action_turns: int, backend: str) -> bool:
    """Use the provider's structured tool mode after repeated prose-only turns."""
    return backend == "llama-cpp" and int(no_action_turns or 0) >= NO_ACTION_TOOL_FORCE_THRESHOLD


def _completion_ready(messages, task_type, validation_plan=None, validation_evidence=None, validation_criteria_hits=None):
    """Require concrete evidence before honoring the model's finish request."""
    if task_type != "code_change":
        return True, None

    mutated = False
    validated = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        name = message.get("tool_name")
        content = str(message.get("content", ""))
        if name in {"write_file", "patch_file"} | PRODUCT_MUTATION_TOOLS and not content.startswith(("ERROR:", "REJECTED")):
            mutated = True
        if (validation_plan.assess(name, {}, content)[0] if validation_plan
                else action_governor.is_substantive_validation(name, {}, content)):
            validated = True
    if not mutated:
        task_text = str(getattr(validation_plan, "task", "")) if validation_plan else ""
        verification_only = bool(task_text) and not re.search(
            r"\b(?:create|build|implement|fix|repair|modify|write|change|add|update)\b",
            task_text,
            re.IGNORECASE,
        )
        if not verification_only:
            return False, "no successful write_file or patch_file call has been observed"
    # Validation was already assessed at dispatch time with the original
    # command arguments. Re-assessing a tool result here with ``{}`` loses the
    # endpoint/method that was present in the command and can falsely reject
    # valid evidence (especially API probes). Use the accepted evidence ledger
    # as the source of truth for completion readiness.
    if validation_evidence:
        validated = True
    if not validated:
        return False, "no passing run_tests, run_command, or run_shell result has been observed"
    if validation_plan and validation_plan.endpoints:
        evidence = validation_criteria_hits or validation_evidence or set()
        required = validation_plan.operations or validation_plan.endpoints
        covered = {item for item in required if any(item.lower() in str(observed).lower() for observed in evidence)}
        if len(covered) < len(required):
            return False, f"only {len(covered)}/{len(required)} required interfaces have validation evidence"
    return True, None

# A hard forcing function (removing read-only tools from what the model is
# offered after N idle iterations) was tried and then REMOVED after real
# data undercut it: across two back-to-back gated runs, the first genuine
# patch_file attempt landed at iteration 17 in BOTH — same as without any
# gate pressure changing when the model actually converged, just adding
# friction (blocked calls, run_shell workarounds re-implementing read_file/
# grep_dir) along the way. Worse, both post-gate first attempts had real
# mistakes (a missing import, a bad indentation guess) immediately after a
# "you must edit now" system message — plausible the gate was rushing a
# worse-quality first attempt rather than producing a better-timed one.
# Left in place instead: the passive nudges (system prompt, recall_note)
# plus every other fix (pruning, edit ledger, whitespace-tolerant
# patch_file, read_file's full-read guidance) — none of those remove any
# capability, they just make the tools/feedback better.


def run_agent(task, tools, iteration_budget=ITERATION_BUDGET, sidecar_enabled=False, worker_enabled=False,
              context_summary_enabled=False, structured_summary_enabled=False, working_state_enabled=False,
              task_type="code_change", think=False, status_path=None, distribution_target_file=None,
              distribution_names=None, novelty_context_enabled=False, novelty_worker_model="qwen3.5:4b",
              novelty_action_gate=False, novelty_action_critic=False,
              chat_timeout=CHAT_TIMEOUT_SECONDS, backend="ollama",
              base_url="http://127.0.0.1:8080/v1", action_first=False):
    TASK_STATE["done"] = False
    TASK_STATE["requested"] = False
    TASK_STATE["summary"] = None
    if context_summary_enabled or structured_summary_enabled or working_state_enabled:
        # Give the model a way to get exact original text back for a
        # pruned entry, without every caller needing to remember to wire
        # this in — it's a direct consequence of enabling pruning below.
        tools = tools + [memory.recall]
    tool_map = {fn.__name__: fn for fn in tools}
    sidecar_log = []
    context_summary = ""
    memory.reset_archive()
    entry_positions = {}  # entry_number -> index into `messages` of its raw tool-result message
    state = structured_state.StructuredState(task) if structured_summary_enabled else None
    # working_state.py: layer-3 memory MVP (pasted proposal, this session) —
    # a restartable checkpoint rather than a conversation summary. See its
    # module docstring for how layers 1/2 (event log, evidence archive)
    # already exist as ledger.history/memory.ARCHIVE below; ws_since_index
    # tracks how much of ledger.history the last checkpoint() call already
    # saw, so each checkpoint only sends genuinely new events.
    ws = working_state.WorkingState(
        objective=(task if len(task) <= 300 else task[:300] + "...(truncated)"), task_type=task_type,
    ) if working_state_enabled else None
    ws_since_index = 0
    # The full progress-governor system (action_governor + task_contract +
    # progress_governor + escalation_governor + risk_layer) — see each
    # module's own docstring for design rationale. Replaces the earlier
    # FORCE_EDIT_AFTER_ITERATIONS mechanism with something that: (a) never
    # pushes toward MUTATE on a task that doesn't need one (task_contract),
    # (b) scales its patience with real task/repo signals instead of a
    # fixed constant (adaptive_budget), (c) escalates gradually instead of
    # flipping from fully-open to fully-restricted at one iteration count
    # (escalation_governor), and (d) makes a demanded attempt safe to make
    # via checkpoint/rollback (risk_layer) instead of just demanding it.
    # Shared by both structured_summary_enabled and working_state_enabled —
    # the governor doesn't care which state-summary mode is layered on top,
    # it only ever reads ledger.history. working_state_enabled's own branch
    # below records into this SAME ledger (no second ledger, no divergence).
    # The risk/checkpoint layer is also part of novelty mode.  Previously it
    # was accidentally limited to the older summary modes, so the very mode
    # intended to improve small-model recovery could still rewrite a supplied
    # test without a rollback path.
    _governed = structured_summary_enabled or working_state_enabled or novelty_context_enabled
    ledger = action_governor.EvidenceLedger() if _governed else None
    contract = task_contract.CONTRACTS_BY_TYPE.get(task_type, task_contract.CODE_CHANGE_CONTRACT)
    escalation = escalation_governor.EscalationState() if _governed else None
    risk = risk_layer.RiskLayer() if _governed else None
    transaction = (
        transaction_buffer.TransactionBuffer(get_root(), followup_turns=1)
        if _governed else None
    )
    if risk is not None:
        risk.protect_existing_tests(get_root())
    # adaptive_budget.py's repo_size_hint — computed ONCE here (not derived
    # from the ledger, which would be circular, see adaptive_budget.py's
    # own docstring) from a cheap, non-recursive top-level listing. Real
    # file COUNT of the project root, not a deep walk — a full recursive
    # count of a large real checkout (sympy has thousands of files) would
    # be slow and mostly noise for this purpose; top-level breadth is a
    # good-enough, cheap proxy.
    workspace_listing = io_tools.list_workspace()
    repo_size_hint = len([l for l in workspace_listing.splitlines() if l.strip()])
    # message_compaction.py's bookkeeping: iteration_number -> index of that
    # iteration's assistant message in `messages`, and -> its real output
    # token count (from response.eval_count). See message_compaction.py's
    # docstring for why this is a separate mechanism from entry_positions'
    # tool-result pruning above.
    iteration_assistant_idx = {}
    iteration_tokens = {}
    compacted_iterations = set()
    # status_report.py: an external, human-readable snapshot for a separate
    # terminal to tail -f/watch — distinct from current_level's use just
    # below (that only lives inside the `if _governed and ledger.history`
    # branch each iteration; kept here too so status writes still have last
    # iteration's value on turns that branch doesn't run, e.g. iteration 1).
    current_level = 0
    recent_errors = []
    novelty_context = NoveltyContext(worker_model=novelty_worker_model) if novelty_context_enabled else None
    live_tool_char_budget, provider_context_tokens = _live_tool_result_char_budget(backend, base_url)
    print(f"📐 [context budget] provider_n_ctx={provider_context_tokens}; "
          f"raw_tool_fraction={RAW_TOOL_CONTEXT_FRACTION:.2f}; "
          f"raw_tool_chars={live_tool_char_budget}")
    # A mutation opens a deterministic verification phase. The actor may
    # choose how to validate, but it cannot make another edit or deliver until
    # one validation action succeeds. This is an engine policy, independent of
    # model, provider, task wording, or tool names beyond their capabilities.
    validation_required = False
    validation_failures = 0
    repair_required = False
    last_validation_failure = ""
    last_repair_packet = ""
    validation_failures_total = 0
    probe_quality_recovery_pending = False
    repair_mode_entries = 0
    repair_mutations = 0
    revalidation_attempts = 0
    successful_repair_cycles = 0
    repair_mutation_pending = False
    completion_nudge_pending = False
    repair_inspection_used = False
    last_mutation_rejected = False
    blocked_mutation_paths = set()
    blocked_progress_helper_paths = set()
    protected_edit_recovery_pending = False
    repair_turns_used = 0
    repair_recovery_mode = False
    rejected_mutation_read_pending = False
    last_rejected_mutation_path = ""
    process_status_used = False
    tool_plane_recovery_attempts = 0
    repair_recovery_entries = 0
    # The last accepted mutation is the minimum durable source context needed
    # for a fresh repair checkpoint.  It prevents a stale action transcript
    # from surviving a blocked inspection without throwing away the code the
    # actor just created.
    last_mutation_checkpoint = None
    # Preserve the most recent focused repair reads when the next prompt is
    # compacted to a mutation checkpoint. Without this, the policy correctly
    # removes broad read tools but also removes the source the actor just
    # inspected, encouraging guessed paths and placeholder patches.
    last_repair_inspection_checkpoint = []
    # Permit a small bounded set of related product mutations after the first
    # successful write so multi-file changes can reach a coherent validation
    # point. The allowance is consumed immediately and never opens an
    # unbounded edit loop.
    validation_batch_remaining = 0
    # The last failed test invocation is safe to replay automatically after a
    # successful product mutation. Keeping its exact argv avoids spending a
    # model turn on a validation request the engine already knows is needed.
    last_failed_test_request = None
    orientation_turns_without_mutation = 0
    orientation_recovery_read_used = False
    no_action_turns = 0
    lifecycle = LifecycleFSM()
    stale_service_restart_pending = False
    stale_service_restart_note = ""
    agent_started_at = time.monotonic()
    first_tool_elapsed = None
    first_mutation_elapsed = None
    first_validation_elapsed = None

    def repair_metrics():
        return {
            "lifecycle": lifecycle.metrics(),
            "validation_failures": validation_failures_total,
            "repair_mode_entries": repair_mode_entries,
            "repair_mutations": repair_mutations,
            "repair_turns": repair_turns_used,
            "repair_recovery_entries": repair_recovery_entries,
            "orientation_turns_without_mutation": orientation_turns_without_mutation,
            "revalidation_attempts": revalidation_attempts,
            "successful_repair_cycles": successful_repair_cycles,
            "transaction": transaction.metrics() if transaction is not None else None,
        }

    def timing_metrics():
        return {
            "first_tool_s": round(first_tool_elapsed, 3) if first_tool_elapsed is not None else None,
            "first_mutation_s": round(first_mutation_elapsed, 3) if first_mutation_elapsed is not None else None,
            "first_validation_s": round(first_validation_elapsed, 3) if first_validation_elapsed is not None else None,
        }
    validation_plan = validation_contract.from_task(task, task_type)
    validation_evidence = set()
    validation_criteria_hits = set()

    def close_novelty_context():
        print(f"🧰 [repair metrics] {json.dumps(repair_metrics(), sort_keys=True)}")
        print(f"⏱️ [agent timing] {json.dumps(timing_metrics(), sort_keys=True)}")
        cleanup_background_processes()
        if novelty_context is not None:
            novelty_context.close()
            print(f"🧬 [novelty metrics] {json.dumps(novelty_context.metrics(), sort_keys=True)}")

    system_prompt = f"""You are a Principal Software Engineer running locally via hardware acceleration.
You are working inside this directory: {get_root()}
Every tool you have is confined to this directory and its subdirectories — you cannot read or write
anything outside it, and attempts to do so will be rejected.

Every path you pass to a tool is ALREADY relative to that directory. Pass just "src/app.py", never prefix
it with the directory's own name — doing so creates an unwanted nested directory instead of reaching the
real file.

You have a registry of tools. The CURRENT TOOL CONTRACT appended to each turn is authoritative; a tool not
listed there is invalid for that turn, even if it appeared in an earlier message or in this registry.

- Use patch_file for small surgical edits; `search` must match the existing text exactly — call read_file
  first if you're not certain of the current contents.
- Use find_files/grep_dir/list_symbols/list_dir to understand code before changing it (grep_dir searches the whole
  project at once — prefer it over calling search_file file-by-file). Use run_tests for unittest projects and
  run_command with an explicit argv list for pytest or the project's native test command; use
  diff_files or git_diff to review one.
- Do not re-explore a file you've already read in full just to "be sure" — if you already have its content
  (including via a pruned entry you called recall(N) on), start editing. Re-reading the same file repeatedly
  without attempting an edit is a sign you're stalling, not being careful; a wrong first attempt you fix with
  a follow-up patch is cheaper than never attempting one. If a pruned tool result's exact text is what you
  need before patch_file, call recall(entry_number) to get it back — don't re-run read_file for something
  you've already seen.
- A real past run on a task like this one reached iteration 23 with zero edit attempts, then said, verbatim:
  "I see the issue - I was reading the file repeatedly instead of making edits." That sentence, once you
  find yourself thinking something like it, means you already have enough information — the next tool call
  should be patch_file or write_file, not another read. Don't wait until you'd say that sentence yourself;
  treat "have I read this before?" as the question to ask BEFORE each read_file call, not after several.
- Use git_status/git_diff to review everything you've changed so far before calling finish_task, if the
  project is a git repository.
- Use web_search to find information or documentation, fetch to read a specific URL's full content.
- Files you write are NOT automatically executed — this isn't a throwaway sandbox, so verify your own work
  explicitly with run_tests or run_command rather than assuming a write succeeded because it didn't error.
- Dependency policy: the workspace has internet access. Inspect project declarations first. If a dependency is
  explicitly required by the task or needed by the application, install it through the project's normal workflow and
  record it in the dependency declaration. Do not invent a third-party dependency merely for an ad hoc probe; for
  probe-only code, prefer the standard library or an existing project dependency. For long-running services, do not
  use a foreground startup command as the behavioral check; use a bounded background lifecycle and probe the service.
- For a long-running command, call run_command or run_shell with background=true. Save the returned handle, use
  process_status to inspect logs/readiness, and call stop_process when finished. Do not use a foreground timeout as
  evidence that a service is broken.
- When — and only when — the task is fully complete, call finish_task with a short summary of what you did.
  Returning plain text without calling finish_task does not end the task; you are expected to keep working."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    for iteration in range(1, iteration_budget + 1):
        lifecycle.transition("turn")
        print(f"\n🌀 [Iteration {iteration}/{iteration_budget}] Calling {MODEL}...")

        # The summary is appended fresh here, for this call only — never
        # baked into `messages` itself. `messages` stays 100% append-only
        # and byte-stable across calls, so Ollama's prefix cache gets full
        # reuse on it; only this trailing block (small) plus whatever's
        # genuinely new this turn needs fresh processing. Putting a
        # genuinely-reworded summary in messages[0] instead (tried first)
        # invalidates the cache for the ENTIRE prompt on every call, since
        # cache matching requires an unbroken identical prefix from
        # position 0 — confirmed live: prompt-eval time grew from ~2s to
        # 87s over 15 iterations before this fix.
        if structured_summary_enabled and (state.files_explored or state.facts_accumulated):
            if ENABLE_PRUNING and entry_positions:
                recall_note = (
                    f"\n\n(Entries 1-{len(entry_positions)} so far. Some older raw tool outputs have been "
                    f"pruned from the conversation to save space — FilesExplored/FactsFound above already "
                    f"capture what's durable from them. Before calling patch_file on a file you haven't seen "
                    f"in the last few turns, call recall(entry_number) to get its EXACT text back — do NOT "
                    f"call read_file again for something you've already read once; that's redundant and slow.)"
                )
            else:
                recall_note = (
                    "\n\n(Nothing has been pruned — every tool result you've seen this run is still "
                    "present above in full, unmodified. Do NOT call read_file again for something you've "
                    "already read once; that's redundant and slow.)"
                )
            messages_for_call = messages + [{"role": "system", "content": state.render() + recall_note}]
        elif working_state_enabled and ws.revision > 0:
            # Only inject once at least one checkpoint has actually landed —
            # an empty just-constructed WorkingState has nothing worth
            # showing over the raw task text already in `messages`.
            recall_note = (
                f"\n\n(Entries 1-{len(entry_positions)} so far. Some older raw tool outputs may have been "
                f"pruned to save space — the working state above already captures what's durable from them "
                f"(facts, decisions, active files). If you need a pruned entry's EXACT text back (e.g. before "
                f"patch_file), call recall(entry_number) — don't re-run read_file for something you've "
                f"already seen.)"
                if entry_positions else ""
            )
            messages_for_call = messages + [{"role": "system", "content": working_state.render(ws) + recall_note}]
        elif context_summary_enabled and context_summary:
            recall_note = (
                f"\n\n(Entries 1-{len(entry_positions)} so far. Some older raw tool outputs have been "
                f"pruned from the conversation to save space — if you need the EXACT original text "
                f"(e.g. before patch_file), call recall(entry_number) to get it back verbatim.)"
                if entry_positions else ""
            )
            messages_for_call = messages + [{
                "role": "system",
                "content": "## Running summary (current state — replaces any earlier version, not a log)\n"
                            + context_summary + recall_note,
            }]
        else:
            messages_for_call = messages

        setup_failure = _is_validation_setup_failure(
            f"{last_repair_packet}\n{last_validation_failure}"
        )
        if validation_required:
            uncovered = validation_plan.uncovered_endpoints(validation_criteria_hits)
            batch_instruction = (
                " If distinct requested product artifacts still need implementation, you may make "
                f"up to {validation_batch_remaining} related product mutation(s) before validating; "
                "do not edit an already changed artifact."
                if validation_batch_remaining > 0 and not repair_required else ""
            )
            messages_for_call = messages_for_call + [{
                "role": "system",
                "content": (
                    "## Verification required\n"
                    "A previous mutation succeeded but has not yet been independently validated. "
                    "Perform one concrete validation action now using run_tests, run_command, or "
                    "run_shell. Do not edit files or call finish_task until validation succeeds. "
                    "The check must exercise the changed behavior or artifact, not merely list or "
                    "diff the file. For APIs, send representative requests and assert status codes "
                    "and response structure; for CLIs, check exit status and output; for libraries, "
                    "run the focused regression test. If a temporary probe is needed, run it inline "
                    "with run_command/run_shell (for example, a single-line node -e or python -c "
                    "command). Keep short probes single-line when practical; multiline argv code is also "
                    "valid for run_command. If a helper file is clearer, write it "
                    "only below .agentic/ and run that helper; do not edit product or supplied test files. "
                    "Record failures and repair from their evidence."
                    + batch_instruction + " "
                    + validation_plan.render() + "\n"
                    + ("Required interfaces still without accepted evidence: " + ", ".join(uncovered) + "\n"
                       if uncovered else "")
                    + (f"The previous validation failed {validation_failures} time(s); use the failure "
                       "output to choose the next targeted check." if validation_failures else "")
                ),
            }]

        if completion_nudge_pending and not validation_required:
            messages_for_call = messages_for_call + [{"role": "system", "content": (
                "## Verified completion is available\n"
                "The latest independent validation succeeded and all required evidence is covered. "
                "Do not inspect or edit the workspace again. Call finish_task now with a short summary."
            )}]
            completion_nudge_pending = False

        if novelty_context is not None:
            if novelty_action_critic and novelty_context.requires_progress():
                messages_for_call = _intervention_messages(messages_for_call)
            messages_for_call = messages_for_call + [{
                "role": "system", "content": novelty_context.render_for_model(
                    action_critic=novelty_action_critic),
            }]

        if stale_service_restart_pending or stale_service_restart_note:
            restart_text = (
                "The engine could not automatically refresh the old managed service. Stop it, launch exactly "
                "one fresh process from the updated workspace, then run the focused behavioral validation."
                if stale_service_restart_pending else
                "The engine automatically refreshed the managed service using its original command. Run the "
                "focused behavioral validation now; do not restart it again."
            )
            messages_for_call = messages_for_call + [{"role": "system", "content": restart_text}]
            stale_service_restart_pending = False
            stale_service_restart_note = ""

        # The graduated progress governor — evaluated using the ledger's
        # state as of the END of the previous iteration (this iteration's
        # tool calls haven't happened yet). Replaces the old blunt
        # FORCE_EDIT_AFTER_ITERATIONS gate; see escalation_governor.py's
        # docstring for why graduated intervention beats a single hard cutoff.
        tools_for_call = tools
        if validation_required:
            print(f"🔒 [validation phase active] failures={validation_failures}")
        if action_first and iteration == 1:
            # A model-neutral interaction policy: reduce choice overload for
            # the first executable turn, then restore the complete registry.
            # This is deliberately independent of provider, model name, and
            # quantization; it is an engine-level contract for agents that
            # need to begin changing a workspace promptly.
            first_action_names = {"write_file", "patch_file", "read_file",
                                  "list_workspace", "find_files", "run_tests",
                                  "run_command", "finish_task"}
            initial_action_instruction = (
                "Initial action contract: take one concrete executable step now. Use the "
                "available file or command tool to begin the requested work; do not return "
                "a plan or broad exploration. The full toolbelt returns after this turn."
            )
            if "run_tests" in {t.__name__ for t in tools} and _has_test_artifacts(workspace_listing):
                # Test-first is a general engineering workflow rule. It does
                # not name a runner, fixture, language, or expected fix; it
                # simply obtains the cheapest authoritative failure signal
                # before the actor spends turns inspecting or editing.
                first_action_names = {"run_tests"}
                initial_action_instruction = (
                    "A conventional test artifact is present. Run the supplied tests first with "
                    "run_tests; do not inspect or edit files until their actual result is known."
                )
            tools_for_call = [t for t in tools if t.__name__ in first_action_names]
            messages_for_call = messages_for_call + [{
                "role": "system",
                "content": initial_action_instruction,
            }]
            print(f"🎯 [initial action contract] {[t.__name__ for t in tools_for_call]}")
        if _governed and ledger.history:
            signal = progress_governor.evaluate(ledger, contract, task=task, repo_size_hint=repo_size_hint)
            last_dup = next(
                (e["scope"] for e in reversed(ledger.history) if e["is_duplicate"]), None
            )
            level = escalation.update(signal, last_duplicate_scope=last_dup)
            current_level = level
            if level == 5 and escalation.consecutive_no_progress >= HARD_STOP_AFTER_TURNS:
                mutations = [e for e in ledger.history if e["capability"] == "MUTATE"]
                succeeded = sum(1 for e in mutations if e["succeeded"] is True)
                failed = sum(1 for e in mutations if e["succeeded"] is False)
                TASK_STATE["done"] = True
                TASK_STATE["summary"] = (
                    f"Harness-forced stop: {escalation.consecutive_no_progress} consecutive no-progress "
                    f"turns at escalation level 5 — the model did not comply with the forced finish_task "
                    f"restriction. {succeeded} mutation(s) succeeded, {failed} failed, out of "
                    f"{len(mutations)} attempted. This summary was generated by the harness, not the model "
                    f"— the task may be incomplete."
                )
                print(f"\n🛑 [hard stop] {TASK_STATE['summary']}")
                # Return directly rather than `break` — the loop's own
                # TASK_STATE["done"] check (which drives the normal ✅ DONE /
                # return True path) only runs AFTER a full iteration's
                # dispatch completes, so a bare `break` here would fall
                # through to the "❌ INCOMPLETE" branch below and silently
                # report this as a failed run despite TASK_STATE["done"]
                # being True — a real bug caught while writing this fix,
                # not live (no run has hit this path with a bare break yet).
                return True
            if level > 0:
                intervention_msg, restricted_names = escalation_governor.build_intervention(
                    level, signal, contract, {t.__name__ for t in tools}, escalation
                )
                if restricted_names is not None:
                    tools_for_call = [t for t in tools if t.__name__ in restricted_names]
                    print(f"🚦 [governor level {level}] tools restricted to {sorted(restricted_names)}")
                else:
                    print(f"🚦 [governor level {level}] nudge only, full toolbelt still offered")
                if intervention_msg:
                    checkpoint_note = (
                        " Any patch_file/write_file call is automatically checkpointed before it runs — "
                        "an attempt that turns out wrong isn't unrecoverable."
                        if level >= 4 else ""
                    )
                    messages_for_call = messages_for_call + [{
                        "role": "system",
                        "content": f"[progress governor — level {level}] {intervention_msg}{checkpoint_note}",
                    }]

        orientation_recovery_active = lifecycle.state == LifecycleState.RECOVER
        orientation_evidence_available = False
        setup_failure = False
        if (not validation_required and not repair_required
                and lifecycle.state == LifecycleState.ACT
                and orientation_turns_without_mutation >= ORIENTATION_TURN_BUDGET):
            lifecycle.transition("orientation_stalled")
            orientation_recovery_active = True
            print("🧭 [FSM] ACT -> RECOVER: orientation budget exhausted")

        if orientation_recovery_active and not validation_required and not repair_required:
            # RECOVER is the lifecycle-owned recovery surface.  Before useful
            # evidence exists, one targeted read is still legal. Once source
            # evidence exists, a code-change contract has a missing MUTATE
            # capability, so the FSM offers only mutation tools. This removes
            # the old loophole where the actor could receive repeated
            # read/validation tools forever while the lifecycle stayed ACT.
            evidence_available = _has_orientation_evidence(messages)
            orientation_evidence_available = evidence_available
            orientation_tools = lifecycle_policy.orientation_action_tools(
                evidence_available=evidence_available,
                recovery_read_used=orientation_recovery_read_used,
            )
            if evidence_available and not orientation_recovery_read_used:
                orientation_instruction = (
                    "Source evidence exists, but one bounded focused read/search remains available in case "
                    "the required file was not actually seen. Use it only if needed; after that, make one "
                    "concrete implementation change immediately. Do not browse broadly or return a plan."
                )
            elif evidence_available:
                orientation_instruction = (
                    "The bounded recovery read has been used and source evidence is available. A code "
                    "mutation is now required. Use patch_file or write_file immediately; do not read, "
                    "search, run a probe, or return a plan."
                )
            else:
                orientation_instruction = (
                    "No useful inspection evidence is present yet. Take at most one targeted read/search, "
                    "then make one concrete implementation change or run focused validation."
                )
            tools_for_call = [t for t in tools_for_call if t.__name__ in orientation_tools]
            messages_for_call = messages_for_call + [{
                "role": "system",
                "content": (
                    "## Orientation budget exhausted\n"
                    f"The last {orientation_turns_without_mutation} turns produced no mutation. "
                    + orientation_instruction + " Do not return a plan."
                ),
            }]
            print(
                f"🧭 [FSM recovery] {orientation_turns_without_mutation} turns without mutation; "
                f"restricting the next turn to {'mutation' if evidence_available and orientation_recovery_read_used else 'focused-progress'} "
                f"tools (evidence={'yes' if evidence_available else 'no'}, "
                f"recovery_read_used={'yes' if orientation_recovery_read_used else 'no'})"
            )

        if validation_required:
            # One pure policy function derives the complete validation tool
            # surface.  This keeps the FSM state, setup/behavior plane, and
            # repair budget from being interpreted by separate branches.
            setup_failure = _is_validation_setup_failure(
                f"{last_repair_packet}\n{last_validation_failure}"
            )
            validation_policy = lifecycle_policy.build_validation_policy(
                validation_required=validation_required,
                repair_required=repair_required,
                setup_failure=setup_failure,
                repair_inspection_used=repair_inspection_used,
                last_mutation_rejected=last_mutation_rejected,
                validation_failures=validation_failures,
                protected_edit_recovery_pending=protected_edit_recovery_pending,
                repair_recovery_mode=repair_recovery_mode,
                rejected_mutation_read_pending=rejected_mutation_read_pending,
                mutation_batch_remaining=validation_batch_remaining,
                accepted_validation_evidence=bool(validation_evidence),
                background_process_active=bool(active_background_handles()),
                process_status_used=process_status_used,
                probe_quality_recovery=(
                    probe_quality_recovery_pending
                    and _has_test_artifacts(workspace_listing)
                ),
            )
            validation_tools = set(validation_policy.tools if validation_policy else ())
            gate_banned = _consume_worker_gate(novelty_action_gate, novelty_context)
            # The synchronous worker is advisory.  Once the deterministic
            # validation policy has classified the active failure as product
            # behavior, stale setup advice must not remove the only legal
            # mutation tools.  The worker may narrow a behavior state (for
            # example by removing finish_task), never invert its plane.
            gate_banned = _authoritative_gate_restrictions(gate_banned, setup_failure)
            if gate_banned:
                validation_tools -= gate_banned
                print(f"🚦 [4B triage gate] tools removed: {sorted(gate_banned)}")
            tools_for_call = [t for t in tools_for_call if t.__name__ in validation_tools]
            if validation_required and not repair_required and not setup_failure:
                # The validation FSM owns this capability plane. Escalation
                # may narrow exploratory tools, but it cannot remove the
                # executable check needed to satisfy the active contract.
                tools_for_call = [t for t in tools if t.__name__ in validation_tools]
            validation_prompt = (validation_policy.prompt if validation_policy else "")
            if repair_required:
                validation_prompt += (
                    " Treat the validation script as evidence, not the artifact: do not weaken or rewrite "
                    "the probe. When actual and expected values differ, repair the implementation's meaning, "
                    "shape, ordering, or state transition—not just formatting. Failure evidence:\n"
                    + last_repair_packet
                    + "\nDeterministic repair authority: do not dismiss this failed or timed-out check "
                    "because a different check passed. Preserve unrelated passing behavior and inspect "
                    "the endpoint, handler, or artifact named by this failure packet; do not edit a "
                    "different healthy component merely to create a mutation."
                )
            messages_for_call = messages_for_call + [{
                "role": "system",
                "content": validation_prompt,
            }]

        if rejected_mutation_read_pending and repair_required and not setup_failure:
            if structured_summary_enabled and state is not None:
                checkpoint_state = state.render()
            elif working_state_enabled and ws is not None and ws.revision > 0:
                checkpoint_state = working_state.render(ws)
            else:
                checkpoint_state = ""
            messages_for_call = _rejected_mutation_inspection_messages(
                messages,
                last_repair_packet=last_repair_packet,
                target_path=last_rejected_mutation_path,
                state_text=checkpoint_state,
            )
        elif _force_repair_recovery(repair_recovery_mode, repair_required, setup_failure):
            # Keep the stable task foundation and recent evidence, then force
            # a concrete mutation. This is a compact repair checkpoint, not a
            # second unbounded transcript.
            if structured_summary_enabled and state is not None:
                checkpoint_state = state.render()
            elif working_state_enabled and ws is not None and ws.revision > 0:
                checkpoint_state = working_state.render(ws)
            else:
                checkpoint_state = ""
            messages_for_call = _repair_checkpoint_messages(
                messages,
                last_repair_packet=last_repair_packet,
                mutation_checkpoint=last_mutation_checkpoint,
                state_text=checkpoint_state,
            )
        elif repair_required and repair_inspection_used and not setup_failure:
            # A trusted traceback excerpt already localizes the repair. Keep
            # the next actor prompt small instead of carrying the full stale
            # validation transcript into a mutation-only call.
            if structured_summary_enabled and state is not None:
                checkpoint_state = state.render()
            elif working_state_enabled and ws is not None and ws.revision > 0:
                checkpoint_state = working_state.render(ws)
            else:
                checkpoint_state = ""
            messages_for_call = _source_backed_repair_messages(
                messages,
                last_repair_packet=last_repair_packet,
                state_text=checkpoint_state,
                inspection_checkpoint=last_repair_inspection_checkpoint,
            )

        # Apply the novelty gate after every lifecycle/validation policy. A
        # policy layer may narrow this surface, never re-expand it after the
        # ledger has declared that state-changing progress is required.
        novelty_progress_tools = (
            _novelty_progress_tool_names(
                novelty_context,
                helper_mutation_blocked=bool(blocked_progress_helper_paths),
                rejected_mutation_read_pending=rejected_mutation_read_pending,
            )
            if novelty_action_gate else None
        )
        if novelty_progress_tools is not None:
            repeated_action = novelty_context.repeated_validation_loop()
            if blocked_progress_helper_paths:
                # Add the recovery-only aliases at the transition itself, not
                # at run startup. This keeps the ordinary model prompt and
                # tool schema unchanged until a helper rejection proves the
                # generic mutation surface is being misapplied.
                for recovery_tool in (patch_product_file, write_product_file):
                    if recovery_tool.__name__ not in tool_map:
                        tools.append(recovery_tool)
                        tool_map[recovery_tool.__name__] = recovery_tool
            # Validation/lifecycle policy may have removed the recovery-only
            # product aliases before this final gate runs. Add only those
            # aliases back from the trusted registry; never re-expand the
            # rest of the tool surface here.
            progress_candidates = list(tools_for_call)
            if blocked_progress_helper_paths:
                progress_candidates.extend(
                    tool for tool in tools
                    if tool.__name__ in PRODUCT_MUTATION_TOOLS
                    and tool not in progress_candidates
                )
            tools_for_call = [
                tool for tool in progress_candidates
                if tool.__name__ in novelty_progress_tools
            ]
            if repeated_action:
                gate_message = (
                    "The same non-mutating action repeated. Validation and inspection are unavailable; "
                    "make one targeted mutation or call finish_task."
                )
            elif novelty_context.recovery_reads_allowed():
                gate_message = (
                    "The bounded context window requires progress. Use one targeted read if needed, "
                    "then make a mutation or call finish_task."
                )
            else:
                gate_message = (
                    "The bounded context window is exhausted without a mutation. Validation is not progress; "
                    "make one targeted mutation or call finish_task."
                )
            if blocked_progress_helper_paths:
                gate_message += (
                    " A validation-helper mutation was rejected on the previous turn. The generic mutation "
                    "tools are unavailable; use patch_product_file or write_product_file on the product "
                    "source implicated by the task, or call finish_task."
                )
            messages_for_call = messages_for_call + [{
                "role": "system",
                "content": "[novelty context action gate] " + gate_message,
            }]

        # The transaction buffer is a short-lived control-plane pin.  Add it
        # after repair/context checkpoint construction so a source-backed or
        # compacted prompt cannot accidentally drop the list of files that
        # must be aligned.  It is derived entirely by the host and never
        # becomes mutable model state.
        if transaction is not None:
            transaction_status = transaction.control_block()
            if transaction_status:
                messages_for_call = messages_for_call + [{
                    "role": "system",
                    "content": transaction_status,
                }]

        # The stable system prompt lists the full registry for orientation,
        # but lifecycle policy may narrow the legal surface for this turn.
        # State that narrowed contract explicitly at the changing tail so a
        # provider cannot safely fall back to a stale tool name from the
        # original prompt or an earlier assistant message.
        current_tool_names = sorted({tool.__name__ for tool in tools_for_call})
        messages_for_call = messages_for_call + [{
            "role": "system",
            "content": (
                "## CURRENT TOOL CONTRACT\n"
                "For this turn, call exactly one tool from this list and no other tool: "
                + (", ".join(current_tool_names) if current_tool_names else "(none)")
                + ". A tool name not in this list is invalid."
            ),
        }]

        response = None
        last_error = None
        contract_retry_used = False
        for attempt in range(1, MAX_CHAT_RETRIES + 1):
            try:
                chat_kwargs = dict(
                    timeout_seconds=chat_timeout, model=MODEL, messages=messages_for_call,
                    tools=tools_for_call, think=think, options={"num_ctx": NUM_CTX},
                    backend=backend, base_url=base_url,
                )
                if action_first and iteration == 1 and backend == "llama-cpp":
                    # llama-server accepts the OpenAI-compatible string form
                    # here.  Its current API rejects the structured
                    # {type:function,function:{name:...}} form, so keep the
                    # provider translation at this boundary rather than
                    # leaking a llama.cpp-specific choice into the agent.
                    chat_kwargs["tool_choice"] = "required"
                    chat_kwargs["max_tokens"] = FORCED_ACTION_MAX_TOKENS
                if (orientation_recovery_active and orientation_evidence_available
                        and contract.requires("MUTATE") and backend == "llama-cpp"):
                    # The FSM has reduced the legal registry to mutation
                    # tools. Require one tool call at the provider boundary as
                    # well, so a plain explanation cannot consume another
                    # recovery turn.
                    chat_kwargs["tool_choice"] = "required"
                if (orientation_recovery_active and orientation_evidence_available
                        and orientation_recovery_read_used and backend == "llama-cpp"
                        and tools_for_call):
                    # Recovery has consumed its one read opportunity. Require
                    # a tool from the mutation/progress surface even when the
                    # novelty worker has not yet reached its event interval.
                    chat_kwargs["tool_choice"] = "required"
                    chat_kwargs["max_tokens"] = FORCED_ACTION_MAX_TOKENS
                    print("🧭 [orientation recovery] requiring one legal progress tool")
                if _force_tool_call_after_no_action(no_action_turns, backend):
                    # Repeated prose-only turns are a transport/control-plane
                    # failure, not a reasoning opportunity. llama.cpp can
                    # enforce the structured tool boundary directly; this is
                    # independent of model name and task wording.
                    chat_kwargs["tool_choice"] = "required"
                    print(
                        f"🧰 [no-action escalation] {no_action_turns} prose-only turns; "
                        "requiring an executable tool call"
                    )
                    chat_kwargs["max_tokens"] = FORCED_ACTION_MAX_TOKENS
                if rejected_mutation_read_pending and backend == "llama-cpp":
                    chat_kwargs["tool_choice"] = "required"
                    chat_kwargs["max_tokens"] = FORCED_ACTION_MAX_TOKENS
                    print("🧰 [rejected mutation recovery] requiring one focused inspection tool")
                elif repair_recovery_mode and repair_required and backend == "llama-cpp":
                    # Recovery has already exhausted the ordinary repair
                    # turns and the registry contains only a targeted
                    # mutation/finish surface. Do not spend two more prose
                    # turns asking the actor to choose from that surface.
                    chat_kwargs["tool_choice"] = "required"
                    chat_kwargs["max_tokens"] = FORCED_ACTION_MAX_TOKENS
                    print("🧰 [repair recovery escalation] requiring a targeted tool call")
                elif repair_required and repair_inspection_used and backend == "llama-cpp":
                    # A trusted source excerpt has already satisfied the
                    # localization step. The legal surface is mutation-only;
                    # enforce that boundary at the provider as well as in
                    # dispatch so a stale/read-oriented response cannot burn
                    # another repair turn.
                    chat_kwargs["tool_choice"] = "required"
                    chat_kwargs["max_tokens"] = FORCED_ACTION_MAX_TOKENS
                    print("🧰 [source-backed repair] requiring a targeted tool call")
                if _progress_tool_call_required(
                    novelty_context, novelty_action_gate, backend, tools_for_call
                ):
                    # The final tool list has already been narrowed by the
                    # deterministic gate. Enforce that the actor selects
                    # from that list instead of replaying a stale call from
                    # an earlier prompt turn.
                    chat_kwargs["tool_choice"] = "required"
                    chat_kwargs["max_tokens"] = FORCED_ACTION_MAX_TOKENS
                    print("🧰 [progress gate] requiring one legal progress tool")
                response = _chat_with_timeout(**chat_kwargs)
                stale_tools = _stale_tool_names(
                    getattr(getattr(response, "message", None), "tool_calls", None),
                    {tool.__name__ for tool in tools_for_call},
                )
                if (
                    stale_tools
                    and not contract_retry_used
                    and tools_for_call
                    and len({tool.__name__ for tool in tools_for_call}) < len(tool_map)
                ):
                    # Some OpenAI-compatible local servers accept
                    # tool_choice=required but do not enforce the selected
                    # name. Retry once inside the same logical turn so a
                    # remembered tool cannot consume an iteration or enter
                    # the repair transcript. The host still chooses no
                    # mutation and reveals no task-specific answer.
                    contract_retry_used = True
                    current_names = ", ".join(sorted({tool.__name__ for tool in tools_for_call}))
                    messages_for_call = messages_for_call + [{
                        "role": "system",
                        "content": (
                            "The previous response named unavailable tool(s): "
                            + ", ".join(stale_tools)
                            + ". That call was not executed. Retry this same turn using exactly one "
                            "currently available tool: " + current_names + ". Do not mention or call "
                            "the unavailable tool again."
                        ),
                    }]
                    print(
                        f"🔁 [tool contract retry] unavailable tool(s) {list(stale_tools)}; "
                        f"requesting one of {current_names}"
                    )
                    continue
                break
            except ChatTimeoutError as e:
                last_error = e
                recent_errors.append(f"iter {iteration}: {e}")
                recent_errors[:] = recent_errors[-5:]
                print(f"⚠️  {e}; ending run cleanly so a stalled model call cannot strand the benchmark")
                break
            except PromptBudgetError as e:
                last_error = e
                recent_errors.append(f"iter {iteration}: prompt budget: {e}")
                recent_errors[:] = recent_errors[-5:]
                print(f"⚠️  {e}; ending run cleanly instead of retrying an oversized prompt")
                break
            except Exception as e:
                last_error = e
                # httpx/ollama transport timeouts arrive as provider-specific
                # ReadTimeout exceptions rather than ChatTimeoutError. They
                # are terminal for this turn; retrying them up to twenty
                # times defeated the benchmark's time bound in practice.
                if "timeout" in f"{type(e).__name__}: {e}".lower():
                    recent_errors.append(f"iter {iteration}: transport timeout: {e}")
                    recent_errors[:] = recent_errors[-5:]
                    print(f"⚠️  transport timeout; ending run cleanly: {e}")
                    break
                error_text = f"{type(e).__name__}: {e}".lower()
                if "xml syntax error" in error_text and attempt >= 2:
                    recent_errors.append(f"iter {iteration}: repeated malformed tool response: {e}")
                    recent_errors[:] = recent_errors[-5:]
                    print(f"⚠️  repeated malformed tool response; ending run cleanly: {e}")
                    break
                if _terminal_provider_error(e):
                    recent_errors.append(f"iter {iteration}: provider unavailable: {e}")
                    recent_errors[:] = recent_errors[-5:]
                    if _retryable_provider_disconnect(e, attempt):
                        print(f"⚠️  transient provider disconnect; retrying once: {e}")
                        time.sleep(1)
                        continue
                    print(f"⚠️  provider unavailable; ending run cleanly instead of retrying: {e}")
                    break
                print(f"⚠️  chat() failed (attempt {attempt}/{MAX_CHAT_RETRIES}): {type(e).__name__}: {e}")
                recent_errors.append(f"iter {iteration}: chat() {type(e).__name__}: {e}")
                recent_errors[:] = recent_errors[-5:]
                if attempt < MAX_CHAT_RETRIES:
                    time.sleep(min(120, 2 ** attempt))
        if response is None:
            print(f"\n❌ acting-model turn failed — ending run cleanly rather than crashing. Last error: {last_error}")
            TASK_STATE["summary"] = f"Run stopped before a response: {last_error}"
            if status_path:
                status_report.write(
                    status_path, iteration=iteration, iteration_budget=iteration_budget,
                    last_action_classification="MODEL_TIMEOUT" if isinstance(last_error, ChatTimeoutError) else "MODEL_ERROR",
                    escalation_level=current_level,
                    ledger_size=len(ledger.history) if _governed else None,
                    distributions_done=status_report.classes_with_method(
                        io_tools._resolve(distribution_target_file), distribution_names
                    ) if distribution_target_file and distribution_names else None,
                    recent_errors=list(recent_errors), task_done=False,
                    task_summary=TASK_STATE["summary"],
                    novelty_context=novelty_context.metrics() if novelty_context else None,
                )
            close_novelty_context()
            return False

        prompt_eval_s = (response.prompt_eval_duration or 0) / 1e9
        eval_s = (response.eval_duration or 0) / 1e9
        print(f"📏 prompt_tokens={response.prompt_eval_count} ({prompt_eval_s:.1f}s) "
              f"output_tokens={response.eval_count} ({eval_s:.1f}s)")

        msg = response.message
        messages.append(msg)
        if msg.tool_calls and first_tool_elapsed is None:
            first_tool_elapsed = time.monotonic() - agent_started_at
            print(f"⏱️ [first tool] {first_tool_elapsed:.3f}s")

        if structured_summary_enabled:
            iteration_assistant_idx[iteration] = len(messages) - 1
            iteration_tokens[iteration] = response.eval_count or 0
            newly_compacted = message_compaction.compact_if_needed(
                messages, iteration_assistant_idx, iteration_tokens, compacted_iterations, task
            )
            if newly_compacted:
                print(f"🗜️  [compacted] turns {newly_compacted[0]}-{newly_compacted[-1]} "
                      f"({len(newly_compacted)} turns) — reasoning summarized, tool calls/results untouched")

        if think and getattr(msg, "thinking", None):
            print(f"💭 {msg.thinking}")
        if msg.content:
            print(f"🧠 {msg.content}")

        if not msg.tool_calls:
            no_action_turns += 1
            print("⚠️  Model returned no tool call — nudging it to act.")
            legal_names = {t.__name__ for t in tools_for_call}
            if novelty_context is not None:
                novelty_context.observe_no_action(
                    iteration,
                    msg.content or "",
                    legal_actions=tuple(legal_names),
                )
                print(f"🧬 [novelty recovery] {novelty_context.render_for_model(action_critic=True)}")
            if legal_names & {"run_command", "run_shell", "run_tests"} and not legal_names & ({"patch_file", "write_file"} | PRODUCT_MUTATION_TOOLS):
                next_action = "run_command, run_shell, or run_tests to produce the required behavioral evidence"
            elif legal_names & ({"patch_file", "write_file"} | PRODUCT_MUTATION_TOOLS):
                next_action = "the offered mutation tool to make the targeted repair"
            else:
                next_action = "the next executable tool offered for this lifecycle phase"
            messages.append({
                "role": "user",
                "content": (
                    "Do not return another explanation. Take one executable action now: use "
                    + next_action
                    + "; for a temporary validation probe, use an inline run_command/run_shell or write "
                    "the helper only below .agentic/; call "
                    "finish_task only after verification."
                ),
            })
            continue

        no_action_turns = 0

        tool_start_idx = len(messages)
        turn_calls = list(msg.tool_calls)
        # risk_layer.py: checkpoint every file about to be MUTATEd, BEFORE
        # dispatch actually runs the call — so a bad patch_file/write_file
        # can be rolled back afterward if needed. Uses the same
        # classification action_governor.py's ledger will independently
        # apply to the same call right after dispatch.
        if _governed:
            for call in turn_calls:
                if action_governor.classify(call.function.name, call.function.arguments) == "MUTATE":
                    rel_path = (call.function.arguments or {}).get("path")
                    if rel_path:
                        risk.checkpoint(rel_path, io_tools._resolve(rel_path), iteration)

        # Always enforced against whatever was actually offered THIS call
        # (tools_for_call), not just during forced-edit gating — the same
        # class of bug (a call to a tool the model wasn't offered, recalled
        # from earlier conversation history) could in principle happen any
        # time, not only when gating is active. tool_map itself stays the
        # full, unrestricted registry (dispatch still needs to look up the
        # real function for whatever's actually allowed).
        allowed_names = {t.__name__ for t in tools_for_call}
        blocked_calls = novelty_context.blocked_calls() if novelty_context is not None else None
        blocked_command_calls = set()
        blocked_command_reasons = {}
        blocked_mutation_reasons = {}
        if (novelty_action_gate and novelty_context is not None
                and _novelty_progress_tool_names(
                    novelty_context,
                    helper_mutation_blocked=bool(blocked_progress_helper_paths),
                    rejected_mutation_read_pending=rejected_mutation_read_pending,
                ) is not None):
            # A progress gate is asking for product state change, not another
            # temporary verifier. Keep helper creation legal during ordinary
            # validation, but do not let a helper write satisfy this specific
            # product-progress boundary.
            for call in turn_calls:
                if call.function.name not in {"write_file", "patch_file"} | PRODUCT_MUTATION_TOOLS:
                    continue
                path = (call.function.arguments or {}).get("path", "")
                if lifecycle_policy.is_validation_helper_path(path):
                    blocked_mutation_reasons[_call_key(call.function.name, call.function.arguments)] = (
                        "REJECTED: the novelty progress gate requires a product mutation; validation "
                        "helpers below .agentic/ cannot satisfy this turn. Patch the implicated product "
                        "artifact or call finish_task if it is already complete."
                    )
        if orientation_recovery_active and orientation_evidence_available:
            for call in turn_calls:
                if call.function.name in {"run_command", "run_shell"}:
                    args = call.function.arguments or {}
                    if lifecycle_policy.is_inspection_command(
                        args.get("command", args.get("argv"))
                    ):
                        blocked_command_calls.add(_call_key(call.function.name, args))
                        blocked_command_reasons[_call_key(call.function.name, args)] = (
                            "REJECTED: this shell command only inspects files. Orientation recovery already has "
                            "usable evidence; make the implementation change or run a behavioral validation command."
                        )
        if validation_required:
            # run_command/run_shell are intentionally still available for
            # behavioral checks and dependency setup. Their command contents
            # are a second capability plane, however: a redirect, tee, copy,
            # or inline file-write must not bypass a validation-only wall.
            # Product mutation remains legal in a behavior-repair turn, while
            # setup recovery and ordinary validation must not rewrite the
            # artifact or supplied evidence.
            command_mutation_blocked = not repair_required or setup_failure
            if setup_failure:
                for call in turn_calls:
                    if call.function.name not in {"patch_file", "write_file"} | PRODUCT_MUTATION_TOOLS:
                        continue
                    args = call.function.arguments or {}
                    path = args.get("path", "")
                    if not (lifecycle_policy.is_dependency_manifest_path(path)
                            or lifecycle_policy.is_validation_helper_path(path)):
                        key = _call_key(call.function.name, args)
                        blocked_command_calls.add(key)
                        blocked_command_reasons[key] = (
                            "REJECTED: setup/validation recovery permits mutation only of a dependency "
                            "manifest or a helper below .agentic/; product and test paths remain frozen."
                        )
            if command_mutation_blocked:
                for call in turn_calls:
                    if call.function.name not in {"run_command", "run_shell"}:
                        continue
                    args = call.function.arguments or {}
                    if action_governor.classify(call.function.name, args) == "MUTATE":
                        key = _call_key(call.function.name, args)
                        blocked_command_calls.add(key)
                        blocked_command_reasons[key] = (
                            "REJECTED: validation/setup plane is active and this shell command mutates files. "
                            "Run a behavioral assertion without redirects or file writes; use patch_file/write_file "
                            "only when the FSM enters product repair."
                        )
        tool_messages = dispatch_tool_calls(
            turn_calls, tool_map, allowed_names=allowed_names, blocked_calls=blocked_calls,
            blocked_mutation_paths=blocked_mutation_paths,
            blocked_command_calls=blocked_command_calls,
            blocked_command_reasons=blocked_command_reasons,
            blocked_mutation_reasons=blocked_mutation_reasons,
        )
        orientation_recovery_read_used = _consume_orientation_recovery_read(
            orientation_recovery_active,
            orientation_evidence_available,
            orientation_recovery_read_used,
            blocked_command_calls,
        )
        for call, tmsg in zip(turn_calls, tool_messages):
            path = (call.function.arguments or {}).get("path", "")
            if (
                novelty_action_gate
                and novelty_context is not None
                and _novelty_progress_tool_names(
                    novelty_context,
                    helper_mutation_blocked=bool(blocked_progress_helper_paths),
                    rejected_mutation_read_pending=rejected_mutation_read_pending,
                ) is not None
                and
                lifecycle_policy.is_validation_helper_path(path)
                and tmsg["content"].startswith("REJECTED:")
            ):
                blocked_progress_helper_paths.add(path)

        # Proactive validation hook: when the actor writes a clearly named
        # helper below .agentic/, execute it immediately instead of spending a
        # second model turn to request the obvious next command. This remains
        # bounded and model-agnostic: only the helper path and interpreter are
        # selected deterministically; unknown extensions stay model-controlled.
        auto_calls = []
        auto_messages = []
        product_mutation_landed = any(
            action_governor.classify(call.function.name, call.function.arguments or {}) == "MUTATE"
            and not lifecycle_policy.is_validation_helper_path(
                (call.function.arguments or {}).get("path", "")
            )
            and not tmsg.get("content", "").startswith(("ERROR:", "REJECTED:"))
            for call, tmsg in zip(turn_calls, tool_messages)
        )
        # A failed validation may describe a coherent multi-file refactor.
        # Keep the first repair edit and defer the automatic replay while the
        # bounded mutation batch is open; otherwise the proactive hook makes
        # the batch allowance unreachable by validating after every file.
        pending_product_paths = set()
        if transaction is not None:
            for call, tmsg in zip(turn_calls, tool_messages):
                if action_governor.classify(call.function.name, call.function.arguments or {}) != "MUTATE":
                    continue
                if tmsg.get("content", "").startswith(("ERROR:", "REJECTED:")):
                    continue
                normalized = transaction_buffer.normalize_product_path(
                    get_root(), (call.function.arguments or {}).get("path", "")
                )
                if normalized:
                    pending_product_paths.add(normalized)
        transaction_window_open = _transaction_window_open(
            transaction,
            validation_required=validation_required,
            repair_required=repair_required,
            mutation_batch_remaining=validation_batch_remaining,
            pending_product_paths=pending_product_paths,
        )
        if (
            product_mutation_landed
            and last_failed_test_request
            and not transaction_window_open
            and not any(call.function.name == "run_tests" for call in turn_calls)
            and "run_tests" in tool_map
        ):
            # A failed test is a bounded, already-declared validation action;
            # rerunning it after the repair is deterministic and safe. This
            # hook does not infer a task-specific command or edit anything.
            auto_call = SimpleNamespace(function=SimpleNamespace(
                name="run_tests",
                arguments=dict(last_failed_test_request["arguments"]),
            ))
            auto_calls.append(auto_call)
            auto_messages.extend(dispatch_tool_calls(
                [auto_call], tool_map, allowed_names=allowed_names | {"run_tests"},
                blocked_calls=blocked_calls,
                blocked_mutation_paths=blocked_mutation_paths,
                blocked_command_calls=blocked_command_calls,
                blocked_command_reasons=blocked_command_reasons,
            ))
            print("⚡ [proactive test validation] reran the last failed test after mutation")
        if (
            product_mutation_landed
            and "run_tests" in tool_map
            and not auto_calls
            and not any(call.function.name in {
                "run_tests", "run_command", "run_shell"
            } for call in turn_calls)
        ):
            # The actor may be repairing a task whose external grader is not
            # visible in the workspace. If a conventional nearby Python test
            # target is visible, run it deterministically after the edit.
            # This is structural and bounded to one target per mutation turn;
            # it does not infer a task-specific command or expected result.
            # A failing intermediate result remains inside the host
            # transaction window rather than triggering rollback.
            test_target = None
            for call, tmsg in zip(turn_calls, tool_messages):
                if action_governor.classify(call.function.name, call.function.arguments or {}) != "MUTATE":
                    continue
                if tmsg.get("content", "").startswith(("ERROR:", "REJECTED:")):
                    continue
                test_target = _nearby_python_test_target(
                    (call.function.arguments or {}).get("path", "")
                )
                if test_target:
                    break
            if test_target:
                auto_call = SimpleNamespace(function=SimpleNamespace(
                    name="run_tests", arguments={"path": test_target}
                ))
                auto_calls.append(auto_call)
                auto_messages.extend(dispatch_tool_calls(
                    [auto_call], tool_map,
                    allowed_names=allowed_names | {"run_tests"},
                    blocked_calls=blocked_calls,
                    blocked_mutation_paths=blocked_mutation_paths,
                    blocked_command_calls=blocked_command_calls,
                    blocked_command_reasons=blocked_command_reasons,
                ))
                print(f"⚡ [nearby test validation] ran {test_target} after product mutation")
        if validation_required and not transaction_window_open and "run_command" in allowed_names:
            for call, tmsg in zip(turn_calls, tool_messages):
                if call.function.name not in {"write_file", "patch_file"} | PRODUCT_MUTATION_TOOLS:
                    continue
                path = (call.function.arguments or {}).get("path", "")
                command = _auto_validation_command(path)
                if not command or tmsg.get("content", "").startswith(("ERROR:", "REJECTED:")):
                    continue
                auto_call = SimpleNamespace(function=SimpleNamespace(
                    name="run_command",
                    arguments={"command": command, "timeout": 120, "cwd": ".", "background": False},
                ))
                auto_calls.append(auto_call)
                auto_messages.extend(dispatch_tool_calls(
                    [auto_call], tool_map, allowed_names=allowed_names,
                    blocked_calls=blocked_calls,
                    blocked_mutation_paths=blocked_mutation_paths,
                    blocked_command_calls=blocked_command_calls,
                    blocked_command_reasons=blocked_command_reasons,
                ))
        if auto_calls:
            turn_calls.extend(auto_calls)
            tool_messages.extend(auto_messages)
            print(f"⚡ [proactive validation] executed {len(auto_calls)} check(s) immediately")
        messages.extend(tool_messages)
        # This runs regardless of which optional memory mode is enabled.
        # Novelty context must not leave raw tool output unbounded.
        _bound_live_tool_results(messages, live_tool_char_budget)

        validation_phase_before_turn = validation_required
        repair_turn_before_dispatch = repair_required or validation_failures > 0
        turn_mutated = False
        turn_validation_succeeded = False
        turn_validation_failed = False
        turn_tool_plane_failure = False
        turn_probe_quality_failure = False
        blocked_repair_action = False
        validation_suggestions = []
        for call, tmsg in zip(turn_calls, tool_messages):
            tool_name = call.function.name
            args = call.function.arguments or {}
            if tool_name == "process_status":
                process_status_used = True
            capability = action_governor.classify(tool_name, args)
            result = tmsg.get("content", "")
            if repair_turn_before_dispatch and _is_blocked_repair_action(tool_name, result):
                blocked_repair_action = True
                print(
                    f"⚠️ [repair checkpoint] blocked inspection {tool_name}; "
                    "discarding the stale action transcript before the next repair turn"
                )
            if _governed and capability == "MUTATE" and args.get("path"):
                rejection = risk.reject_destructive_rewrite(
                    args["path"], io_tools._resolve(args["path"]),
                    tool_name=tool_name, repair_turn=repair_turn_before_dispatch,
                )
                if rejection is None:
                    rejection = risk.reject_protected_test_mutation(
                        args["path"], io_tools._resolve(args["path"]),
                        repair_turn=repair_turn_before_dispatch,
                    )
                if rejection:
                    result = "REJECTED: " + rejection
                    tmsg["content"] = result
                    if "protected test" in rejection.lower():
                        blocked_mutation_paths.add(args["path"])
                        protected_edit_recovery_pending = True
                    print(f"🛡️ [risk layer] {result}")
            if repair_required and lifecycle_policy.counts_as_repair_inspection(tool_name):
                repair_inspection_used = True
                if rejected_mutation_read_pending:
                    # The inspection allowance is consumed by an attempted
                    # focused read, even if the read itself fails. This keeps
                    # the recovery surface bounded.
                    rejected_mutation_read_pending = False
                    print("🔎 [rejected mutation recovery] bounded inspection consumed")
                if not result.startswith(("ERROR:", "REJECTED:")):
                    last_repair_inspection_checkpoint.append({
                        "role": "tool",
                        "content": result,
                        "tool_name": tool_name,
                    })
                    last_repair_inspection_checkpoint = last_repair_inspection_checkpoint[-4:]
            if capability == "MUTATE" and result.startswith(("REJECTED:", "ERROR:")):
                last_mutation_rejected = True
                # A fresh recovery checkpoint intentionally drops the old
                # action tail. Preserve the rejection itself so a malformed
                # patch is not replayed without its syntax/error evidence.
                last_repair_packet = (
                    f"Previous {tool_name} mutation was rejected; change the patch rather than "
                    f"repeating it.\n{result}"
                )[-3000:]
                if (
                    not lifecycle_policy.is_validation_helper_path(args.get("path", ""))
                    and "protected test" not in result.lower()
                ):
                    rejected_mutation_read_pending = True
                    last_rejected_mutation_path = str(args.get("path", ""))
                    print(
                        "🔎 [rejected mutation recovery] scheduling one fresh source inspection "
                        f"for {last_rejected_mutation_path or 'the implicated product file'}"
                    )
            success = action_governor.infer_success(capability, tool_name, result)
            if capability == "MUTATE" and result.startswith(("REJECTED:", "ERROR:")):
                # A governor heuristic must never turn a failed mutation into
                # a successful repair cycle and reopen broad editing.
                success = False
            helper_mutation = (
                capability == "MUTATE"
                and lifecycle_policy.is_validation_helper_path(args.get("path", ""))
            )
            if capability == "MUTATE" and success is True and not helper_mutation:
                turn_mutated = True
                last_mutation_rejected = False
                rejected_mutation_read_pending = False
                last_rejected_mutation_path = ""
                if first_mutation_elapsed is None:
                    first_mutation_elapsed = time.monotonic() - agent_started_at
                    print(f"⏱️ [first mutation] {first_mutation_elapsed:.3f}s")
            # During the verification phase, a successful executable check is
            # validation even when the command is an app/API smoke test rather
            # than a pytest command and the general classifier calls it OBSERVE.
            phase_validation = validation_required and tool_name in {
                "run_tests", "run_command", "run_shell", "process_status", "diff_files", "git_diff"
            }
            if phase_validation:
                revalidation_attempts += 1
                if first_validation_elapsed is None:
                    first_validation_elapsed = time.monotonic() - agent_started_at
                    print(f"⏱️ [first validation] {first_validation_elapsed:.3f}s")
            if capability == "VALIDATE" or phase_validation:
                if probe_quality_recovery_pending and tool_name == "run_tests":
                    # The recovery contract has now been exercised. Whether
                    # the suite passes or fails, let the normal validation
                    # state machine classify that authoritative result on the
                    # next turn instead of keeping the one-shot lock forever.
                    probe_quality_recovery_pending = False
                if protected_edit_recovery_pending and tool_name in {
                    "run_tests", "run_command", "run_shell",
                }:
                    protected_edit_recovery_pending = False
                if validation_plan.is_lifecycle_setup(tool_name, args, result):
                    # Process startup/status/cleanup is setup, not proof of
                    # application behavior. Keep the validation phase open
                    # until a test or request produces evidence.
                    continue
                assessment = validation_plan.assess(tool_name, args, result)
                if (success is True or (phase_validation and assessment[0])) and assessment[0] and assessment[3] not in validation_evidence:
                    turn_validation_succeeded = True
                    validation_evidence.add(assessment[3])
                    validation_criteria_hits.update(assessment[4])
                elif success is False or result.startswith(("ERROR:", "REJECTED:")):
                    if validation_contract.is_tool_plane_failure(tool_name, result):
                        turn_tool_plane_failure = True
                        if "syntaxerror" in result.lower() and "<string>" in result.lower():
                            validation_suggestions.append(
                                "the inline interpreter probe is syntactically invalid; use a valid one-line "
                                "probe or write a temporary helper below .agentic/ with proper multiline syntax; "
                                "do not modify the product for this error"
                            )
                        else:
                            validation_suggestions.append(
                                "tool-plane failure: use the correctly shaped executable validation tool; "
                                "do not modify the product for this error"
                            )
                        print(
                            f"⚠️ [tool-plane recovery] {tool_name} failed before product validation; "
                            "reopening validation tools"
                        )
                    else:
                        turn_validation_failed = True
                        validation_suggestions.append(assessment[2])
                elif phase_validation:
                    if validation_contract.is_probe_quality_failure(assessment[1]):
                        turn_probe_quality_failure = True
                        validation_suggestions.append(assessment[2])
                        print(
                            f"⚠️ [probe-quality recovery] {tool_name}: "
                            f"{assessment[1]} — improve the check; do not patch product code"
                        )
                    else:
                        turn_validation_failed = True
                        validation_suggestions.append(assessment[1])
                        print(
                            f"⚠️ [validation evidence rejected] {tool_name}: "
                            f"{assessment[1]} — {assessment[2]}"
                        )
        if turn_probe_quality_failure and not turn_validation_failed:
            validation_required = True
            repair_required = False
            validation_failures = 0
            repair_turns_used = 0
            repair_recovery_mode = False
            repair_inspection_used = False
            probe_quality_recovery_pending = _has_test_artifacts(workspace_listing)
            messages.append({"role": "system", "content": (
                "Probe-quality recovery: the application check executed, but it did not explicitly "
                "assert every required response shape or interface. This is not a product defect. "
                "Use the trusted test runner on the next turn, rerun it, and do not patch the product."
            )})
            print("⚠️ [probe-quality recovery] reopening behavioral validation without product repair")
        if turn_tool_plane_failure and not turn_validation_failed:
            if repair_required:
                # A rejected validation call is control-plane evidence, not a
                # product failure. Reopen the validation surface even if the
                # actor was nominally in repair: the failed call may have been
                # the only attempt to start a required service or invoke a
                # helper. Keeping mutation-only repair tools here creates a
                # generic failure loop in which a healthy product is edited
                # because its checker was never actually executed.
                if tool_plane_recovery_attempts == 0:
                    validation_required = True
                    repair_required = False
                    validation_failures = 0
                    repair_turns_used = 0
                    repair_recovery_mode = False
                    repair_inspection_used = False
                    tool_plane_recovery_attempts += 1
                    lifecycle.transition("tool_plane_recovery")
                    messages.append({"role": "system", "content": (
                        "Tool-plane recovery: the previous validation call was rejected before execution. "
                        "This is not evidence that the product is defective. Reopen the validation target "
                        "or service if needed, then use run_command/run_shell with the declared argument "
                        "schema to execute the focused behavioral check. Product files remain frozen until "
                        "a real check reports a product failure."
                    )})
                    print("⚠️ [tool-plane recovery] reopening validation after a blocked repair-phase call")
                else:
                    # One grace recovery is enough. A second rejected probe
                    # means the actor is trying to evade the repair plane; keep
                    # the FSM in repair and force inspection/mutation instead
                    # of reopening the same failing validation loop.
                    messages.append({"role": "system", "content": (
                        "Repeated tool-plane failure: validation recovery was already attempted. "
                        "Do not call run_command, run_shell, or process_status again. Inspect the "
                        "implementation named by the latest failure and make one targeted product "
                        "repair with read_file followed by patch_file or write_file."
                    )})
                    print("⚠️ [tool-plane recovery] grace exhausted; preserving repair state")
            else:
                # A dispatch/schema/allow-list failure during validation is
                # not evidence that the implementation is wrong. Reopen the
                # validation surface, but never force a product patch.
                validation_required = True
                repair_required = False
                validation_failures = 0
                repair_turns_used = 0
                repair_recovery_mode = False
                repair_inspection_used = False
                tool_plane_recovery_attempts = 0
                lifecycle.transition("tool_plane_recovery")
                messages.append({"role": "system", "content": (
                    "Tool-plane recovery: the previous validation call was rejected before execution "
                    "because its tool name or argument shape was invalid. This is not a product defect. "
                    "Use run_command/run_shell with the declared schema (command may be an argv list or "
                    "a shell string), execute a real behavioral check, and do not patch the product."
                )})
        if repair_turn_before_dispatch:
            repair_turns_used += 1
            print(f"🧭 [repair turn] {repair_turns_used}/{REPAIR_TURN_BUDGET}")
        if turn_mutated:
            orientation_recovery_read_used = False
            last_repair_inspection_checkpoint = []
            successful_mutation_messages = []
            if tool_start_idx > 0:
                successful_mutation_messages.append(messages[tool_start_idx - 1])
            for call, tmsg in zip(turn_calls, tool_messages):
                capability = action_governor.classify(call.function.name, call.function.arguments or {})
                result = tmsg.get("content", "")
                if capability == "MUTATE" and not result.startswith(("ERROR:", "REJECTED:")):
                    successful_mutation_messages.append(tmsg)
                    if transaction is not None:
                        transaction.record_mutation(
                            (call.function.arguments or {}).get("path", ""),
                            checkpoint_id=iteration,
                        )
            last_mutation_checkpoint = successful_mutation_messages or last_mutation_checkpoint
            mutation_was_in_validation_batch = validation_phase_before_turn and not repair_turn_before_dispatch
            lifecycle.transition("mutation")
            if repair_required:
                repair_mutations += 1
                repair_mutation_pending = True
                repair_required = False
                repair_inspection_used = False
                validation_failures = 0
                repair_turns_used = 0
                repair_recovery_mode = False
                tool_plane_recovery_attempts = 0
                print("🛠️  [repair phase] targeted mutation landed; returning to validation")
            validation_required = True
            validation_failures = 0
            if mutation_was_in_validation_batch:
                validation_batch_remaining = max(0, validation_batch_remaining - 1)
            else:
                validation_batch_remaining = MUTATION_BATCH_LIMIT
            print("🔒 [validation phase] mutation succeeded; validation required before another edit")
            stale_service_handles = active_background_handles()
            if stale_service_handles:
                restart_results = [restart_background(handle) for handle in stale_service_handles]
                for result in restart_results:
                    print(f"♻️ [stale service] automatic refresh: {result}")
                stale_service_restart_pending = any(result.startswith("ERROR:") for result in restart_results)
                if not stale_service_restart_pending:
                    stale_service_restart_note = (
                        "The engine refreshed the managed service after the mutation; validation is still required."
                    )
                process_status_used = False
                print(f"♻️ [stale service] refresh complete; validation required: {stale_service_handles}")
        if not validation_required and not repair_required:
            if turn_mutated:
                orientation_turns_without_mutation = 0
            else:
                orientation_turns_without_mutation += 1
                if orientation_recovery_active and any(
                    lifecycle_policy.counts_as_repair_inspection(call.function.name)
                    for call in turn_calls
                ):
                    orientation_recovery_read_used = True
        if turn_validation_failed:
            # A failed executable check on the first turn is already useful
            # diagnostic evidence. Promote it into the normal validation /
            # repair FSM instead of leaving the actor in unrestricted ACT,
            # where it can discard the failure and start broad orientation.
            # This is provider- and task-agnostic; setup failures still flow
            # through the deterministic setup-plane policy below.
            if not validation_required:
                validation_required = True
            validation_batch_remaining = 0
            lifecycle.transition("validation_failed")
            if not repair_required:
                repair_mode_entries += 1
            validation_failures_total += 1
            validation_failures += 1
            repair_required = True
            repair_inspection_used = False
            failed_packets = []
            for call, tmsg in zip(turn_calls, tool_messages):
                name = call.function.name
                args = call.function.arguments or {}
                if name in {"run_tests", "run_command", "run_shell", "process_status", "diff_files", "git_diff"}:
                    result_content = tmsg.get("content", "")
                    source_context = validation_contract.source_context_from_failure(
                        result_content, get_root()
                    )
                    packet = validation_plan.synthesize_failure_feedback(
                        name, args, result_content,
                        source_context=source_context,
                    )
                    provenance = validation_contract.build_failure_provenance(
                        name,
                        args,
                        result_content,
                        get_root(),
                        changed_paths=(
                            tuple(sorted(transaction.files))
                            if transaction is not None else tuple(sorted(pending_product_paths))
                        ),
                    )
                    packet = packet + "\n" + provenance.render()
                    test_context = validation_contract.failed_test_context(
                        result_content, get_root()
                    )
                    if test_context:
                        packet += "\nTest-only context (do not edit):\n" + test_context
                    failed_packets.append(packet)
                    if name == "run_tests":
                        last_failed_test_request = {
                            "tool_name": name,
                            "arguments": dict(args),
                        }
                    # A trustworthy traceback excerpt already supplies the
                    # local evidence needed for a targeted edit. Remove
                    # redundant read tools for this repair turn only; if no
                    # safe excerpt exists, keep the normal inspect-then-patch
                    # path for complex or indirect failures.
                    if source_context and not _is_validation_setup_failure(result_content):
                        repair_inspection_used = True
            # A repair turn may only inspect the evidence. Do not erase the
            # previous failure packet in that case; recovery decisions still
            # need to know whether the active problem is setup or behavior.
            if failed_packets:
                last_repair_packet = "\n\n".join(failed_packets)[-3000:]
            last_validation_failure = "; ".join(dict.fromkeys(s for s in validation_suggestions if s))[-1200:]
            messages.append({"role": "system", "content": (
                last_repair_packet or ("Validation feedback: " + "; ".join(dict.fromkeys(s for s in validation_suggestions if s)))
            )})
            if transaction is not None:
                transaction_decision = transaction.note_validation_failed(last_repair_packet)
                transaction_status = transaction.control_block()
                if transaction_status:
                    messages.append({"role": "system", "content": transaction_status})
                    print(
                        f"🔁 [transaction buffer] action={transaction_decision.action}; "
                        f"files={list(transaction.files)}; "
                        f"turns_remaining={transaction_decision.turns_remaining}"
                    )
                if transaction_decision.action == "recover":
                    # Expiration is a deterministic lifecycle event, not an
                    # instruction to erase product work. RiskLayer retains
                    # the available checkpoint for an explicit recovery path.
                    repair_recovery_mode = True
                    repair_recovery_entries += 1
                    if lifecycle.state == LifecycleState.REPAIR:
                        lifecycle.transition("recovery_budget_exhausted")
                    messages.append({"role": "system", "content": (
                        "Transaction window expired. Keep the accepted files available for recovery; "
                        "do not perform a destructive reset automatically. Use the latest failure and "
                        "the existing checkpoint to make one deliberate recovery decision."
                    )})
            if (novelty_context is not None and last_repair_packet
                    and _worker_triage_enabled(novelty_action_critic, novelty_action_gate)):
                gate_judgment = novelty_context.synchronous_triage(
                    iteration,
                    lifecycle.state.value,
                    last_repair_packet,
                    legal_actions=("patch_file", "write_file", "run_tests", "run_command", "run_shell", "finish_task"),
                    protected_paths=tuple(sorted(blocked_mutation_paths)),
                )
                print(
                    f"🚦 [4B triage gate] class={gate_judgment.failure_class} "
                    f"next={gate_judgment.next_action or gate_judgment.recommended_action} "
                    f"confidence={gate_judgment.confidence:.2f}"
                )
            print(f"⚠️  [validation phase] validation failed ({validation_failures}); targeted repair required before recheck")
        if (blocked_repair_action and repair_turn_before_dispatch and not turn_mutated
                and not setup_failure and not repair_recovery_mode):
            # A rejected inspection is a control-plane convergence failure,
            # not a reason to spend the whole ordinary repair budget replaying
            # the same action.  Move directly to the bounded mutation
            # checkpoint on the next turn.
            lifecycle.transition("recovery_budget_exhausted")
            repair_recovery_mode = True
            # The earlier tool-plane branch may have reopened VALIDATE and
            # cleared the ordinary repair flag. Preserve the pre-dispatch
            # repair intent so the next loop actually builds the checkpoint
            # instead of returning to the rejected validation surface.
            validation_required = True
            repair_required = True
            repair_recovery_entries += 1
            repair_turns_used = max(repair_turns_used, REPAIR_TURN_BUDGET)
            print(
                "🧭 [repair recovery] blocked inspection detected; "
                "fresh targeted-mutation checkpoint required next turn"
            )
        if (repair_required and not turn_mutated and repair_turns_used >= REPAIR_TURN_BUDGET
                and not repair_recovery_mode):
            lifecycle.transition("recovery_budget_exhausted")
            repair_recovery_mode = True
            repair_recovery_entries += 1
            print(
                f"🧭 [repair recovery] budget exhausted after {repair_turns_used} turns; "
                "compacting checkpoint and forcing a targeted mutation"
            )
        if turn_validation_succeeded:
            # Do not carry a stale test invocation into a later, unrelated
            # lifecycle. A fresh failure will install a fresh request.
            last_failed_test_request = None
            if repair_mutation_pending:
                successful_repair_cycles += 1
                repair_mutation_pending = False
            uncovered = validation_plan.uncovered_endpoints(validation_criteria_hits)
            if uncovered:
                lifecycle.transition("validation_partial")
                # One passing probe is useful evidence, but it is not enough
                # when the task names several interfaces. Keep the actor in a
                # focused validation phase until every interface is covered.
                validation_required = True
                validation_failures = 0
                validation_batch_remaining = 0
                print("✅ [validation phase] probe succeeded; still required: " + ", ".join(uncovered))
            else:
                lifecycle.transition("validation_passed")
                validation_required = False
                validation_failures = 0
                validation_batch_remaining = 0
                if transaction is not None and transaction.note_validation_passed():
                    print("✅ [transaction buffer] authoritative validation passed; cleared transaction state")
                # Independent validation is the authoritative completion
                # signal. Waiting for one more model turn to call
                # finish_task wastes tokens and can strand a correct artifact
                # behind a slow actor/provider. The model's summary is useful,
                # but it is not stronger evidence than the accepted check.
                TASK_STATE["summary"] = (
                    "Completed after independent validation; all required acceptance evidence passed."
                )
                approve_task()
                print("✅ [orchestrator completion] independent validation succeeded; task is complete")

        if novelty_context is not None:
            for call, tmsg in zip(turn_calls, tool_messages):
                args = call.function.arguments or {}
                tool_name = call.function.name
                capability = action_governor.classify(tool_name, args)
                phase_validation = validation_phase_before_turn and tool_name in {
                    "run_tests", "run_command", "run_shell", "process_status", "diff_files", "git_diff"
                }
                novelty_context.observe(
                    iteration, tool_name, args, tmsg["content"],
                    mutation=(
                        capability == "MUTATE"
                        and not lifecycle_policy.is_validation_helper_path(args.get("path", ""))
                        and not tmsg["content"].startswith(("ERROR:", "REJECTED:"))
                    ),
                    validation=capability == "VALIDATE" or phase_validation,
                )
            print(f"🧬 [novelty context] {novelty_context.render_for_model()}")

        if structured_summary_enabled:
            # Unlike context_summary_enabled, state.update() already extracts
            # everything durable from the raw content (files_explored,
            # facts_accumulated via fact_extraction) BEFORE this point, so
            # pruning the raw text afterward loses nothing state.render()
            # depends on — only recall(N) needs it, for exact original text.
            for i, (call, tmsg) in enumerate(zip(turn_calls, tool_messages)):
                state.update(call.function.name, call.function.arguments, tmsg["content"])
                entry_number = len(entry_positions) + 1
                memory.record(entry_number, tmsg["content"])
                entry_positions[entry_number] = tool_start_idx + i
                ledger.record(iteration, call.function.name, call.function.arguments, tmsg["content"])
            print(f"🧱 [state updated] {state.render()}")
            recent = ledger.history[-len(turn_calls):]
            gov_desc = ", ".join(
                f"{e['tool_name']}[{e['capability']}]{'(dup)' if e['is_duplicate'] else ''}" for e in recent
            )
            print(f"📊 [governor] {gov_desc} — recent_progress={ledger.recent_progress()}")

            if ENABLE_PRUNING:
                # Char-budget eligibility (see KEEP_RECENT_RAW_CHARS above):
                # walk newest -> oldest accumulating size; once the running
                # total exceeds budget, that entry and everything older is
                # prunable, regardless of call count.
                ordered = sorted(entry_positions)
                running = 0
                candidates = []
                for n in reversed(ordered):
                    content = messages[entry_positions[n]]["content"]
                    if content.startswith("[pruned"):
                        continue
                    running += len(content)
                    if running > KEEP_RECENT_RAW_CHARS:
                        candidates.append(n)
                candidates.reverse()  # oldest-first, matches pruning order below

                # Real problem caught live: pruning one entry at a time,
                # every iteration once past the threshold, mutates an old
                # `messages` entry almost every single call — each mutation
                # invalidates Ollama's prefix cache for everything
                # downstream (documented, accepted tradeoff below), so
                # pruning THIS often quietly reintroduces the exact
                # per-call cache-invalidation cost the trailing-injection
                # pattern (see the top of this loop) was built to eliminate.
                # Batching means paying that cost once every
                # PRUNE_BATCH_SIZE tool results instead of every single one.
                newly_pruned = []
                if len(candidates) >= PRUNE_BATCH_SIZE:
                    for entry_number in candidates:
                        idx = entry_positions[entry_number]
                        content = messages[idx]["content"]
                        # Semantic pointer: tool + scope (file/command/
                        # pattern) instead of a bare entry number, so the
                        # model can judge whether recall is worth it without
                        # having to remember what #N contained. ledger.history
                        # grows in exact lockstep with entry_positions (both
                        # populated once per tool call, same loop, same
                        # order), so entry_number - 1 is always the right index.
                        source = ledger.history[entry_number - 1] if entry_number - 1 < len(ledger.history) else None
                        if source and source.get("scope"):
                            source_desc = f"{source['tool_name']}({source['scope']})"
                        elif source:
                            source_desc = source["tool_name"]
                        else:
                            source_desc = "unknown"
                        messages[idx]["content"] = (
                            f"[pruned — entry #{entry_number}: {source_desc}, {len(content)} chars raw output. "
                            f"Call recall({entry_number}) for the exact original text if needed.]"
                        )
                        newly_pruned.append((entry_number, len(content)))
                if newly_pruned:
                    pruned_desc = ", ".join(f"#{n} ({c} chars)" for n, c in newly_pruned)
                    print(f"🗑️  [pruned batch of {len(newly_pruned)}] {pruned_desc} — still recallable via recall(N)")
        elif working_state_enabled:
            # Deterministic fields first (never trust a model's self-report
            # of what happened — see working_state.py's module docstring),
            # archived + fingerprinted into the SAME ledger the governor
            # above already reads, then a checkpoint call if this turn
            # crosses one of should_checkpoint's real triggers.
            turn_mutated = False
            turn_validated = False
            for i, (call, tmsg) in enumerate(zip(turn_calls, tool_messages)):
                entry_number = len(entry_positions) + 1
                memory.record(entry_number, tmsg["content"])
                entry_positions[entry_number] = tool_start_idx + i
                ledger.record(iteration, call.function.name, call.function.arguments, tmsg["content"])
                working_state.update_deterministic(
                    ws, call.function.name, call.function.arguments, tmsg["content"],
                    entry_number, io_tools._resolve, ledger,
                )
                capability = action_governor.classify(call.function.name, call.function.arguments)
                turn_mutated = turn_mutated or capability == "MUTATE"
                turn_validated = turn_validated or capability == "VALIDATE"

            recent = ledger.history[-len(turn_calls):]
            gov_desc = ", ".join(
                f"{e['tool_name']}[{e['capability']}]{'(dup)' if e['is_duplicate'] else ''}" for e in recent
            )
            print(f"📊 [governor] {gov_desc} — recent_progress={ledger.recent_progress()}")

            if working_state.should_checkpoint(ws, mutated_workspace=turn_mutated, validation_completed=turn_validated):
                working_state.checkpoint(ws, ledger, memory.ARCHIVE, since_index=ws_since_index)
                ws_since_index = len(ledger.history)
                print(f"🧱 [checkpoint rev {ws.revision}] facts={len(ws.facts)} decisions="
                      f"{sum(1 for d in ws.decisions if not d.superseded)} changes={len(ws.changes)} "
                      f"validations={len(ws.validations)} phase={ws.phase}")

            if ENABLE_PRUNING:
                # Same char-budget + semantic-pointer eviction as
                # structured_summary_enabled above — this is a brand new
                # mode with no prior baseline to hold stable for comparison,
                # so it starts from the improved pruning rather than
                # deliberately reintroducing the known-inferior count-based
                # version.
                ordered = sorted(entry_positions)
                running = 0
                candidates = []
                for n in reversed(ordered):
                    content = messages[entry_positions[n]]["content"]
                    if content.startswith("[pruned"):
                        continue
                    running += len(content)
                    if running > KEEP_RECENT_RAW_CHARS:
                        candidates.append(n)
                candidates.reverse()

                newly_pruned = []
                if len(candidates) >= PRUNE_BATCH_SIZE:
                    for entry_number in candidates:
                        idx = entry_positions[entry_number]
                        content = messages[idx]["content"]
                        source = ledger.history[entry_number - 1] if entry_number - 1 < len(ledger.history) else None
                        if source and source.get("scope"):
                            source_desc = f"{source['tool_name']}({source['scope']})"
                        elif source:
                            source_desc = source["tool_name"]
                        else:
                            source_desc = "unknown"
                        messages[idx]["content"] = (
                            f"[pruned — entry #{entry_number}: {source_desc}, {len(content)} chars raw output. "
                            f"Call recall({entry_number}) for the exact original text if needed.]"
                        )
                        newly_pruned.append((entry_number, len(content)))
                if newly_pruned:
                    pruned_desc = ", ".join(f"#{n} ({c} chars)" for n, c in newly_pruned)
                    print(f"🗑️  [pruned batch of {len(newly_pruned)}] {pruned_desc} — still recallable via recall(N)")
        elif context_summary_enabled:
            # Whole-context re-synthesis: ONE holistic summary, REPLACED
            # each call rather than appended to a growing list — lets the
            # worker deduplicate/reorganize across calls (e.g. collapse
            # several "explored crv_types.py" notes into one) instead of
            # just accumulating independent per-call entries the way the
            # sidecar_enabled branch below does. See worker.summarize_context.
            # Injected as a trailing message for the NEXT call (top of the
            # loop), not written into `messages` itself — see the comment
            # there for why.
            for i, (call, tmsg) in enumerate(zip(turn_calls, tool_messages)):
                context_summary = worker.summarize_context(
                    context_summary, call.function.name, call.function.arguments, tmsg["content"]
                )
                # Archive the RAW content (not the summary) under a fresh
                # entry number, and remember where its message lives in
                # `messages` so it can be pruned later while staying
                # recoverable via kernel.memory.recall.
                entry_number = len(entry_positions) + 1
                memory.record(entry_number, tmsg["content"])
                entry_positions[entry_number] = tool_start_idx + i
            print(f"🗒️  [summary updated, entries 1-{len(entry_positions)}] {context_summary}")

            # Prune raw tool-result content older than the recent window —
            # the exact fix for hitting num_ctx's ceiling live (97.5% full
            # by iteration 39, zero writes, in the un-pruned version). This
            # is the one deliberate exception to keeping `messages` fully
            # stable: it costs one cache-invalidating call right after a
            # prune (content before the tail changed), then `messages` is
            # stable again until the next prune — far cheaper than letting
            # raw results grow unbounded.
            prunable = sorted(entry_positions)[:-KEEP_RECENT_RAW_RESULTS] if len(entry_positions) > KEEP_RECENT_RAW_RESULTS else []
            newly_pruned = []
            for entry_number in prunable:
                idx = entry_positions[entry_number]
                content = messages[idx]["content"]
                if not content.startswith("[pruned"):
                    messages[idx]["content"] = (
                        f"[pruned — entry #{entry_number}'s raw output ({len(content)} chars). "
                        f"Call recall({entry_number}) for the exact original text if needed.]"
                    )
                    newly_pruned.append((entry_number, len(content)))
            if newly_pruned:
                pruned_desc = ", ".join(f"#{n} ({c} chars)" for n, c in newly_pruned)
                print(f"🗑️  [pruned] {pruned_desc} — still recallable via recall(N)")
        elif sidecar_enabled:
            for call, tmsg in zip(turn_calls, tool_messages):
                if worker_enabled:
                    # Orchestrator-worker pattern: qwen3.5:9b compresses the
                    # raw result into a real semantic summary instead of
                    # sidecar.py's mechanical name+args+length line. See
                    # worker.py — falls back to the mechanical summary on
                    # any worker failure, so this never blocks the
                    # orchestrator's own progress.
                    entry = worker.compress_tool_result(
                        call.function.name, call.function.arguments, tmsg["content"]
                    )
                else:
                    entry = sidecar.summarize_call(call.function.name, call.function.arguments, tmsg["content"])
                print(f"🗒️  [{len(sidecar_log) + 1}] {entry}")
                sidecar_log.append(entry)
            # Rebuilt fresh every iteration from the immutable base prompt,
            # not appended to in place — keeps this idempotent regardless of
            # how many times the loop runs, and keeps the sidecar pinned at
            # the very top of the prompt (messages[0]) every single call.
            messages[0]["content"] = system_prompt + sidecar.render_sidecar(sidecar_log)

        if TASK_STATE["requested"] and not TASK_STATE["done"]:
            ready, reason = _completion_ready(messages, task_type, validation_plan, validation_evidence, validation_criteria_hits)
            if ready:
                approve_task()
                print(f"✅ Completion evidence verified: {TASK_STATE['summary']}")
            else:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Completion was requested but verification is incomplete: {reason}. "
                        "Continue working, then run an appropriate passing test or command before "
                        "calling finish_task again."
                    ),
                })
                TASK_STATE["requested"] = False

        if status_path:
            for tmsg in tool_messages:
                content = tmsg.get("content", "")
                if content.startswith("ERROR") or content.startswith("REJECTED"):
                    recent_errors.append(f"iter {iteration}: {content[:200]}")
            recent_errors[:] = recent_errors[-5:]
            last_entry = ledger.history[-1] if _governed and ledger.history else None
            status_report.write(
                status_path,
                iteration=iteration,
                iteration_budget=iteration_budget,
                last_action_classification=last_entry["capability"] if last_entry else None,
                escalation_level=current_level,
                ledger_size=len(ledger.history) if _governed else None,
                distributions_done=status_report.classes_with_method(
                    io_tools._resolve(distribution_target_file), distribution_names
                ) if distribution_target_file and distribution_names else None,
                recent_errors=list(recent_errors),
                task_done=TASK_STATE["done"],
                task_summary=TASK_STATE["summary"],
                novelty_context=novelty_context.metrics() if novelty_context else None,
            )

        if TASK_STATE["done"]:
            print(f"\n✅ DONE: {TASK_STATE['summary']}")
            close_novelty_context()
            return True

    print("\n" + "=" * 60)
    print(f"❌ INCOMPLETE: finish_task was not called within {iteration_budget} iterations.")
    print("=" * 60)
    close_novelty_context()
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the coding agent against a real task.")
    parser.add_argument("task", nargs="+", help="The task to work on.")
    parser.add_argument(
        "--project", default=None,
        help="Directory to confine the agent to. Defaults to evolutionEngine/workspace.",
    )
    parser.add_argument(
        "--model", default=MODEL,
        help=f"Ollama model tag for the orchestrator's own calls (default {MODEL!r}). "
             "Per-run override only — does not change the module default.",
    )
    parser.add_argument(
        "--backend", choices=["ollama", "llama-cpp"], default="ollama",
        help="Actor serving backend (default: ollama).",
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8080/v1",
        help="OpenAI-compatible base URL for --backend llama-cpp.",
    )
    parser.add_argument(
        "--action-first", action="store_true",
        help="Use a model-neutral initial action contract with a small executable tool surface.",
    )
    parser.add_argument(
        "--network", action="store_true",
        help="Expose web_search and fetch. Off by default to reduce tool-routing cost for coding tasks.",
    )
    parser.add_argument(
        "--sidecar", action="store_true",
        help="Enable the automated activity-log sidecar (see sidecar.py) pinned to the system prompt.",
    )
    parser.add_argument(
        "--worker", action="store_true",
        help="Requires --sidecar. Use qwen3.5:9b (see worker.py) to compress each tool result into a "
             "real semantic summary for the sidecar, instead of the mechanical name+args+length line.",
    )
    parser.add_argument(
        "--context-summary", action="store_true",
        help="Alternative to --sidecar/--worker's append-only list: qwen3.5:9b maintains ONE holistic "
             "summary, re-synthesized (not appended to) after every tool call. See worker.summarize_context.",
    )
    parser.add_argument(
        "--structured-summary", action="store_true",
        help="Alternative to --context-summary: files/facts are tracked entirely by code (zero LLM "
             "involvement, zero hallucination risk); qwen3.5:9b only judges a short one-line status. "
        "See structured_state.py.",
    )
    parser.add_argument(
        "--novelty-context", action="store_true",
        help="Enable the asynchronous qwen3.5:4b context worker and novelty metrics.",
    )
    parser.add_argument(
        "--novelty-worker-model", default="qwen3.5:4b",
        help="Ollama model used by --novelty-context (default: qwen3.5:4b).",
    )
    parser.add_argument(
        "--novelty-action-gate", action="store_true",
        help="Opt in to restricting observation tools after a stagnant novelty window; off by default.",
    )
    parser.add_argument(
        "--novelty-action-critic", action="store_true",
        help="Ask the 4B worker for one concrete next-action recommendation on stalled/error turns; advisory only.",
    )
    parser.add_argument(
        "--thinking", action="store_true",
        help="Enable the orchestrator model's extended-thinking/reasoning mode (ollama.chat(..., think=True)) "
             "for MODEL's own calls only. Previously hardcoded to think=False everywhere in this file — no "
             "prior flag for this existed. Sidecar/worker/status/checkpoint calls (worker.py, "
             "structured_state.py, message_compaction.py, working_state.py) always pass think=False "
             "regardless of this flag; they're one-line judgment calls on a smaller model, not reasoning tasks.",
    )
    parser.add_argument(
        "--iteration-budget", type=int, default=ITERATION_BUDGET,
        help=f"Max iterations before giving up (default {ITERATION_BUDGET}). A real multi-file SWE-bench "
             f"task needs far more than the historical default — the naive-baseline reference run on "
             f"sympy-13878 reached iteration 102+ before being stopped by hand.",
    )
    parser.add_argument(
        "--status-file", default=None,
        help="Path to write a per-iteration JSON snapshot to (overwritten each iteration; a sibling "
             ".jsonl with the same basename is appended to instead, for `tail -f`). See status_report.py. "
             "For external monitoring from another terminal — not consumed by the model itself.",
    )
    parser.add_argument(
        "--chat-timeout", type=float, default=CHAT_TIMEOUT_SECONDS,
        help=f"Maximum seconds for one acting-model turn before cleanly stopping (default {CHAT_TIMEOUT_SECONDS}; 0 disables).",
    )
    parser.add_argument(
        "--distribution-target-file", default=None,
        help="Relative path (within --project) of a file to ast-scan each iteration for which of "
             "--distribution-names' classes define their own _cdf method — only meaningful together "
             "with --status-file. Task-agnostic mechanism, sympy-13878-shaped example usage.",
    )
    parser.add_argument(
        "--distribution-names", default=None,
        help="Comma-separated class names to check via --distribution-target-file, e.g. "
             "'Arcsin,Dagum,Gamma'.",
    )
    args = parser.parse_args()

    modes_enabled = sum([args.sidecar, args.context_summary, args.structured_summary])
    if args.worker and not args.sidecar:
        raise SystemExit("❌ --worker requires --sidecar (there's nowhere to put the compressed summary otherwise).")
    if modes_enabled > 1:
        raise SystemExit("❌ --sidecar/--context-summary/--structured-summary are mutually exclusive — pick one mode.")

    if args.project:
        try:
            set_root(args.project)
        except NotADirectoryError as e:
            raise SystemExit(f"❌ {e}")

    if args.model != MODEL:
        MODEL = args.model
        print(f"🔀 Model override: {MODEL}")

    # A real project's files aren't a throwaway sandbox — don't auto-execute
    # whatever the model just wrote. See kernel/io_tools.py's AUTO_RUN_AFTER_WRITE.
    io_tools.AUTO_RUN_AFTER_WRITE["enabled"] = False

    task = " ".join(args.task)
    tools = load_registry(include_network=args.network) + [finish_task]

    print(f"📁 Operating in: {get_root()}")
    print(f"🧰 Loaded {len(tools)} tool(s): {[fn.__name__ for fn in tools]}")
    if args.structured_summary:
        print("🧱 Structured state mode: ENABLED (files/facts code-tracked, qwen3.5:9b judges Status only)")
    elif args.context_summary:
        print("🗒️  Whole-context summary mode: ENABLED (qwen3.5:9b re-synthesizes one holistic summary)")
    elif args.sidecar:
        print(f"🗒️  Automated activity-log sidecar: ENABLED{' + qwen3.5:9b worker compression' if args.worker else ' (mechanical)'}")
    if args.novelty_context:
        print(f"🧬 Novelty context: ENABLED ({args.novelty_worker_model}, asynchronous 4B worker)")
    if args.thinking:
        print("🧠 Orchestrator thinking mode: ENABLED (think=True on MODEL's own calls)")

    if args.status_file:
        print(f"📟 Status file: ENABLED ({args.status_file}, plus {args.status_file.rsplit('.', 1)[0]}.jsonl)")

    run_agent(task, tools, iteration_budget=args.iteration_budget, sidecar_enabled=args.sidecar,
              worker_enabled=args.worker, context_summary_enabled=args.context_summary,
              structured_summary_enabled=args.structured_summary, think=args.thinking,
              status_path=args.status_file, distribution_target_file=args.distribution_target_file,
              distribution_names=args.distribution_names.split(",") if args.distribution_names else None,
              novelty_context_enabled=args.novelty_context, novelty_worker_model=args.novelty_worker_model,
              novelty_action_gate=args.novelty_action_gate, novelty_action_critic=args.novelty_action_critic,
              chat_timeout=args.chat_timeout, backend=args.backend, base_url=args.base_url,
              action_first=args.action_first)
