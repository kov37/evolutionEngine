"""Self-test for the event/artifact store — run directly (no pytest
dependency, matching this project's GRADUATION_CONTRACT convention:
`python3 memory/test_memory.py`, exit 0 iff every assertion passes.

Covers Phase 0's acceptance tests: artifacts retrievable byte-for-byte,
process interruption doesn't corrupt prior events, event_id/parent chains
are valid, and a malformed line doesn't crash reconstruction.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.artifacts import store as store_artifact, load as load_artifact
from memory.events import EventWriter, read_events
from memory.schema import validate_event_record
from memory.store import RunStore, compute_metrics


def test_artifact_roundtrip(tmp_dir):
    artifacts_dir = os.path.join(tmp_dir, "artifacts")
    content = "line one\nline two\n" + ("x" * 10_000)  # exceeds dispatch.py's 4000-char truncation cap
    artifact_id = store_artifact(artifacts_dir, content)
    assert artifact_id.startswith("sha256:")
    recovered = load_artifact(artifacts_dir, artifact_id)
    assert recovered == content, "byte-for-byte round trip failed"

    # Idempotency: storing identical content twice must not create a second file.
    artifact_id_2 = store_artifact(artifacts_dir, content)
    assert artifact_id == artifact_id_2
    assert len(os.listdir(artifacts_dir)) == 1

    # Sub-range retrieval (memory_expand's eventual use case).
    partial = load_artifact(artifacts_dir, artifact_id, offset=5, length=3)
    assert partial == content[5:8]


def test_event_ordering_and_parent_chain(tmp_dir):
    run_dir = os.path.join(tmp_dir, "run_a")
    os.makedirs(run_dir)
    writer = EventWriter(run_dir, run_id="run_a")

    e1 = writer.append("model_call", {"x": 1}, iteration=1)
    e2 = writer.append("tool_call", {"x": 2}, iteration=1)
    e3 = writer.append("tool_call", {"x": 3}, iteration=2)

    assert e1["parent_event_id"] is None
    assert e2["parent_event_id"] == e1["event_id"]
    assert e3["parent_event_id"] == e2["event_id"]
    assert [e1["event_id"], e2["event_id"], e3["event_id"]] == ["evt-000001", "evt-000002", "evt-000003"]

    records = list(read_events(run_dir))
    assert len(records) == 3
    assert [r["event_id"] for r in records] == ["evt-000001", "evt-000002", "evt-000003"]


def test_interruption_recovery(tmp_dir):
    run_dir = os.path.join(tmp_dir, "run_b")
    os.makedirs(run_dir)

    writer_1 = EventWriter(run_dir, run_id="run_b")
    writer_1.append("model_call", {}, iteration=1)
    writer_1.append("tool_call", {"tool_name": "read_file", "arguments": {}, "result_preview": ""}, iteration=1)
    del writer_1  # simulates the process dying here

    # A fresh writer against the same run_dir must resume, not restart.
    writer_2 = EventWriter(run_dir, run_id="run_b")
    assert writer_2._seq == 2
    e3 = writer_2.append("model_call", {}, iteration=2)
    assert e3["event_id"] == "evt-000003"

    records = list(read_events(run_dir))
    assert len(records) == 3, "interruption must not lose or duplicate prior events"
    assert e3["parent_event_id"] == records[1]["event_id"]


def test_corrupt_line_does_not_crash_reconstruction(tmp_dir):
    run_dir = os.path.join(tmp_dir, "run_c")
    os.makedirs(run_dir)
    writer = EventWriter(run_dir, run_id="run_c")
    writer.append("model_call", {}, iteration=1)

    with open(os.path.join(run_dir, "events.jsonl"), "a", encoding="utf-8") as f:
        f.write("not valid json at all\n")

    writer.append("model_call", {}, iteration=2)  # written after the corrupt line

    records = list(read_events(run_dir))
    assert len(records) == 3
    assert records[1]["event_type"] == "corrupt_event"
    assert records[0]["event_type"] == "model_call"
    assert records[2]["event_type"] == "model_call"


def test_schema_validation_rejects_missing_field():
    try:
        validate_event_record({"schema_version": 1, "event_id": "evt-000001"})
        assert False, "expected ValueError for missing required fields"
    except ValueError:
        pass


def test_run_store_end_to_end(tmp_dir):
    os.environ["EVOLUTION_RUNS_ROOT"] = tmp_dir
    try:
        store = RunStore(
            task_id="self-test-task", task_text="Fix the thing.", model="qwen3.6:35b-mlx",
            model_options={"temperature": 1, "presence_penalty": 1.5, "num_ctx": 32768, "think": False},
            project_root="/tmp/fake-project", memory_policy="append-all", iteration_budget=20,
        )
        store.record_model_call(
            iteration=1, prompt_preview="Fix the thing.", response_text="I will look at the code.",
            input_tokens=120, output_tokens=15, latency_ms=800,
        )
        store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "a.py"},
                                result_text="def f(): pass\n" * 2000)  # forces truncation-worthy size
        store.record_tool_call(iteration=2, tool_name="patch_file", arguments={"path": "a.py"},
                                result_text="OK: patched.")
        metrics = store.record_task_finished(iteration=2, outcome="finished", summary="Fixed it.")

        assert os.path.exists(os.path.join(store.run_dir, "run.json"))
        assert os.path.exists(os.path.join(store.run_dir, "events.jsonl"))
        assert os.path.exists(os.path.join(store.run_dir, "metrics.json"))

        with open(os.path.join(store.run_dir, "run.json"), "r", encoding="utf-8") as f:
            run_record = json.load(f)
        assert run_record["outcome"] == "finished"
        assert run_record["model_options"]["presence_penalty"] == 1.5

        assert metrics["model_calls"] == 1
        assert metrics["tool_calls"] == 2
        assert metrics["write_calls"] == 1
        assert metrics["turns_to_first_write"] == 2
        assert metrics["total_input_tokens"] == 120
        assert metrics["total_output_tokens"] == 15

        recomputed = compute_metrics(store.run_dir)
        assert recomputed == metrics, "metrics.json must match a fresh recompute from the event log alone"

        # A failed write attempt must not count as a write — regression test
        # for a real bug the first smoke-test run against the live model
        # surfaced: turns_to_first_write reported the turn of a patch_file
        # call that had actually errored (verbatim search-text mismatch).
        store.record_tool_call(iteration=3, tool_name="patch_file", arguments={"path": "a.py"},
                                result_text="ERROR: search text was not found verbatim in 'a.py'.")
        metrics_after_failed_write = compute_metrics(store.run_dir)
        assert metrics_after_failed_write["write_calls"] == 1, "a failed patch_file must not increment write_calls"
        assert metrics_after_failed_write["turns_to_first_write"] == 2, "first SUCCESSFUL write stays turn 2"

        # The large tool result must be recoverable byte-for-byte even though
        # dispatch.py-style truncation would have capped what the model saw.
        records = [r for r in read_events(store.run_dir) if r["event_type"] == "tool_call"]
        large_result_artifact = records[0]["artifact_id"]
        full = load_artifact(os.path.join(store.run_dir, "artifacts"), large_result_artifact)
        assert full == "def f(): pass\n" * 2000
    finally:
        del os.environ["EVOLUTION_RUNS_ROOT"]


def _run_self_test():
    tests = [
        test_artifact_roundtrip,
        test_event_ordering_and_parent_chain,
        test_interruption_recovery,
        test_corrupt_line_does_not_crash_reconstruction,
        test_run_store_end_to_end,
    ]
    for test_fn in tests:
        tmp_dir = tempfile.mkdtemp(prefix="memory_test_")
        try:
            test_fn(tmp_dir)
            print(f"OK   {test_fn.__name__}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    test_schema_validation_rejects_missing_field()
    print(f"OK   test_schema_validation_rejects_missing_field")

    print("\nAll memory/ self-tests passed.")


if __name__ == "__main__":
    _run_self_test()
