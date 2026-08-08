"""RunStore: the integration surface agent.py actually calls.

Owns one .evolution/runs/<run_id>/ directory per run (run.json, events.jsonl,
artifacts/, metrics.json — episodes/ and checkpoints/ aren't created yet,
since nothing populates them until Phase 4/5). Everything here is pure
recording: it never reads TASK_STATE, never changes what's sent to the
model, and has no opinion about phases or subgoals.
"""

import json
import os

from memory.artifacts import store as store_artifact
from memory.events import EventWriter
from memory.schema import make_run_record, new_run_id, now_iso

RUNS_ROOT_ENV = "EVOLUTION_RUNS_ROOT"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def runs_root() -> str:
    return os.environ.get(RUNS_ROOT_ENV) or os.path.join(_REPO_ROOT, ".evolution", "runs")


class RunStore:
    def __init__(self, task_id, task_text, model, model_options, project_root, memory_policy, iteration_budget):
        self.run_id = new_run_id(task_id)
        self.run_dir = os.path.join(runs_root(), self.run_id)
        self.artifacts_dir = os.path.join(self.run_dir, "artifacts")
        os.makedirs(self.run_dir, exist_ok=True)

        task_artifact_id = store_artifact(self.artifacts_dir, task_text)
        self.run_record = make_run_record(
            run_id=self.run_id, task_id=task_id, model=model, model_options=model_options,
            project_root=project_root, memory_policy=memory_policy, iteration_budget=iteration_budget,
            task_artifact_id=task_artifact_id,
        )
        self._write_run_record()
        self.events = EventWriter(self.run_dir, self.run_id)

    def _write_run_record(self):
        path = os.path.join(self.run_dir, "run.json")
        tmp = path + f".tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.run_record, f, indent=2)
        os.replace(tmp, path)

    def record_model_call(self, iteration, prompt_preview, response_text, input_tokens, output_tokens, latency_ms,
                           compiled_context_tokens_estimate=None):
        response_text = response_text or ""
        artifact_id = store_artifact(self.artifacts_dir, response_text) if response_text else None
        payload = {"response_preview": response_text[:500]}
        if compiled_context_tokens_estimate is not None:
            payload["compiled_context_tokens_estimate"] = compiled_context_tokens_estimate
        return self.events.append(
            "model_call", payload=payload, iteration=iteration,
            artifact_id=artifact_id, prompt_preview=(prompt_preview or "")[:500],
            input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms,
        )

    def record_tool_call(self, iteration, tool_name, arguments, result_text, post_content=None):
        artifact_id = store_artifact(self.artifacts_dir, result_text)
        payload = {"tool_name": tool_name, "arguments": arguments, "result_preview": result_text[:500]}
        if post_content is not None:
            payload["post_content_artifact_id"] = store_artifact(self.artifacts_dir, post_content)
        return self.events.append("tool_call", iteration=iteration, artifact_id=artifact_id, payload=payload)

    def record_task_finished(self, iteration, outcome, summary=None):
        self.events.append("task_finished", payload={"outcome": outcome, "summary": summary}, iteration=iteration)
        self.run_record["outcome"] = outcome
        self.run_record["finish_summary"] = summary
        self.run_record["ended_at"] = now_iso()
        self._write_run_record()
        metrics = compute_metrics(self.run_dir)
        with open(os.path.join(self.run_dir, "metrics.json"), "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)

        # Phase 2: materialized state, purely derived from the event log
        # (memory/reducers.py) — imported here, not at module level, so
        # Phase 0/1's store.py has no hard dependency on Phase 2 existing.
        from memory.reducers import reduce_state
        state = reduce_state(self.run_dir)
        with open(os.path.join(self.run_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        return metrics


def compute_metrics(run_dir: str) -> dict:
    """Derives Phase 0's required token/latency/tool/diff metrics purely
    from the event log — usable both at run-finish time and standalone
    (`python3 -m memory.report <run_id>`) against any past run_dir,
    including ones from an interrupted process."""
    from memory.events import read_events  # local import avoids a cycle with the module-level import above

    total_input_tokens = 0
    total_output_tokens = 0
    total_latency_ms = 0
    model_calls = 0
    tool_calls = 0
    tool_call_counts = {}
    write_calls = 0
    max_iteration = 0
    compiled_context_tokens_by_turn = []

    for record in read_events(run_dir):
        if record.get("event_type") == "corrupt_event":
            continue
        max_iteration = max(max_iteration, record.get("iteration") or 0)
        if record["event_type"] == "model_call":
            model_calls += 1
            total_input_tokens += record.get("input_tokens") or 0
            total_output_tokens += record.get("output_tokens") or 0
            total_latency_ms += record.get("latency_ms") or 0
            estimate = record["payload"].get("compiled_context_tokens_estimate")
            if estimate is not None:
                compiled_context_tokens_by_turn.append(estimate)
        elif record["event_type"] == "tool_call":
            tool_calls += 1
            payload = record["payload"]
            name = payload["tool_name"]
            tool_call_counts[name] = tool_call_counts.get(name, 0) + 1
            # Matches agent.py's own wrote_this_turn check: a failed write
            # attempt (ERROR/REJECTED) doesn't count as a write, or
            # turns_to_first_write below would report a turn whose patch_file
            # call actually errored as if it had succeeded.
            succeeded = not payload["result_preview"].startswith(("ERROR", "REJECTED"))
            if name in ("write_file", "patch_file") and succeeded:
                write_calls += 1

    return {
        "model_calls": model_calls,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_latency_ms": total_latency_ms,
        "tool_calls": tool_calls,
        "tool_call_counts": tool_call_counts,
        "write_calls": write_calls,
        "turns_to_first_write": _turns_to_first_write(run_dir),
        "max_iteration": max_iteration,
        # Phase 3's own "context-size metrics" deliverable: the estimator's
        # per-turn view of what was actually sent (not what accumulated in
        # `messages` — that's total_input_tokens above, from Ollama itself).
        # Peak, not just final, matters for policy comparison — a policy
        # that stays flat vs. one that grows then gets trimmed look
        # identical at the last turn but very different across the run.
        "compiled_context_tokens_by_turn": compiled_context_tokens_by_turn,
        "peak_compiled_context_tokens_estimate": max(compiled_context_tokens_by_turn, default=0),
    }


def _turns_to_first_write(run_dir: str) -> int:
    from memory.events import read_events

    for record in read_events(run_dir):
        if record.get("event_type") != "tool_call":
            continue
        payload = record["payload"]
        if payload["tool_name"] in ("write_file", "patch_file") and \
                not payload["result_preview"].startswith(("ERROR", "REJECTED")):
            return record["iteration"]
    return -1
