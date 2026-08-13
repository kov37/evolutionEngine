"""Fixed benchmark tasks with mechanical pass/fail criteria, run against the
current agent.py so "did a change help" is a number, not a vibe.

Each run invokes agent.py as a subprocess (clean process isolation — avoids
having to hand-reset every module-level global like kernel.sandbox's root
between repeated in-process calls) and parses metrics directly out of its
captured stdout.
"""

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(REPO_ROOT, "state", "benchmark", "fixtures")
RUNS_DIR = os.path.join(REPO_ROOT, "state", "benchmark", "runs")
os.makedirs(RUNS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Checkers — mechanical pass/fail, no LLM judgment involved.
# ---------------------------------------------------------------------------

def check_docstring_task(project_dir: str):
    """Every function in the fixture must now have a real docstring, and
    nothing about the fixture's structure should have changed otherwise."""
    expected = {
        "fixture_a.py": ["add", "multiply", "greet"],
        "fixture_b.py": ["is_even", "reverse_string"],
    }
    missing = []
    for filename, funcs in expected.items():
        path = os.path.join(project_dir, filename)
        if not os.path.exists(path):
            return False, f"{filename} is missing entirely"
        try:
            tree = ast.parse(open(path).read())
        except SyntaxError as e:
            return False, f"{filename} no longer parses: {e}"
        found = {
            node.name: ast.get_docstring(node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for fn in funcs:
            if fn not in found:
                missing.append(f"{filename}:{fn} (function no longer exists)")
            elif not found[fn]:
                missing.append(f"{filename}:{fn} (still no docstring)")
    if missing:
        return False, f"Missing: {missing}"
    return True, "All 5 functions have docstrings"


def check_tool_proposal_task(project_dir: str):
    """A new curriculum.py entry must exist, with a function_name that
    doesn't collide with the 5 kernel tools or any prior curriculum entry,
    and the required fields present."""
    KERNEL_TOOL_NAMES = {"read_file", "write_file", "patch_file", "list_workspace", "run_shell"}

    diff_text = subprocess.run(
        ["git", "diff", "curriculum.py"], cwd=project_dir, capture_output=True, text=True,
    ).stdout
    if not diff_text.strip():
        return False, "curriculum.py has no uncommitted changes — nothing was proposed"

    added_names = re.findall(r'^\+\s*"function_name":\s*"([^"]+)"', diff_text, re.MULTILINE)
    if not added_names:
        return False, "No new function_name field found in the diff"

    spec = importlib.util.spec_from_file_location("curriculum", os.path.join(project_dir, "curriculum.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    all_names = [entry["function_name"] for entry in module.CURRICULUM]

    for name in added_names:
        if name in KERNEL_TOOL_NAMES:
            return False, f"Proposed function_name '{name}' duplicates a kernel tool"
        if all_names.count(name) > 1:
            return False, f"Proposed function_name '{name}' duplicates an existing curriculum entry"

    return True, f"New entry added: function_name={added_names}"


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------

TASKS = {
    "docstring": {
        "fixture": "docstring_task",
        "task_text": (
            "Find every function missing a docstring across fixture_a.py and fixture_b.py, "
            "and add a short one-line docstring to each using patch_file. Call finish_task when done."
        ),
        "only_write": "fixture_a.py,fixture_b.py",
        "checker": check_docstring_task,
        "budget": 15,
        "live_repo": False,
    },
    "propose_tool": {
        "fixture": None,  # runs against the live repo — see note in run_once
        "task_text": (
            "IMPORTANT CONTEXT: this system already has 5 kernel-level tools not listed in curriculum.py: "
            "read_file, write_file, patch_file, list_workspace, and run_shell. run_shell is NOT available "
            "this session — do not try it as a fallback. Do not propose anything duplicating those 5 or an "
            "existing curriculum.py entry. Inspect curriculum.py's existing entries, make EXACTLY ONE "
            "web_search call and AT MOST ONE fetch call, then add exactly one new, genuinely non-duplicate "
            "tool entry to curriculum.py via patch_file, following the exact existing pattern. If patch_file "
            "fails on a search-text mismatch, call read_file again before retrying — never guess. Use "
            "git_diff to show the change, then call finish_task."
        ),
        "only_write": "curriculum.py",
        "checker": check_tool_proposal_task,
        "budget": 30,
        "live_repo": True,
    },
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _parse_output_metrics(text):
    """Parses metrics directly from the subprocess's captured stdout. The
    reverted baseline's agent.py doesn't write a persistent log file
    (run_log.py was part of the parked controller rebuild) — it only
    prints — but subprocess.run already captures that in proc.stdout, so
    there's no need for a separate log file to parse."""
    if not text:
        return {"iterations": None, "tool_calls": 0, "duplicate_tool_calls": 0, "done": False, "failure_streak_nudges": 0}
    iterations = len(re.findall(r"🌀 \[Iteration \d+/\d+\]", text))
    tool_call_lines = re.findall(r"^🔧 .+$", text, re.MULTILINE)
    duplicate_tool_calls = len(tool_call_lines) - len(set(tool_call_lines))
    done = "✅ DONE" in text
    # An actual signal for "stuck retrying, not recovering": how many times
    # the failure-streak nudge fired (2+ failures in a row — see agent.py).
    failure_streak_nudges = len(re.findall(r"failed calls in a row", text))
    novelty = None
    novelty_lines = re.findall(r"🧬 \[novelty metrics\] (\{.*\})", text)
    if novelty_lines:
        try:
            novelty = json.loads(novelty_lines[-1])
        except json.JSONDecodeError:
            novelty = {"parse_error": True}
    return {
        "iterations": iterations,
        "tool_calls": len(tool_call_lines),
        "duplicate_tool_calls": duplicate_tool_calls,
        "done": done,
        "failure_streak_nudges": failure_streak_nudges,
        "novelty": novelty,
    }


def _classify_failure(passed, metrics):
    """`iterations >= tool_calls` (the original heuristic here) is true for
    almost any failed run — any run averaging <=1 tool call per iteration,
    which is the common case — so it labeled nearly everything
    "no_recovery_loop" regardless of actual cause. Using the failure-streak
    nudge count instead: that's a real, specific signal (agent.py only logs
    it when 2+ consecutive tool calls actually failed), not a coincidence
    of call-counting arithmetic."""
    if passed:
        return "none"
    if metrics["done"]:
        return "false_positive_completion"
    if metrics["duplicate_tool_calls"] >= 2:
        return "redundant_call_spiral"
    if metrics.get("failure_streak_nudges", 0) >= 1:
        return "no_recovery_loop"
    return "other"


def run_once(task_name: str, run_index: int, label: str = "baseline", novelty_context: bool = False):
    task = TASKS[task_name]
    start = time.time()

    if task["fixture"]:
        fixture_src = os.path.join(FIXTURES_DIR, task["fixture"])
        project_dir = tempfile.mkdtemp(prefix=f"bench_{task_name}_")
        for f in os.listdir(fixture_src):
            shutil.copy(os.path.join(fixture_src, f), project_dir)
    else:
        project_dir = REPO_ROOT  # propose_tool runs against the live repo, on purpose

    # NOTE: the reverted baseline's agent.py only supports --project — the
    # --only-write allowlist and --budget flags were part of the parked
    # controller-rebuild work and aren't available here. See run_once's
    # caller for the safety implication this has for live-repo tasks.
    cmd = [
        sys.executable, os.path.join(REPO_ROOT, "agent.py"),
        "--project", project_dir,
        task["task_text"],
    ]
    if novelty_context:
        cmd.insert(2, "--novelty-context")
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    elapsed = time.time() - start

    metrics = _parse_output_metrics(proc.stdout)
    passed, detail = task["checker"](project_dir)
    failure_mode = _classify_failure(passed, metrics)

    diff_saved_path = None
    if task["live_repo"]:
        diff_text = subprocess.run(
            ["git", "diff", task["only_write"]], cwd=project_dir, capture_output=True, text=True,
        ).stdout
        if diff_text.strip():
            diff_saved_path = os.path.join(RUNS_DIR, f"{task_name}_{label}_run{run_index}_{int(time.time())}.diff")
            with open(diff_saved_path, "w", encoding="utf-8") as f:
                f.write(diff_text)

    record = {
        "label": label, "task": task_name, "run_index": run_index,
        "passed": passed, "detail": detail, "failure_mode": failure_mode,
        "elapsed_seconds": round(elapsed, 1), "diff_saved_path": diff_saved_path,
        **metrics,
    }

    with open(os.path.join(RUNS_DIR, "results.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    if task["fixture"]:
        shutil.rmtree(project_dir, ignore_errors=True)
    elif task["live_repo"]:
        # Diff is already saved above regardless of pass/fail — always revert
        # so the next repeat starts from the same clean baseline state.
        subprocess.run(["git", "checkout", "--", task["only_write"]], cwd=project_dir)

    print(f"[{label}] {task_name} run {run_index}: {'PASS' if passed else 'FAIL'} "
          f"({failure_mode}) — {detail} — {elapsed:.0f}s, {metrics['iterations']} iterations")
    return record


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in TASKS:
        print(f"Usage: python3 benchmark.py <{'|'.join(TASKS)}> [n_runs] [label] [--novelty-context]")
        raise SystemExit(1)

    task_name = sys.argv[1]
    n_runs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    label = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else "baseline"
    novelty_context = "--novelty-context" in sys.argv[2:]

    for i in range(1, n_runs + 1):
        run_once(task_name, i, label, novelty_context=novelty_context)
