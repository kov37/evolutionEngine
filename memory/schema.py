"""Versioned record shapes for the event/artifact store.

Only run and event records exist yet. Evidence/hypothesis/subgoal/episode
records (the plan's Phase 2 and Phase 4) are deliberately not added here
until a reducer or controller actually consumes them — an unused field on
a record no one reads is exactly the kind of speculative structure the
plan's own design principles argue against building ahead of need.
"""

import time
import uuid

SCHEMA_VERSION = 1


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_run_id(task_id: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{task_id}-{stamp}-{uuid.uuid4().hex[:8]}"


def new_event_id(seq: int) -> str:
    return f"evt-{seq:06d}"


def make_run_record(run_id, task_id, model, model_options, project_root, memory_policy, iteration_budget,
                     task_artifact_id):
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "model": model,
        "model_options": model_options,
        "project_root": project_root,
        "memory_policy": memory_policy,
        "iteration_budget": iteration_budget,
        "task_artifact_id": task_artifact_id,
        "started_at": now_iso(),
        "ended_at": None,
        "outcome": None,  # "finished" | "budget_exhausted"
        "finish_summary": None,
    }


def make_event_record(event_id, run_id, parent_event_id, iteration, event_type, payload, artifact_id=None,
                       prompt_preview=None, input_tokens=None, output_tokens=None, latency_ms=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "run_id": run_id,
        "parent_event_id": parent_event_id,
        "iteration": iteration,
        "event_type": event_type,  # "model_call" | "tool_call" | "task_finished"
        "timestamp": now_iso(),
        "payload": payload,
        "artifact_id": artifact_id,
        "prompt_preview": prompt_preview,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
    }


REQUIRED_EVENT_FIELDS = ("schema_version", "event_id", "run_id", "event_type", "timestamp")


def validate_event_record(record: dict):
    """Raises ValueError naming the first missing/wrong-typed field, rather
    than letting a malformed record surface as a confusing KeyError deep in
    a reader. Read path (read_events) uses this to skip and report a
    corrupt line instead of crashing the whole run's reconstruction."""
    for field in REQUIRED_EVENT_FIELDS:
        if field not in record:
            raise ValueError(f"event record missing required field '{field}'")
    if record["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"event record schema_version {record['schema_version']} != {SCHEMA_VERSION}")
