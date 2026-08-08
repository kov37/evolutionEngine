"""The consumer, not the bootstrapper: takes a real task instead of a
curriculum goal, and works it with the full tool registry. Not a curriculum
goal itself — same tier as harness.py, hand-written orchestration code.

Defaults to evolutionEngine/workspace like harness.py, but --project can
point the whole toolbelt (kernel tools + every confined graduated tool) at
any real directory — see kernel/sandbox.py for how that confinement works.
"""

import argparse
import re

from ollama import chat

import kernel.io_tools as io_tools
from dispatch import dispatch_tool_calls
from kernel.control import TASK_STATE, finish_task
from kernel.sandbox import get_root, set_root
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


def _diagnosed_symbols(text: str) -> set:
    """Symbols the model named alongside diagnostic language this turn —
    only meaningful if the SAME symbol shows up this way on a later turn
    too (see REPETITION_DIAGNOSIS below)."""
    if not text or not DIAGNOSTIC_PHRASE_PATTERN.search(text):
        return set()
    return set(BACKTICK_SYMBOL_PATTERN.findall(text))


def run_agent(task, tools, iteration_budget=ITERATION_BUDGET):
    TASK_STATE["done"] = False
    TASK_STATE["summary"] = None
    tool_map = {fn.__name__: fn for fn in tools}

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

    for iteration in range(1, iteration_budget + 1):
        print(f"\n🌀 [Iteration {iteration}/{iteration_budget}] Calling {MODEL}...")

        response = chat(model=MODEL, messages=messages, tools=tools, think=False)
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
