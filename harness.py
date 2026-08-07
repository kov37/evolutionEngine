import os

from ollama import chat

from curriculum import CURRICULUM
from dispatch import dispatch_tool_calls
from kernel.io_tools import RUN_STATE
from kernel.sandbox import get_root
from registry import load_registry, verify, verify_and_promote, is_promoted

MODEL = "qwen3.6:35b-mlx"


def run_recursive_engine(master_goal, tools, function_name=None, iteration_budget=5):
    RUN_STATE["goal_met"] = False
    RUN_STATE["target_file"] = None
    tool_map = {fn.__name__: fn for fn in tools}

    system_prompt = """You are a Principal Software Engineer running locally via hardware acceleration.
Your job is to incrementally build standalone Python tools that satisfy the goal you are given, using the
tools available to you.

- Every path you pass to a tool (write_file, patch_file, read_file, list_dir, etc.) is ALREADY relative to
  your working directory. Pass just the filename, e.g. path="my_tool.py" — never prefix it with "workspace/"
  or any other directory name pointing at that root itself, or you will create an unwanted nested directory.
- Use write_file to create a brand new file or to fully rewrite one.
- Use patch_file only for a small surgical edit to a file that already exists; `search` must match the existing text exactly, including whitespace — call read_file first if you're not certain of the current contents.
- Use list_workspace to check what already exists, and run_shell to run tests or install anything you need.
- Writing a .py file is automatically run in a sandbox immediately afterward and you will be told whether it executed cleanly. Keep iterating until it does.
- The file must be directly runnable Python — do not substitute explanation or commentary for working code."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": master_goal},
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
                "content": "You must call a tool to make progress. A plain-text response makes no change.",
            })
            continue

        messages.extend(dispatch_tool_calls(msg.tool_calls, tool_map))

        if RUN_STATE["goal_met"]:
            if function_name:
                module_path = os.path.join(get_root(), RUN_STATE["target_file"])
                ok, err = verify(module_path, function_name)
                if not ok:
                    print(f"⚠️  '{RUN_STATE['target_file']}' ran cleanly, but doesn't satisfy the goal: {err}")
                    RUN_STATE["goal_met"] = False
                    messages.append({
                        "role": "user",
                        "content": (
                            f"'{RUN_STATE['target_file']}' ran without error, but it does not define a "
                            f"callable function named `{function_name}` as the objective requires — "
                            f"{err}. Running cleanly is not enough; the file must expose exactly that "
                            f"function. Continue working."
                        ),
                    })
                    continue

            print(f"\n🎉 WIN! Created tool verified: workspace/{RUN_STATE['target_file']}")
            return True

    print("\n" + "=" * 60)
    print(f"❌ FAILED: Goal not met within {iteration_budget} iterations.")
    print("=" * 60)
    return False


if __name__ == "__main__":
    tools = load_registry()
    print(f"🧰 Loaded {len(tools)} tool(s): {[fn.__name__ for fn in tools]}")

    for entry in CURRICULUM:
        if entry.get("function_name") and is_promoted(entry["name"]):
            print(f"\n⏭️  Skipping '{entry['name']}' — already promoted.")
            continue

        print(f"\n{'#' * 60}\n# GOAL: {entry['name']}\n{'#' * 60}")
        won = run_recursive_engine(entry["goal"], tools, function_name=entry.get("function_name"))

        if won and entry.get("function_name"):
            module_path = os.path.join(get_root(), RUN_STATE["target_file"])
            ok, err = verify_and_promote(
                entry["name"], module_path, entry["function_name"],
                entry.get("description", ""), entry.get("path_params"),
            )
            if ok:
                print(f"🧬 Promoted '{entry['name']}' — available as a tool to every goal after this one.")
                tools = load_registry()
                print(f"🧰 Registry now has {len(tools)} tool(s): {[fn.__name__ for fn in tools]}")
            else:
                print(f"⚠️  Won the goal but promotion failed: {err}")
