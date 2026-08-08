"""The consumer, not the bootstrapper: takes a real task instead of a
curriculum goal, and works it with the full tool registry. Not a curriculum
goal itself — same tier as harness.py, hand-written orchestration code.

Defaults to evolutionEngine/workspace like harness.py, but --project can
point the whole toolbelt (kernel tools + every confined graduated tool) at
any real directory — see kernel/sandbox.py for how that confinement works.
"""

import argparse
import os
import re
import time

from ollama import chat, show as ollama_show

import kernel.io_tools as io_tools
from dispatch import dispatch_tool_calls
from kernel.control import TASK_STATE, finish_task
from kernel.sandbox import get_root, set_root
from context.budget import estimate_tokens
from context.compiler import compile_context
from controller.completion import current_git_head, evaluate_completion_gate
from controller.hypotheses import make_hypothesis_tools
from controller.progress import detect_patch_reversion, stagnation_nudge
from controller.subgoals import (auto_close_open_subgoals, make_subgoal_tools, open_subgoals_with_progress,
                                  should_escalate_duplicate)
from memory.store import RunStore
from memory.tools import make_memory_tools
from registry import load_registry

MODEL = "qwen3.6:35b-mlx"
ITERATION_BUDGET = 20

# Five real SWE-bench runs on the same instance mapped out both edges of
# this: a soft prompt and a polite mechanical nudge never got the model to
# write any code at all (0/35 turns, every time) — but a maximally
# forceful prompt + an early fixed-turn watchdog (turn 20) DID get it to
# write something, and that something was wrong: a plausible-looking but
# incorrect patch, written before the model had actually converged on the
# real fix (independently verified: resolved=False). Forcing action too
# early is its own failure mode, not just failing to force it at all.
#
# The actual observed pattern wasn't "ran out of time" — across every run,
# the model reached the correct diagnosis once, then kept re-deriving and
# re-stating that SAME diagnosis several times over without ever acting on
# it. That's a repetition signal, not a duration signal, so it's what
# REPETITION_DIAGNOSIS_PATTERN detects directly: the same backtick-quoted
# symbol flagged with diagnostic language ("found it", "the bug is", etc.)
# on two separate turns means the model already has an answer and is just
# re-confirming it — that's the moment to force a write, not an arbitrary
# elapsed-turn count. WATCHDOG_TURNS_WITHOUT_WRITE is only the backstop for
# runs where repetition never triggers (e.g. still exploring new ground
# turn after turn) — pushed later than the old single-tier value (20) so it
# rarely fires first now that the repetition detector exists.
#
# Adapted from arxiv.org/html/2505.23480v1 ("Revisiting Overthinking...from
# the Perspective of Self-Doubt"): that paper fixes a different flavor of
# overthinking (redundant self-verification in math CoT after already being
# right) by forcing an explicit early validation/commitment step, which the
# authors argue gives the model "permission" to stop re-litigating. Applied
# here to the FIRST mention of a diagnosed symbol, not the second — a softer
# checkpoint than the repetition trigger below, with a real escape hatch
# (state what's actually missing) rather than a blind forced write. The
# repetition trigger still exists as the harder fallback for when this
# checkpoint's answer doesn't lead anywhere new.
DIAGNOSTIC_PHRASE_PATTERN = re.compile(
    r"found it|the bug is|the issue is|the problem is|root cause|now i understand|i see the issue",
    re.IGNORECASE,
)
BACKTICK_SYMBOL_PATTERN = re.compile(r"`(\w+)`")
WATCHDOG_TURNS_WITHOUT_WRITE = 28
SUBGOAL_AUTO_CLOSE_GRACE_TURNS = 3
MAX_CHAT_RETRIES = 20  # see the retry loop's own comment — a bad streak needs real room to clear


def _diagnosed_symbols(text: str) -> set:
    """Symbols the model named alongside diagnostic language this turn —
    only meaningful if the SAME symbol shows up this way on a later turn
    too (see REPETITION_DIAGNOSIS below)."""
    if not text or not DIAGNOSTIC_PHRASE_PATTERN.search(text):
        return set()
    return set(BACKTICK_SYMBOL_PATTERN.findall(text))


def _live_model_options(model: str) -> dict:
    """Read the model's ACTUAL configured parameters from Ollama at run
    start, rather than hardcoding a guess into this file — the plan's
    "test model settings independently" step needs run.json to reflect
    reality even if the Modelfile changes later. `ollama.show()`'s
    `.parameters` is plain text ("name<spaces>value" per line), not a
    dict, so it's parsed here. Notably: num_ctx does NOT appear here for
    qwen3.6:35b-mlx as of this writing — it isn't set in the Modelfile,
    so the effective context window is whatever Ollama defaults to, not
    necessarily the 32K this project has been assuming."""
    options = {"think": False}  # the one option agent.py's chat() call actually sets explicitly
    try:
        raw = ollama_show(model).parameters or ""
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2:
                name, value = parts
                try:
                    value = float(value) if "." in value else int(value)
                except ValueError:
                    pass
                options[name] = value
    except Exception as e:
        options["_show_error"] = str(e)
    return options


def run_agent(task, tools, iteration_budget=ITERATION_BUDGET, task_id=None, memory_policy="append-all",
              forbidden_paths=None, requires_code_changes=True, max_wall_clock_seconds=None):
    """max_wall_clock_seconds, if given, caps real elapsed time rather than
    turn count — for a genuinely hard, open-ended task where a fixed
    iteration_budget is the wrong constraint (a model that's making real,
    slow progress shouldn't be cut off mid-investigation just because it
    hit an arbitrary turn number). iteration_budget still applies as an
    outer backstop; pass a very large value to make it effectively
    non-binding when wall-clock time is the real limit."""
    TASK_STATE["done"] = False
    TASK_STATE["summary"] = None
    run_started = time.monotonic()

    run_store = RunStore(
        task_id=task_id or f"adhoc-{int(time.time())}", task_text=task, model=MODEL,
        model_options=_live_model_options(MODEL), project_root=get_root(),
        memory_policy=memory_policy, iteration_budget=iteration_budget,
    )
    print(f"🗂️  Recording run '{run_store.run_id}' to {run_store.run_dir}")

    initial_git_head = current_git_head(get_root())
    print(f"🌿 Initial git HEAD: {initial_git_head[:12] if initial_git_head else '(not a git repo)'}")

    # Phase 4 controller tools + Phase 5 memory tools — bound to this run's
    # store since their evidence gates / retrieval need to read this run's
    # own event log. Ordinary tool functions otherwise: dispatch_tool_calls
    # records them exactly like read_file/write_file, so every ledger is
    # always rebuildable from the same event log as everything else.
    subgoal_create, subgoal_complete = make_subgoal_tools(run_store, model=MODEL)
    hypothesis_record, hypothesis_resolve = make_hypothesis_tools(run_store)
    memory_status, memory_recall, memory_expand = make_memory_tools(run_store)
    tools = tools + [subgoal_create, subgoal_complete, hypothesis_record, hypothesis_resolve,
                      memory_status, memory_recall, memory_expand]
    tool_map = {fn.__name__: fn for fn in tools}

    system_prompt = f"""You are a Principal Software Engineer running locally via hardware acceleration.
You are working inside this directory: {get_root()}
Every tool you have is confined to this directory and its subdirectories — you cannot read or write
anything outside it, and attempts to do so will be rejected.

Every path you pass to a tool is ALREADY relative to that directory. Pass just "src/app.py", never prefix
it with the directory's own name — doing so creates an unwanted nested directory instead of reaching the
real file.

You have a full toolbelt: read_file, write_file, patch_file, list_workspace, run_shell, search_file,
list_symbols, list_dir, diff_files, run_tests, grep_dir, git_status, git_diff, web_search, fetch,
subgoal_create, subgoal_complete, hypothesis_record, hypothesis_resolve, memory_status, memory_recall,
memory_expand, and finish_task.

- Use patch_file for small surgical edits; `search` must match the existing text exactly — call read_file
  first if you're not certain of the current contents.
- Use grep_dir/list_symbols/list_dir to understand code before changing it (grep_dir searches the whole
  project at once — prefer it over calling search_file file-by-file), run_tests to verify a change,
  diff_files or git_diff to review one.
- Use git_status/git_diff (the dedicated tools, NOT `git status`/`git diff` via run_shell) to review
  everything you've changed so far before calling finish_task. Outside a git repository, git itself prints
  a large usage/help dump for a raw `git diff` command instead of a clean error — the dedicated git_diff
  tool fails with one short line instead. If git_diff isn't applicable, read_file every file you changed.
- Use web_search to find information or documentation, fetch to read a specific URL's full content.
- Files you write are NOT automatically executed — this isn't a throwaway sandbox, so verify your own work
  explicitly with run_tests or run_shell rather than assuming a write succeeded because it didn't error.
- If the fix touches more than one file or step, call subgoal_create for EACH piece before starting the
  first one — state your whole plan, not just the next action. subgoal_complete is REJECTED unless you've
  actually done something (a read, a write, a test) since creating that subgoal; restating your reasoning
  does not satisfy it.
- Use hypothesis_record before testing a belief, and hypothesis_resolve afterward citing the real result
  that showed whether it held — not from reasoning alone.
- Use memory_status if you're unsure what's already been established (current phase, open subgoals/
  hypotheses, most recent failure). Use memory_recall to search past subgoal summaries and files touched
  instead of re-reading something you suspect you've already looked at. Use memory_expand with a ref=
  you saw in memory_recall or the current-state summary to pull back the full, untruncated detail behind it.
- When — and only when — the task is fully complete, call finish_task with a short summary of what you did.
  finish_task is a REQUEST, not a guarantee — it will be REJECTED unless you've actually changed a file, run
  a test or command afterward with no new failure, and reviewed the final diff (git_diff/diff_files). If
  rejected, the reason names exactly what's missing — address that specific thing, don't just re-explain.
  Returning plain text without calling finish_task does not end the task; you are expected to keep working.

CRITICAL RULE — DO NOT SKIP THIS: The MOMENT you can name the specific file, function, and line that
causes the bug, YOU MUST CALL patch_file OR write_file ON YOUR VERY NEXT TURN. Not after one more
read_file. Not after one more grep_dir. Not after "just confirming" with run_shell. THE NEXT TURN.
Re-reading code you have already read, re-stating a diagnosis you have already made, or "double-checking"
a hypothesis you already believe is CORRECT is NOT PROGRESS and IS FORBIDDEN once you can name the fix
location. WRITE THE PATCH FIRST. Verify it with run_tests AFTER, using the REAL test result — not more
reasoning — to decide whether to refine it. An imperfect patch you can iterate on beats a perfect diagnosis
you never act on."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    turns_since_last_write = 0
    diagnosed_symbol_turns = {}  # symbol -> list of iterations it was flagged as the cause
    subgoal_nudge_turns = {}  # subgoal_id -> iteration it was nudged about — once each, never repeated

    for iteration in range(1, iteration_budget + 1):
        if max_wall_clock_seconds is not None and (time.monotonic() - run_started) >= max_wall_clock_seconds:
            elapsed_min = (time.monotonic() - run_started) / 60
            print("\n" + "=" * 60)
            print(f"⏰ WALL-CLOCK LIMIT REACHED: {elapsed_min:.1f} min elapsed "
                  f"(limit {max_wall_clock_seconds / 60:.0f} min) at iteration {iteration}/{iteration_budget}.")
            print("=" * 60)
            run_store.record_task_finished(iteration=iteration - 1, outcome="wall_clock_exhausted")
            return False

        print(f"\n🌀 [Iteration {iteration}/{iteration_budget}] Calling {MODEL}...")

        compiled_messages = compile_context(memory_policy, messages, run_dir=run_store.run_dir)

        # A long unattended run must not die to one transient API hiccup —
        # confirmed live, twice: a malformed-tool-call XML response from
        # Ollama (a 500, "element <function> closed by </parameter>") with
        # zero retry handling crashed the whole process outright. A first
        # fix (5 retries, backoff capped at 30s) still wasn't enough —
        # confirmed live a second time: this error recurred on 3 of the
        # first 4 turns of a real overnight run, and the 4th occurrence
        # didn't clear within 5 attempts, ending the entire run at
        # iteration 4. Since the OTHER 3 occurrences each cleared after
        # just one retry, this looks recoverable with more patience, not
        # fundamentally broken — MAX_CHAT_RETRIES raised substantially and
        # backoff extended so a bad streak has real room to clear before
        # the run gives up.
        call_started = time.monotonic()
        response = None
        last_error = None
        for attempt in range(1, MAX_CHAT_RETRIES + 1):
            try:
                response = chat(model=MODEL, messages=compiled_messages, tools=tools, think=False)
                break
            except Exception as e:
                last_error = e
                print(f"⚠️  chat() failed (attempt {attempt}/{MAX_CHAT_RETRIES}): {type(e).__name__}: {e}")
                if attempt < MAX_CHAT_RETRIES:
                    time.sleep(min(120, 2 ** attempt))
        if response is None:
            print(f"\n❌ chat() failed {MAX_CHAT_RETRIES} times in a row — ending run cleanly rather than "
                  f"crashing. Last error: {last_error}")
            run_store.record_task_finished(iteration=iteration, outcome="api_failure",
                                            summary=f"Aborted after repeated chat() failures: {last_error}")
            return False
        latency_ms = int((time.monotonic() - call_started) * 1000)
        msg = response.message
        messages.append(msg)

        run_store.record_model_call(
            iteration=iteration, prompt_preview=(messages[-2]["content"] if len(messages) >= 2 else ""),
            response_text=msg.content, input_tokens=response.prompt_eval_count,
            output_tokens=response.eval_count, latency_ms=latency_ms,
            compiled_context_tokens_estimate=estimate_tokens(compiled_messages),
        )

        if msg.content:
            print(f"🧠 {msg.content}")

        if not msg.tool_calls:
            print("⚠️  Model returned no tool call — nudging it to act.")
            messages.append({
                "role": "user",
                "content": "You must call a tool to make progress, or call finish_task if you're actually done.",
            })
            continue

        reverted_patch_event_id = [None]  # mutable cell _record_tool_call can write into

        def _record_tool_call(tool_name, arguments, full_result, _iteration=iteration):
            # Phase 2's changed-entity reducer needs real before/after file
            # content to compute a diff. The event log can't get that at
            # reconstruction time (the file may have moved on, or the run
            # may be against a checkout that no longer exists) — so it's
            # captured here, now, while the file is still on disk and this
            # write just succeeded.
            post_content = None
            if tool_name in ("write_file", "patch_file") and not full_result.startswith(("ERROR", "REJECTED")):
                try:
                    with open(os.path.join(get_root(), arguments.get("path", "")), "r", encoding="utf-8") as f:
                        post_content = f.read()
                except OSError:
                    post_content = None

            if tool_name == "patch_file" and not full_result.startswith(("ERROR", "REJECTED")):
                reverted_patch_event_id[0] = detect_patch_reversion(
                    run_store.run_dir, arguments.get("path"), arguments.get("search"), arguments.get("replace"),
                )

            # Returned to dispatch_tool_calls so its truncation message (if
            # this result is over MAX_MESSAGE_CONTENT_CHARS) can point the
            # model at memory_expand(ref=<this exact event_id>) instead of
            # relying on it to remember a generic prompt mention.
            return run_store.record_tool_call(iteration=_iteration, tool_name=tool_name, arguments=arguments,
                                               result_text=full_result, post_content=post_content)

        tool_messages = dispatch_tool_calls(msg.tool_calls, tool_map, recorder=_record_tool_call)
        messages.extend(tool_messages)

        # Separation of concerns: state tracking belongs to the
        # deterministic runtime, not to whether the model remembers to
        # call subgoal_complete. Creating a NEW subgoal is a real,
        # reducer-visible transition — the model moving on — so any
        # EARLIER subgoal that already has real progress gets closed here,
        # regardless of whether the model ever explicitly closes it. One
        # with no progress yet (e.g. two subgoals declared back-to-back
        # before any work starts) is correctly left open — see
        # auto_close_open_subgoals' own guard.
        if any(m["tool_name"] == "subgoal_create" and not m["content"].startswith(("ERROR", "REJECTED"))
               for m in tool_messages):
            auto_closed = auto_close_open_subgoals(run_store, MODEL, iteration, reason="new subgoal created")
            for sid in auto_closed:
                print(f"🔒 Auto-closed '{sid}' — runtime detected real progress before the model moved on.")

        # Duplicate-subgoal escalation — quantified live on the sympy-13878
        # overnight run: the model recreated the identical subgoal 8 times
        # (70% of the whole run) because nothing carried "you already
        # scoped and abandoned this" forward. subgoal_create's own
        # duplicate_subgoal_check blocks the 1st recreation with the prior
        # attempt's episode injected as a lesson — a real chance to act on
        # it. If a 2ND recreation of the SAME goal still happens after
        # that, blocking-and-nudging has already been tried and failed
        # once; re-deriving the same check here (stateless, same event
        # log) and ending the run beats burning the rest of the budget on
        # a 3rd identical cycle. Deliberately NOT another nudge — the
        # watchdog/repetition-detector nudges above already proved, live,
        # that asking nicely doesn't reliably change this behavior.
        for call in msg.tool_calls:
            if call.function.name != "subgoal_create":
                continue
            if should_escalate_duplicate(run_store.run_dir, call.function.arguments.get("goal", "")):
                print("\n" + "=" * 60)
                print("🛑 DUPLICATE SUBGOAL ESCALATION: this goal has been recreated after already being "
                      "blocked once — ending the run rather than repeating the cycle again.")
                print("=" * 60)
                run_store.record_task_finished(
                    iteration=iteration, outcome="stuck_duplicate_subgoal",
                    summary=f"Repeatedly recreated the same subgoal without executing it: "
                            f"{call.function.arguments.get('goal', '')[:200]}",
                )
                return False

        # Enforced-grammar nudge for "action instability" — a live run
        # surfaced a new failure mode distinct from the already-documented
        # "plan instability": the model correctly patched a bug fix, then
        # one patch later reverted its own fix back to the buggy version,
        # then reapplied it a third time. detect_patch_reversion() is
        # purely mechanical (string comparison on recorded arguments) —
        # it doesn't judge whether a revert was justified, it forces the
        # model to say so explicitly instead of silently undoing verified
        # work. Takes priority over the subgoal-completion nudge below
        # (it's about something that just happened this exact turn).
        if reverted_patch_event_id[0]:
            print(f"↩️  Reversion detected: this turn's patch undoes {reverted_patch_event_id[0]} — forcing justification.")
            messages.append({
                "role": "user",
                "content": (
                    f"STOP. The patch you just applied exactly reverts an earlier patch you already made and "
                    f"had verified ({reverted_patch_event_id[0]}) — you've undone your own confirmed fix. "
                    f"Respond using EXACTLY this two-line format, as the very first thing in your next message:\n\n"
                    f"REVERT_JUSTIFICATION: <\"mistake\" or \"new evidence\">\n"
                    f"ACTION: <what you are about to do>\n\n"
                    f"If REVERT_JUSTIFICATION is \"mistake\", ACTION must be \"re-applying the correct fix\" and "
                    f"you must call patch_file to restore it in this SAME turn. If REVERT_JUSTIFICATION is "
                    f"\"new evidence\", ACTION must name the SPECIFIC new observation (a failing test, a new "
                    f"symptom) that justifies undoing verified work — not general uncertainty."
                ),
            })

        messages_before_nudges = len(messages)

        wrote_this_turn = any(
            m["tool_name"] in ("write_file", "patch_file") and not m["content"].startswith(("ERROR", "REJECTED"))
            for m in tool_messages
        )
        if wrote_this_turn:
            turns_since_last_write = 0
            diagnosed_symbol_turns = {}
        else:
            turns_since_last_write += 1

            repeated_symbol = None
            first_mention_symbol = None
            for symbol in _diagnosed_symbols(msg.content):
                seen_turns = diagnosed_symbol_turns.setdefault(symbol, [])
                seen_turns.append(iteration)
                if len(seen_turns) == 1 and first_mention_symbol is None:
                    first_mention_symbol = symbol
                elif len(seen_turns) >= 2 and repeated_symbol is None:
                    repeated_symbol = symbol

            if first_mention_symbol and not repeated_symbol:
                print(f"🤔 Confidence checkpoint: '{first_mention_symbol}' flagged as a likely cause "
                      f"(turn {iteration}) — asking for an explicit commit-or-explain decision.")
                messages.append({
                    "role": "user",
                    "content": (
                        f"You just identified `{first_mention_symbol}` as a likely cause of this bug. Before "
                        f"you do anything else, you MUST respond using EXACTLY this two-line format, as the "
                        f"very first thing in your next message:\n\n"
                        f"CONFIDENCE: <a whole number from 1 to 10>\n"
                        f"ACTION: <what you are about to do>\n\n"
                        f"For CONFIDENCE, use the full range honestly — 1 means \"pure guess, I have not "
                        f"actually traced this against the real code or confirmed it explains the reported "
                        f"symptom\"; 10 means \"certain — I have read the exact lines involved and can explain "
                        f"precisely why they produce the bug described\". Do not default to a middle number; "
                        f"pick the value that actually reflects how sure you are right now.\n\n"
                        f"For ACTION: if your CONFIDENCE is 6 or higher, ACTION must be \"writing the fix now\" "
                        f"— and you must then actually call patch_file or write_file in this SAME turn, "
                        f"immediately after these two lines. You can still refine the fix afterward using real "
                        f"test feedback; being confident does not mean being finished. If your CONFIDENCE is 5 "
                        f"or lower, ACTION must name the ONE specific, concrete question you need answered "
                        f"before you could write a fix — a precise thing you don't yet know, not a vague plan "
                        f"like \"investigate further\" or \"look at related code\". Then your very next tool "
                        f"call must be aimed directly at answering that exact question, not a repeat of "
                        f"something you have already checked."
                    ),
                })

            if repeated_symbol:
                print(f"🔁 Repetition detected: '{repeated_symbol}' flagged as the cause on turns "
                      f"{diagnosed_symbol_turns[repeated_symbol]} — forcing action.")
                messages.append({
                    "role": "user",
                    "content": (
                        f"STOP. You have already identified `{repeated_symbol}` as the cause of this bug "
                        f"MORE THAN ONCE (turns {diagnosed_symbol_turns[repeated_symbol]}). You do NOT need "
                        f"to confirm this again. THIS TURN, call patch_file or write_file on `{repeated_symbol}` "
                        f"— no other tool is an acceptable response. Then use run_tests to find out if it "
                        f"actually worked. Re-deriving the same conclusion a third time is NOT progress."
                    ),
                })
                # Deliberately NOT resetting turns_since_last_write here — an
                # earlier version did, and it meant the fallback watchdog below
                # could never fire again once repetition triggered late in the
                # budget (turn 22 + 28 > a 35-turn budget). The backstop has to
                # stay live even when this trigger fires and still gets ignored.
            if turns_since_last_write >= WATCHDOG_TURNS_WITHOUT_WRITE:
                print(f"⏰ Watchdog: {turns_since_last_write} turns without a successful write — forcing action.")
                messages.append({
                    "role": "user",
                    "content": (
                        f"STOP INVESTIGATING. You have spent {turns_since_last_write} turns without writing "
                        f"any code. THIS TURN, you MUST call patch_file or write_file — no other tool is an "
                        f"acceptable response. If you are not 100% certain of the fix, WRITE YOUR BEST GUESS "
                        f"ANYWAY. Being wrong and finding out from a real test result is progress. Reading "
                        f"more code is NOT."
                    ),
                })

            # Layers on top of the two mechanisms above, doesn't replace
            # them: those fire on "no successful WRITE" (built for, and
            # tested against, single-symbol stalling). This fires on "no
            # successful ANYTHING" — a broader, more severe stall (not even
            # a new read or a declared subgoal) that those can't see,
            # e.g. re-litigating the same file across many turns without
            # ever advancing to the next part of a multi-file fix.
            nudge = stagnation_nudge(run_store.run_dir, iteration)
            if nudge:
                print(f"🧊 Stagnation detected — forcing a re-plan.")
                messages.append({"role": "user", "content": nudge})

        # Enforced-grammar nudge for subgoal_complete — same pattern as the
        # confidence checkpoint above, applied to the same problem it
        # surfaced live: the model reliably creates and works subgoals but
        # doesn't reliably close them explicitly (see IMPLEMENTATION_LOG.md,
        # Phase 5). auto_close_open_subgoals above is the deterministic
        # backstop that guarantees data doesn't depend on this; this nudge
        # is the behavioral push to get an explicit close when possible —
        # skipped if something else already claimed this turn's one
        # enforced-grammar slot, to avoid stacking conflicting formats.
        if len(messages) == messages_before_nudges:
            for sid, entry in open_subgoals_with_progress(run_store.run_dir).items():
                if sid in subgoal_nudge_turns:
                    continue
                subgoal_nudge_turns[sid] = iteration
                print(f"📋 Subgoal checkpoint: '{sid}' has real progress but isn't closed — "
                      f"asking for an explicit decision.")
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have real, recorded progress for open subgoal `{sid}` ({entry['goal']}) but "
                        f"haven't called subgoal_complete for it. Before doing anything else, respond using "
                        f"EXACTLY this two-line format, as the very first thing in your next message:\n\n"
                        f"SUBGOAL: {sid}\n"
                        f"DECISION: <\"complete\" or \"continue\">\n\n"
                        f"If DECISION is \"complete\", you MUST call subgoal_complete on `{sid}` in this SAME "
                        f"turn, immediately after these two lines. If DECISION is \"continue\", name the ONE "
                        f"specific thing still needed before it's actually done — not a vague plan."
                    ),
                })
                break  # one enforced-grammar nudge per turn

        # Grace-period backstop: the two transition triggers above (new
        # subgoal_create, finish_task) don't fire if the model just keeps
        # working without either — a real live run hit exactly this, and
        # the open subgoal's raw detail fell out of the recent-tail window
        # with nothing compensating for it (no episode ever created),
        # plausibly why that run never converged. Once the enforced-grammar
        # nudge above has already given the model one real chance to close
        # it explicitly, auto-close it a few turns later if it still
        # hasn't — bounded, not indefinite, and doesn't undercut the nudge
        # the way auto-closing on every turn would (that would make the
        # nudge moot before the model even saw it).
        for sid, nudged_at in list(subgoal_nudge_turns.items()):
            if iteration - nudged_at >= SUBGOAL_AUTO_CLOSE_GRACE_TURNS:
                closed = auto_close_open_subgoals(run_store, MODEL, iteration, reason="grace period after nudge")
                if closed:
                    for c in closed:
                        print(f"🔒 Auto-closed '{c}' — grace period after the enforced-grammar nudge elapsed.")
                break  # auto_close_open_subgoals already handles every eligible subgoal in one pass

        if TASK_STATE["done"]:
            # finish_task is itself a transition — the model considers the
            # whole task done, so any subgoal still open (with real
            # progress) is closed here too, same as the new-subgoal
            # trigger above. Runs regardless of whether the gate below
            # ends up allowing completion — the model moving to declare
            # done is the signal, not whether that declaration is honored.
            auto_closed = auto_close_open_subgoals(run_store, MODEL, iteration, reason="finish_task called")
            for sid in auto_closed:
                print(f"🔒 Auto-closed '{sid}' — runtime detected real progress before finish_task.")

            gate = evaluate_completion_gate(
                run_store.run_dir, project_root=get_root(), initial_git_head=initial_git_head,
                forbidden_paths=forbidden_paths, requires_code_changes=requires_code_changes,
            )
            if not gate["allowed"]:
                print(f"\n🚫 finish_task REJECTED: {'; '.join(gate['reasons'])}")
                TASK_STATE["done"] = False
                messages.append({
                    "role": "user",
                    "content": (
                        f"finish_task was rejected: {'; '.join(gate['reasons'])}. This is not a request for "
                        f"more explanation — it means a specific, checkable condition wasn't met. Address it "
                        f"directly, then call finish_task again."
                    ),
                })
            else:
                print(f"\n✅ DONE ({gate['outcome']}): {TASK_STATE['summary']}")
                for reason in gate["reasons"]:
                    print(f"   - {reason}")
                run_store.record_task_finished(iteration=iteration, outcome=gate["outcome"],
                                                summary=TASK_STATE["summary"])
                return True

    print("\n" + "=" * 60)
    print(f"❌ INCOMPLETE: finish_task was not called within {iteration_budget} iterations.")
    print("=" * 60)
    run_store.record_task_finished(iteration=iteration_budget, outcome="budget_exhausted")
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the coding agent against a real task.")
    parser.add_argument("task", nargs="+", help="The task to work on.")
    parser.add_argument(
        "--project", default=None,
        help="Directory to confine the agent to. Defaults to evolutionEngine/workspace.",
    )
    args = parser.parse_args()

    if args.project:
        try:
            set_root(args.project)
        except NotADirectoryError as e:
            raise SystemExit(f"❌ {e}")

    # A real project's files aren't a throwaway sandbox — don't auto-execute
    # whatever the model just wrote. See kernel/io_tools.py's AUTO_RUN_AFTER_WRITE.
    io_tools.AUTO_RUN_AFTER_WRITE["enabled"] = False

    task = " ".join(args.task)
    tools = load_registry() + [finish_task]

    print(f"📁 Operating in: {get_root()}")
    print(f"🧰 Loaded {len(tools)} tool(s): {[fn.__name__ for fn in tools]}")

    run_agent(task, tools)
