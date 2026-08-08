"""Deterministic reducers — fold an event stream into structured state.

Pure functions over memory.events.read_events() (plus the run's own
artifacts/ directory for full tool-result text, which counts as "the
event log" broadly — it's the run's own recorded data, not live external
state). Never touches the project's filesystem or a subprocess: state
must always be reconstructable from a run directory alone, even one whose
underlying checkout no longer exists. "Before" content for a changed
entity is whatever this run last actually observed for that path (a
prior read_file result, or a prior write's captured post-content) — not
a live disk read.

Deliberately does NOT include: hypothesis/evidence-claim extraction (no
mechanism yet produces a claim — that's an explicit model tool call in
Phase 5, or a controller in Phase 4; extracting "claims" by parsing free
prose here would be exactly the evidence-not-prose violation design
principle 6 warns about) or phase/subgoal state (Phase 4's controller,
which doesn't exist yet). This only derives what's mechanically knowable
from tool calls already being recorded: what was read, what was written,
what tests/commands ran, and how they failed.

Entity tracking is scoped to tools with one unambiguous target path
(read_file, search_file, write_file, patch_file) — not grep_dir/list_dir/
list_symbols/git_status/git_diff, which operate over a whole directory,
not one entity. Those still appear in the raw event log; they're just not
folded into inspected_entities/changed_entities here.
"""

import os
import re

from memory.artifacts import load as load_artifact
from memory.events import read_events

ENTITY_READ_TOOLS = {"read_file", "search_file"}
ENTITY_WRITE_TOOLS = {"write_file", "patch_file"}
GIT_TOOLS = {"git_status", "git_diff"}

# Structured failure taxonomy (plan's Phase 2 deliverable). Heuristic,
# pattern-based classification of run_shell/run_tests output — not a real
# per-framework parser. Documented as heuristic rather than exact on
# purpose: a wrong classification here is a worse failure mode than an
# honest "unknown_failure" would be.
FAILURE_TAXONOMY = (
    "product_failure",
    "test_environment_failure",
    "patch_application_failure",
    "missing_dependency_failure",
    "timeout_or_resource_failure",
    "unknown_failure",
)


def _entity_path(tool_name: str, arguments: dict):
    if tool_name == "search_file":
        return arguments.get("filepath")
    return arguments.get("path")


def _classify_shell_failure(exit_code, stdout: str, stderr: str):
    if exit_code is None:
        return "timeout_or_resource_failure"
    if exit_code == 0:
        return None
    text = f"{stdout}\n{stderr}"
    if re.search(r"ModuleNotFoundError|ImportError|No module named", text):
        return "missing_dependency_failure"
    if re.search(r"SyntaxError|IndentationError", text):
        return "test_environment_failure"
    if re.search(r"AssertionError|FAILED|Traceback", text):
        return "product_failure"
    return "unknown_failure"


def _parse_run_shell_result(full_text: str):
    m = re.search(r"Exit code: (-?\d+)", full_text)
    exit_code = int(m.group(1)) if m else None
    if full_text.startswith("TIMEOUT after"):
        exit_code = None
    stdout_m = re.search(r"STDOUT:\n(.*?)\nSTDERR:\n", full_text, re.DOTALL)
    stderr_m = re.search(r"STDERR:\n(.*)$", full_text, re.DOTALL)
    # A self-generated stress test caught this: unlike stdout (bounded by
    # the \nSTDERR:\n that follows it, so its own trailing newline is never
    # part of the match), stderr runs to the literal end of the string —
    # kernel/exec_tools.py's run_shell always ends its output with a
    # newline after stderr content, which without rstrip ends up INSIDE
    # the captured group (e.g. "error\n" instead of "error").
    stderr = stderr_m.group(1).rstrip("\n") if stderr_m else ""
    return exit_code, (stdout_m.group(1) if stdout_m else ""), stderr


def _parse_run_tests_result(full_text: str):
    # dispatch.py stringifies run_tests's (bool, str) return as a tuple
    # repr, e.g. "(True, 'Ran 5 tests: 4 passed, 1 failed, 0 errors')" — but
    # the counts alone already determine success (matches
    # workspace/run_tests_tool.py's own definition), so the repr's bool
    # isn't even needed.
    m = re.search(r"Ran (\d+) tests: (\d+) passed, (\d+) failed, (\d+) errors", full_text)
    if not m:
        return None
    total, passed, failed, errors = (int(x) for x in m.groups())
    return {"total": total, "passed": passed, "failed": failed, "errors": errors,
            "success": failed == 0 and errors == 0 and total > 0}


def reduce_state(run_dir: str) -> dict:
    artifacts_dir = os.path.join(run_dir, "artifacts")

    def full_text(record):
        if not record.get("artifact_id"):
            return record["payload"].get("result_preview", "")
        return load_artifact(artifacts_dir, record["artifact_id"])

    inspected = {}
    changed = {}
    test_runs = []
    shell_runs = []
    git_calls = []
    failures = []

    for record in read_events(run_dir):
        if record.get("event_type") != "tool_call":
            continue
        payload = record["payload"]
        tool_name = payload["tool_name"]
        args = payload.get("arguments", {})
        result_preview = payload.get("result_preview", "")
        succeeded = not result_preview.startswith(("ERROR", "REJECTED"))
        event_id, iteration = record["event_id"], record.get("iteration")

        if tool_name in ENTITY_READ_TOOLS:
            path = _entity_path(tool_name, args)
            if path:
                inspected[path] = {
                    "path": path, "tool": tool_name, "observed_at_event_id": event_id,
                    "iteration": iteration, "artifact_id": record.get("artifact_id"), "stale": False,
                }

        elif tool_name in ENTITY_WRITE_TOOLS:
            path = _entity_path(tool_name, args)
            if not path:
                continue
            if not succeeded:
                failures.append({
                    "event_id": event_id, "iteration": iteration, "tool": tool_name, "path": path,
                    "taxonomy": "patch_application_failure", "detail": result_preview[:300],
                })
                continue
            # Stale-evidence invalidation (Phase 2 acceptance test: editing
            # a file invalidates prior file facts) — any earlier read or
            # write of this same path no longer reflects current content.
            if path in inspected:
                inspected[path]["stale"] = True
            if path in changed:
                changed[path]["stale"] = True
            changed[path] = {
                "path": path, "tool": tool_name, "changed_at_event_id": event_id, "iteration": iteration,
                "post_content_artifact_id": payload.get("post_content_artifact_id"), "stale": False,
            }

        elif tool_name == "run_tests":
            parsed = _parse_run_tests_result(full_text(record))
            test_runs.append({"event_id": event_id, "iteration": iteration,
                               "path": args.get("path"), "parsed": parsed})
            if parsed is not None and not parsed["success"]:
                failures.append({
                    "event_id": event_id, "iteration": iteration, "tool": tool_name,
                    "path": args.get("path"), "taxonomy": "product_failure", "detail": result_preview[:300],
                })

        elif tool_name == "run_shell":
            exit_code, stdout, stderr = _parse_run_shell_result(full_text(record))
            shell_runs.append({"event_id": event_id, "iteration": iteration,
                                "command": args.get("command"), "exit_code": exit_code})
            taxonomy = _classify_shell_failure(exit_code, stdout, stderr)
            if taxonomy:
                failures.append({
                    "event_id": event_id, "iteration": iteration, "tool": tool_name,
                    "command": args.get("command"), "taxonomy": taxonomy, "detail": result_preview[:300],
                })

        elif tool_name in GIT_TOOLS:
            git_calls.append({"event_id": event_id, "iteration": iteration, "tool": tool_name,
                               "result_preview": result_preview[:300]})

    return {
        "inspected_entities": list(inspected.values()),
        "changed_entities": list(changed.values()),
        "test_runs": test_runs,
        "shell_runs": shell_runs,
        "git_calls": git_calls,
        "failures": failures,
    }
