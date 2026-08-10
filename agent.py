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
import sidecar
from dispatch import dispatch_tool_calls
from kernel.control import TASK_STATE, finish_task
from kernel.sandbox import get_root, set_root
from registry import load_registry

MODEL = "qwen3.6:35b-mlx"
ITERATION_BUDGET = 20

# Chosen for latency, not memory — throughput collapses well before the
# memory ceiling on this hardware (215 tok/s at num_ctx=262144 vs.
# 1,489 tok/s here). See REFACTORING_LEARNINGS.md findings #19-21.
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


def run_agent(task, tools, iteration_budget=ITERATION_BUDGET, sidecar_enabled=False):
    TASK_STATE["done"] = False
    TASK_STATE["summary"] = None
    tool_map = {fn.__name__: fn for fn in tools}
    sidecar_log = []

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

        response = None
        last_error = None
        for attempt in range(1, MAX_CHAT_RETRIES + 1):
            try:
                response = chat(model=MODEL, messages=messages, tools=tools, think=False,
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

        tool_messages = dispatch_tool_calls(msg.tool_calls, tool_map)
        messages.extend(tool_messages)

        if sidecar_enabled:
            for call, tmsg in zip(msg.tool_calls, tool_messages):
                sidecar_log.append(
                    sidecar.summarize_call(call.function.name, call.function.arguments, tmsg["content"])
                )
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
    if args.sidecar:
        print("🗒️  Automated activity-log sidecar: ENABLED")

    run_agent(task, tools, sidecar_enabled=args.sidecar)
