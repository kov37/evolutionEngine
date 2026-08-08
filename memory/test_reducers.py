"""Self-test for memory/reducers.py — run directly:
`python3 memory/test_reducers.py`, exit 0 iff every assertion passes.

Covers Phase 2's stated acceptance tests: a synthetic trajectory produces
the expected state, editing a file invalidates prior file facts, a failed
test creates a machine-readable failure record, and state can be rebuilt
from events alone (a second reduce_state() call over the same run_dir,
with no live process state, must reproduce the same result).
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.reducers import reduce_state
from memory.store import RunStore


def _new_store(tmp_dir):
    os.environ["EVOLUTION_RUNS_ROOT"] = tmp_dir
    return RunStore(
        task_id="reducer-test", task_text="Fix the thing.", model="qwen3.6:35b-mlx",
        model_options={}, project_root="/tmp/fake-project", memory_policy="append-all", iteration_budget=20,
    )


def test_synthetic_trajectory_produces_expected_state(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "a.py"},
                            result_text="--- a.py (20 chars) ---\ndef f(): return 1")
    store.record_tool_call(iteration=2, tool_name="patch_file", arguments={"path": "a.py"},
                            result_text="Wrote 'a.py' (20 bytes).", post_content="def f(): return 2")
    store.record_tool_call(iteration=3, tool_name="run_tests", arguments={"path": "."},
                            result_text="(True, 'Ran 3 tests: 3 passed, 0 failed, 0 errors')")

    state = reduce_state(store.run_dir)
    assert [e["path"] for e in state["changed_entities"]] == ["a.py"]
    assert state["changed_entities"][0]["post_content_artifact_id"] is not None
    assert len(state["test_runs"]) == 1
    assert state["test_runs"][0]["parsed"]["success"] is True
    assert state["failures"] == []


def test_editing_a_file_invalidates_prior_reads(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "a.py"},
                            result_text="--- a.py (20 chars) ---\ndef f(): return 1")
    state_before_edit = reduce_state(store.run_dir)
    assert state_before_edit["inspected_entities"][0]["stale"] is False

    store.record_tool_call(iteration=2, tool_name="write_file", arguments={"path": "a.py", "content": "..."},
                            result_text="Wrote 'a.py' (9 bytes).", post_content="def f(): return 2")
    state_after_edit = reduce_state(store.run_dir)
    assert state_after_edit["inspected_entities"][0]["stale"] is True, "prior read must be marked stale after a write"
    assert state_after_edit["changed_entities"][0]["stale"] is False, "the write itself is the current fact, not stale"


def test_failed_test_creates_machine_readable_failure(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="run_tests", arguments={"path": "."},
                            result_text="(False, 'Ran 4 tests: 2 passed, 2 failed, 0 errors')")
    state = reduce_state(store.run_dir)
    assert len(state["failures"]) == 1
    assert state["failures"][0]["taxonomy"] == "product_failure"
    assert state["test_runs"][0]["parsed"]["failed"] == 2


def test_shell_failure_taxonomy_classification(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(
        iteration=1, tool_name="run_shell", arguments={"command": "python3 x.py"},
        result_text="Exit code: 1\nSTDOUT:\n\nSTDERR:\nModuleNotFoundError: No module named 'requests'",
    )
    store.record_tool_call(
        iteration=2, tool_name="run_shell", arguments={"command": "python3 y.py"},
        result_text="Exit code: 0\nSTDOUT:\nok\nSTDERR:\n",
    )
    store.record_tool_call(
        iteration=3, tool_name="run_shell", arguments={"command": "sleep 999"},
        result_text="TIMEOUT after 15s — command likely hung.",
    )
    state = reduce_state(store.run_dir)
    taxonomies = [f["taxonomy"] for f in state["failures"]]
    assert "missing_dependency_failure" in taxonomies
    assert "timeout_or_resource_failure" in taxonomies
    assert len(state["shell_runs"]) == 3
    # The exit-0 run must not have produced a failure record.
    assert len(taxonomies) == 2


def test_patch_application_failure_recorded(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(
        iteration=1, tool_name="patch_file", arguments={"path": "a.py"},
        result_text="ERROR: search text was not found verbatim in 'a.py'.",
    )
    state = reduce_state(store.run_dir)
    assert len(state["failures"]) == 1
    assert state["failures"][0]["taxonomy"] == "patch_application_failure"
    assert state["changed_entities"] == [], "a failed write must not register as a changed entity"


def test_state_rebuildable_from_events_alone(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "a.py"},
                            result_text="--- a.py (10 chars) ---\nx = 1")
    store.record_tool_call(iteration=2, tool_name="write_file", arguments={"path": "a.py", "content": "x = 2"},
                            result_text="Wrote 'a.py' (5 bytes).", post_content="x = 2")

    first = reduce_state(store.run_dir)
    # A completely independent call, as if this were a fresh process that
    # only knows the run_dir path — no reference to `store` or any live
    # object is used here.
    second = reduce_state(store.run_dir)
    assert first == second, "reduce_state must be a pure function of the run directory"


def _run_self_test():
    tests = [
        test_synthetic_trajectory_produces_expected_state,
        test_editing_a_file_invalidates_prior_reads,
        test_failed_test_creates_machine_readable_failure,
        test_shell_failure_taxonomy_classification,
        test_patch_application_failure_recorded,
        test_state_rebuildable_from_events_alone,
    ]
    for test_fn in tests:
        tmp_dir = tempfile.mkdtemp(prefix="reducers_test_")
        try:
            test_fn(tmp_dir)
            print(f"OK   {test_fn.__name__}")
        finally:
            os.environ.pop("EVOLUTION_RUNS_ROOT", None)
            shutil.rmtree(tmp_dir, ignore_errors=True)
    print("\nAll memory/reducers.py self-tests passed.")


if __name__ == "__main__":
    _run_self_test()
