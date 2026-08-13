"""The consumer, not the bootstrapper: takes a real task instead of a
curriculum goal, and works it with the full tool registry. Not a curriculum
goal itself — same tier as harness.py, hand-written orchestration code.

Defaults to evolutionEngine/workspace like harness.py, but --project can
point the whole toolbelt (kernel tools + every confined graduated tool) at
any real directory — see kernel/sandbox.py for how that confinement works.
"""

import argparse
import json
import time

from ollama import chat

import action_governor
import adaptive_budget
import escalation_governor
import kernel.io_tools as io_tools
import kernel.memory as memory
import message_compaction
import progress_governor
import risk_layer
import sidecar
import status_report
import structured_state
import task_contract
import worker
import working_state
from dispatch import dispatch_tool_calls
from kernel.control import TASK_STATE, approve_task, finish_task
from kernel.sandbox import get_root, set_root
from novelty_context import NoveltyContext
from registry import load_registry

MODEL = "qwen3.6:35b-mlx"
ITERATION_BUDGET = 20

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


def _completion_ready(messages, task_type):
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
        if name in {"write_file", "patch_file"} and not content.startswith(("ERROR:", "REJECTED")):
            mutated = True
        if name == "run_tests" and content.startswith("(True,"):
            validated = True
        if name in {"run_shell", "run_command"} and "Exit code: 0" in content:
            validated = True
    if not mutated:
        return False, "no successful write_file or patch_file call has been observed"
    if not validated:
        return False, "no passing run_tests, run_command, or run_shell result has been observed"
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
              distribution_names=None, novelty_context_enabled=False, novelty_worker_model="qwen3.5:4b"):
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
    _governed = structured_summary_enabled or working_state_enabled
    ledger = action_governor.EvidenceLedger() if _governed else None
    contract = task_contract.CONTRACTS_BY_TYPE.get(task_type, task_contract.CODE_CHANGE_CONTRACT)
    escalation = escalation_governor.EscalationState() if _governed else None
    risk = risk_layer.RiskLayer() if _governed else None
    # adaptive_budget.py's repo_size_hint — computed ONCE here (not derived
    # from the ledger, which would be circular, see adaptive_budget.py's
    # own docstring) from a cheap, non-recursive top-level listing. Real
    # file COUNT of the project root, not a deep walk — a full recursive
    # count of a large real checkout (sympy has thousands of files) would
    # be slow and mostly noise for this purpose; top-level breadth is a
    # good-enough, cheap proxy.
    repo_size_hint = len([l for l in io_tools.list_workspace().splitlines() if l.strip()])
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

    def close_novelty_context():
        if novelty_context is not None:
            novelty_context.close()
            print(f"🧬 [novelty metrics] {json.dumps(novelty_context.metrics(), sort_keys=True)}")

    offered_tool_names = ", ".join(fn.__name__ for fn in tools)
    system_prompt = f"""You are a Principal Software Engineer running locally via hardware acceleration.
You are working inside this directory: {get_root()}
Every tool you have is confined to this directory and its subdirectories — you cannot read or write
anything outside it, and attempts to do so will be rejected.

Every path you pass to a tool is ALREADY relative to that directory. Pass just "src/app.py", never prefix
it with the directory's own name — doing so creates an unwanted nested directory instead of reaching the
real file.

You have this focused toolbelt: {offered_tool_names}.

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
- When — and only when — the task is fully complete, call finish_task with a short summary of what you did.
  Returning plain text without calling finish_task does not end the task; you are expected to keep working."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    for iteration in range(1, iteration_budget + 1):
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

        if novelty_context is not None:
            messages_for_call = messages_for_call + [{
                "role": "system", "content": novelty_context.render_for_model(),
            }]

        # The graduated progress governor — evaluated using the ledger's
        # state as of the END of the previous iteration (this iteration's
        # tool calls haven't happened yet). Replaces the old blunt
        # FORCE_EDIT_AFTER_ITERATIONS gate; see escalation_governor.py's
        # docstring for why graduated intervention beats a single hard cutoff.
        tools_for_call = tools
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

        if novelty_context is not None and novelty_context.requires_progress():
            progress_tools = {"patch_file", "write_file", "run_tests", "run_command", "run_shell",
                              "finish_task", "recall"}
            gated_names = {t.__name__ for t in tools_for_call} & progress_tools
            tools_for_call = [t for t in tools_for_call if t.__name__ in gated_names]
            messages_for_call = messages_for_call + [{
                "role": "system",
                "content": (
                    "[novelty context action gate] The recent context window contains no mutation or "
                    "validation. Observation tools are temporarily unavailable. Use the evidence already "
                    "gathered to patch, validate, finish, or recall exact prior text."
                ),
            }]

        response = None
        last_error = None
        for attempt in range(1, MAX_CHAT_RETRIES + 1):
            try:
                response = chat(model=MODEL, messages=messages_for_call, tools=tools_for_call, think=think,
                                 options={"num_ctx": NUM_CTX})
                break
            except Exception as e:
                last_error = e
                print(f"⚠️  chat() failed (attempt {attempt}/{MAX_CHAT_RETRIES}): {type(e).__name__}: {e}")
                recent_errors.append(f"iter {iteration}: chat() {type(e).__name__}: {e}")
                recent_errors[:] = recent_errors[-5:]
                if attempt < MAX_CHAT_RETRIES:
                    time.sleep(min(120, 2 ** attempt))
        if response is None:
            print(f"\n❌ chat() failed {MAX_CHAT_RETRIES} times in a row — ending run cleanly rather than "
                  f"crashing. Last error: {last_error}")
            close_novelty_context()
            return False

        prompt_eval_s = (response.prompt_eval_duration or 0) / 1e9
        eval_s = (response.eval_duration or 0) / 1e9
        print(f"📏 prompt_tokens={response.prompt_eval_count} ({prompt_eval_s:.1f}s) "
              f"output_tokens={response.eval_count} ({eval_s:.1f}s)")

        msg = response.message
        messages.append(msg)

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
            print("⚠️  Model returned no tool call — nudging it to act.")
            messages.append({
                "role": "user",
                "content": "You must call a tool to make progress, or call finish_task if you're actually done.",
            })
            continue

        tool_start_idx = len(messages)
        # risk_layer.py: checkpoint every file about to be MUTATEd, BEFORE
        # dispatch actually runs the call — so a bad patch_file/write_file
        # can be rolled back afterward if needed. Uses the same
        # classification action_governor.py's ledger will independently
        # apply to the same call right after dispatch.
        if _governed:
            for call in msg.tool_calls:
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
        tool_messages = dispatch_tool_calls(
            msg.tool_calls, tool_map, allowed_names=allowed_names, blocked_calls=blocked_calls
        )
        messages.extend(tool_messages)

        if novelty_context is not None:
            for call, tmsg in zip(msg.tool_calls, tool_messages):
                args = call.function.arguments or {}
                tool_name = call.function.name
                capability = action_governor.classify(tool_name, args)
                novelty_context.observe(
                    iteration, tool_name, args, tmsg["content"],
                    mutation=capability == "MUTATE", validation=capability == "VALIDATE",
                )
            print(f"🧬 [novelty context] {novelty_context.render_for_model()}")

        if structured_summary_enabled:
            # Unlike context_summary_enabled, state.update() already extracts
            # everything durable from the raw content (files_explored,
            # facts_accumulated via fact_extraction) BEFORE this point, so
            # pruning the raw text afterward loses nothing state.render()
            # depends on — only recall(N) needs it, for exact original text.
            for i, (call, tmsg) in enumerate(zip(msg.tool_calls, tool_messages)):
                state.update(call.function.name, call.function.arguments, tmsg["content"])
                entry_number = len(entry_positions) + 1
                memory.record(entry_number, tmsg["content"])
                entry_positions[entry_number] = tool_start_idx + i
                ledger.record(iteration, call.function.name, call.function.arguments, tmsg["content"])
            print(f"🧱 [state updated] {state.render()}")
            recent = ledger.history[-len(msg.tool_calls):]
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
            for i, (call, tmsg) in enumerate(zip(msg.tool_calls, tool_messages)):
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

            recent = ledger.history[-len(msg.tool_calls):]
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
            for i, (call, tmsg) in enumerate(zip(msg.tool_calls, tool_messages)):
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
            for call, tmsg in zip(msg.tool_calls, tool_messages):
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
            ready, reason = _completion_ready(messages, task_type)
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
              novelty_context_enabled=args.novelty_context, novelty_worker_model=args.novelty_worker_model)
