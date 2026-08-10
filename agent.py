"""The consumer, not the bootstrapper: takes a real task instead of a
curriculum goal, and works it with the full tool registry. Not a curriculum
goal itself — same tier as harness.py, hand-written orchestration code.

Defaults to evolutionEngine/workspace like harness.py, but --project can
point the whole toolbelt (kernel tools + every confined graduated tool) at
any real directory — see kernel/sandbox.py for how that confinement works.
"""

import argparse
import time

from ollama import chat

import kernel.io_tools as io_tools
import kernel.memory as memory
import sidecar
import structured_state
import worker
from dispatch import dispatch_tool_calls
from kernel.control import TASK_STATE, finish_task
from kernel.sandbox import get_root, set_root
from registry import load_registry

MODEL = "qwen3.6:35b-mlx"
ITERATION_BUDGET = 20

# context_summary_enabled only: how many of the most recent tool-result
# messages stay verbatim in `messages` before being pruned to a short
# pointer (kernel.memory.recall still has the full original). Bounds
# `messages`'s own growth — the exact problem that hit num_ctx's ceiling
# live during testing (97.5% full by iteration 39, zero writes yet).
KEEP_RECENT_RAW_RESULTS = 5

# Chosen for latency, not memory — throughput collapses well before the
# memory ceiling on this hardware (215 tok/s at num_ctx=262144 vs.
# 1,489 tok/s here). See REFACTORING_LEARNINGS.md findings #19-21.
# Lowered from 65536 to 32768 when the orchestrator-worker pattern was
# introduced (qwen3.5:9b running concurrently as the compression worker) —
# leaves more headroom for the second model to stay resident alongside this
# one without either getting pushed toward GPU offload.
NUM_CTX = 32768

# A transient Ollama-side hiccup (e.g. "XML syntax error... element <function>
# closed by </parameter>", a malformed-tool-call response from the model that
# the server can't parse) must not crash the whole run outright — confirmed
# live, twice, in this project's real history: a first fix (5 retries, 30s
# backoff cap) still wasn't enough on a real overnight run, so the retry
# count and backoff cap here are the values that actually held up, not a
# fresh guess. Exponential, capped, so a genuinely dead server still gives up
# in reasonable time rather than retrying forever.
MAX_CHAT_RETRIES = 20


def run_agent(task, tools, iteration_budget=ITERATION_BUDGET, sidecar_enabled=False, worker_enabled=False,
              context_summary_enabled=False, structured_summary_enabled=False):
    TASK_STATE["done"] = False
    TASK_STATE["summary"] = None
    if context_summary_enabled:
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

    system_prompt = f"""You are a Principal Software Engineer running locally via hardware acceleration.
You are working inside this directory: {get_root()}
Every tool you have is confined to this directory and its subdirectories — you cannot read or write
anything outside it, and attempts to do so will be rejected.

Every path you pass to a tool is ALREADY relative to that directory. Pass just "src/app.py", never prefix
it with the directory's own name — doing so creates an unwanted nested directory instead of reaching the
real file.

You have a full toolbelt: read_file, write_file, patch_file, list_workspace, run_shell, search_file,
list_symbols, list_dir, diff_files, run_tests, grep_dir, git_status, git_diff, web_search, fetch, and
finish_task.

- Use patch_file for small surgical edits; `search` must match the existing text exactly — call read_file
  first if you're not certain of the current contents.
- Use grep_dir/list_symbols/list_dir to understand code before changing it (grep_dir searches the whole
  project at once — prefer it over calling search_file file-by-file), run_tests to verify a change,
  diff_files or git_diff to review one.
- Use git_status/git_diff to review everything you've changed so far before calling finish_task, if the
  project is a git repository.
- Use web_search to find information or documentation, fetch to read a specific URL's full content.
- Files you write are NOT automatically executed — this isn't a throwaway sandbox, so verify your own work
  explicitly with run_tests or run_shell rather than assuming a write succeeded because it didn't error.
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
            messages_for_call = messages + [{"role": "system", "content": state.render()}]
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

        response = None
        last_error = None
        for attempt in range(1, MAX_CHAT_RETRIES + 1):
            try:
                response = chat(model=MODEL, messages=messages_for_call, tools=tools, think=False,
                                 options={"num_ctx": NUM_CTX})
                break
            except Exception as e:
                last_error = e
                print(f"⚠️  chat() failed (attempt {attempt}/{MAX_CHAT_RETRIES}): {type(e).__name__}: {e}")
                if attempt < MAX_CHAT_RETRIES:
                    time.sleep(min(120, 2 ** attempt))
        if response is None:
            print(f"\n❌ chat() failed {MAX_CHAT_RETRIES} times in a row — ending run cleanly rather than "
                  f"crashing. Last error: {last_error}")
            return False

        prompt_eval_s = (response.prompt_eval_duration or 0) / 1e9
        eval_s = (response.eval_duration or 0) / 1e9
        print(f"📏 prompt_tokens={response.prompt_eval_count} ({prompt_eval_s:.1f}s) "
              f"output_tokens={response.eval_count} ({eval_s:.1f}s)")

        msg = response.message
        messages.append(msg)

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
        tool_messages = dispatch_tool_calls(msg.tool_calls, tool_map)
        messages.extend(tool_messages)

        if structured_summary_enabled:
            for call, tmsg in zip(msg.tool_calls, tool_messages):
                state.update(call.function.name, call.function.arguments, tmsg["content"])
            print(f"🧱 [state updated] {state.render()}")
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

        if TASK_STATE["done"]:
            print(f"\n✅ DONE: {TASK_STATE['summary']}")
            return True

    print("\n" + "=" * 60)
    print(f"❌ INCOMPLETE: finish_task was not called within {iteration_budget} iterations.")
    print("=" * 60)
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the coding agent against a real task.")
    parser.add_argument("task", nargs="+", help="The task to work on.")
    parser.add_argument(
        "--project", default=None,
        help="Directory to confine the agent to. Defaults to evolutionEngine/workspace.",
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

    # A real project's files aren't a throwaway sandbox — don't auto-execute
    # whatever the model just wrote. See kernel/io_tools.py's AUTO_RUN_AFTER_WRITE.
    io_tools.AUTO_RUN_AFTER_WRITE["enabled"] = False

    task = " ".join(args.task)
    tools = load_registry() + [finish_task]

    print(f"📁 Operating in: {get_root()}")
    print(f"🧰 Loaded {len(tools)} tool(s): {[fn.__name__ for fn in tools]}")
    if args.structured_summary:
        print("🧱 Structured state mode: ENABLED (files/facts code-tracked, qwen3.5:9b judges Status only)")
    elif args.context_summary:
        print("🗒️  Whole-context summary mode: ENABLED (qwen3.5:9b re-synthesizes one holistic summary)")
    elif args.sidecar:
        print(f"🗒️  Automated activity-log sidecar: ENABLED{' + qwen3.5:9b worker compression' if args.worker else ' (mechanical)'}")

    run_agent(task, tools, sidecar_enabled=args.sidecar, worker_enabled=args.worker,
              context_summary_enabled=args.context_summary,
              structured_summary_enabled=args.structured_summary)
