"""The consumer, not the bootstrapper: takes a real task instead of a
curriculum goal, and works it with the full tool registry. Not a curriculum
goal itself — same tier as harness.py, hand-written orchestration code.
"""

import sys

from ollama import chat

from kernel.control import TASK_STATE, finish_task
from registry import load_registry

MODEL = "qwen3.6:35b-mlx"
ITERATION_BUDGET = 20


def run_agent(task, tools, iteration_budget=ITERATION_BUDGET):
    TASK_STATE["done"] = False
    TASK_STATE["summary"] = None
    tool_map = {fn.__name__: fn for fn in tools}

    system_prompt = """You are a Principal Software Engineer running locally via hardware acceleration.
You have a full toolbelt for working in `./workspace/`: read_file, write_file, patch_file, list_workspace,
run_shell, search_file, list_symbols, list_dir, diff_files, run_tests, and finish_task.

- Use patch_file for small surgical edits; `search` must match the existing text exactly — call read_file
  first if you're not certain of the current contents.
- Use search_file/list_symbols/list_dir to understand code before changing it, run_tests to verify a change,
  diff_files to review one.
- Writing a .py file is automatically run in a sandbox immediately afterward and you'll be told whether it
  executed cleanly.
- When — and only when — the task is fully complete, call finish_task with a short summary of what you did.
  Returning plain text without calling finish_task does not end the task; you are expected to keep working."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    for iteration in range(1, iteration_budget + 1):
        print(f"\n🌀 [Iteration {iteration}/{iteration_budget}] Calling {MODEL}...")

        response = chat(model=MODEL, messages=messages, tools=tools)
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

        for call in msg.tool_calls:
            fn = tool_map.get(call.function.name)
            if fn is None:
                result = f"ERROR: unknown tool '{call.function.name}'."
            else:
                print(f"🔧 {call.function.name}({call.function.arguments})")
                try:
                    result = fn(**call.function.arguments)
                except TypeError as e:
                    result = f"ERROR: bad arguments for {call.function.name}: {e}"
                except ValueError as e:
                    result = f"REJECTED: {e}"
            print(result)
            messages.append({"role": "tool", "tool_name": call.function.name, "content": result})

        if TASK_STATE["done"]:
            print(f"\n✅ DONE: {TASK_STATE['summary']}")
            return True

    print("\n" + "=" * 60)
    print(f"❌ INCOMPLETE: finish_task was not called within {iteration_budget} iterations.")
    print("=" * 60)
    return False


if __name__ == "__main__":
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
    else:
        task = input("Task: ").strip()

    tools = load_registry() + [finish_task]
    print(f"🧰 Loaded {len(tools)} tool(s): {[fn.__name__ for fn in tools]}")

    run_agent(task, tools)
