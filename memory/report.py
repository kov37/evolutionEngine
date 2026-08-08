"""Reconstruct a run turn-by-turn from its event log, and print its
metrics — Phase 0's acceptance test #1 ("a run can be reconstructed turn
by turn") as a runnable check, not just a claim.

Usage: python3 -m memory.report <run_id> [--expand <event_id>]
"""

import argparse
import json
import os

from memory.artifacts import load as load_artifact
from memory.events import read_events
from memory.store import compute_metrics, runs_root


def _run_dir(run_id: str) -> str:
    return os.path.join(runs_root(), run_id)


def print_trajectory(run_id: str):
    run_dir = _run_dir(run_id)
    run_path = os.path.join(run_dir, "run.json")
    if os.path.exists(run_path):
        with open(run_path, "r", encoding="utf-8") as f:
            run_record = json.load(f)
        print(f"=== run {run_id} ===")
        print(f"task: {run_record['task_id']}  model: {run_record['model']}  "
              f"outcome: {run_record['outcome']}  memory_policy: {run_record['memory_policy']}")
        print()

    for record in read_events(run_dir):
        if record.get("event_type") == "corrupt_event":
            print(f"[CORRUPT event on line {record['line_number']}: {record['error']}]")
            continue
        it = record.get("iteration")
        prefix = f"[turn {it}]" if it is not None else "[-]"
        if record["event_type"] == "model_call":
            print(f"{prefix} model_call ({record['event_id']}, parent={record['parent_event_id']}) "
                  f"in={record['input_tokens']} out={record['output_tokens']} "
                  f"latency_ms={record['latency_ms']}\n  {record['payload']['response_preview'][:200]}")
        elif record["event_type"] == "tool_call":
            p = record["payload"]
            print(f"{prefix} tool_call ({record['event_id']}, parent={record['parent_event_id']}) "
                  f"{p['tool_name']}({p['arguments']})\n  -> {p['result_preview'][:200]}")
        elif record["event_type"] == "task_finished":
            print(f"{prefix} task_finished: {record['payload']}")

    print()
    print("=== metrics ===")
    print(json.dumps(compute_metrics(run_dir), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--expand", help="Print the full artifact for one event_id, not just its preview")
    args = parser.parse_args()

    if args.expand:
        run_dir = _run_dir(args.run_id)
        target = next((r for r in read_events(run_dir) if r.get("event_id") == args.expand), None)
        if target is None or not target.get("artifact_id"):
            raise SystemExit(f"event '{args.expand}' not found or has no artifact")
        print(load_artifact(os.path.join(run_dir, "artifacts"), target["artifact_id"]))
    else:
        print_trajectory(args.run_id)
