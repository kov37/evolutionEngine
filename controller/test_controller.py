"""Self-test for controller/ — run directly:
`python3 controller/test_controller.py`, exit 0 iff every assertion passes.

Covers Phase 4's stated acceptance tests (a no-op trajectory triggers a
useful recovery action; repeated diagnosis without state change does not
advance the phase; a patch cannot be marked complete without diff and
verification checks; a failed test returns control to localization/
patching) plus the subgoal/hypothesis evidence gates and the completion
gate's generic predicates directly.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory.summaries as summaries_module
from controller.completion import evaluate_completion_gate
from controller.hypotheses import make_hypothesis_tools
from controller.phases import derive_phase
from controller.progress import stagnation_nudge
from controller.subgoals import (auto_close_open_subgoals, make_subgoal_tools, open_subgoals_with_progress,
                                  subgoal_ledger)
from memory.episodes import list_episodes
from memory.store import RunStore

TEST_MODEL = "qwen3.6:35b-mlx"


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeResponse:
    def __init__(self, content):
        self.message = _FakeMessage(content)


def _make_subgoal_tools_for_test(store):
    """Since Phase 5, subgoal_complete triggers a real episode-summary LLM
    call on success. That's Phase 5's own concern (covered by
    memory/test_phase5.py) — stubbed here so these tests stay fast and
    deterministic, testing only the evidence gate this file is about."""
    return make_subgoal_tools(store, model=TEST_MODEL)


def _new_store(tmp_dir):
    os.environ["EVOLUTION_RUNS_ROOT"] = tmp_dir
    return RunStore(
        task_id="controller-test", task_text="Fix the thing.", model="qwen3.6:35b-mlx",
        model_options={}, project_root="/tmp/fake-project", memory_policy="append-all", iteration_budget=50,
    )


def _call_and_record(store, iteration, fn, tool_name, kwargs, post_content=None):
    """Simulates what agent.py's dispatch recorder does: call the tool
    function, then record its (tool_name, arguments, result) exactly as
    dispatch_tool_calls would — so ledgers built from the event log see
    the same thing they would in a real run."""
    result = fn(**kwargs)
    store.record_tool_call(iteration=iteration, tool_name=tool_name, arguments=kwargs,
                            result_text=result, post_content=post_content)
    return result


# ---- phases.py ----

def test_derive_phase_progression():
    assert derive_phase({"inspected_entities": [], "changed_entities": [], "test_runs": [], "shell_runs": []}) == "orient"
    assert derive_phase({"inspected_entities": [{"path": "a.py", "iteration": 1}], "changed_entities": [],
                          "test_runs": [], "shell_runs": []}) == "localize"
    assert derive_phase({"inspected_entities": [], "changed_entities": [],
                          "test_runs": [{"iteration": 1, "parsed": None}], "shell_runs": []}) == "reproduce"
    assert derive_phase({"inspected_entities": [], "changed_entities": [{"path": "a.py", "iteration": 2}],
                          "test_runs": [], "shell_runs": []}) == "patch"
    assert derive_phase({
        "inspected_entities": [], "changed_entities": [{"path": "a.py", "iteration": 2}],
        "test_runs": [{"iteration": 3, "parsed": {"success": False}}], "shell_runs": [],
        "failures": [{"iteration": 3, "taxonomy": "product_failure"}],
    }) == "verify"
    assert derive_phase({
        "inspected_entities": [], "changed_entities": [{"path": "a.py", "iteration": 2}],
        "test_runs": [{"iteration": 3, "parsed": {"success": True}}], "shell_runs": [], "failures": [],
    }) == "review"


# ---- completion.py ----

def test_completion_gate_rejects_with_no_changes(tmp_dir):
    store = _new_store(tmp_dir)
    gate = evaluate_completion_gate(store.run_dir)
    assert gate["allowed"] is False
    assert "changed_entities is empty" in gate["reasons"][0]


def test_completion_gate_rejects_without_verification_after_write(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="patch_file", arguments={"path": "a.py"},
                            result_text="Wrote 'a.py' (5 bytes).", post_content="x = 2")
    gate = evaluate_completion_gate(store.run_dir)
    assert gate["allowed"] is False
    assert "no test or command was run" in gate["reasons"][0]


def test_completion_gate_rejects_with_failure_after_write(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="patch_file", arguments={"path": "a.py"},
                            result_text="Wrote 'a.py' (5 bytes).", post_content="x = 2")
    store.record_tool_call(iteration=2, tool_name="run_tests", arguments={"path": "."},
                            result_text="(False, 'Ran 3 tests: 2 passed, 1 failed, 0 errors')")
    gate = evaluate_completion_gate(store.run_dir)
    assert gate["allowed"] is False
    assert "most recent verification" in gate["reasons"][0]


def test_completion_gate_ignores_superseded_earlier_failure(tmp_dir):
    # Regression test for a real bug a live run against the actual model
    # surfaced: it tried `python` (not found, exit 127), then correctly
    # switched to `python3` and passed. The gate must judge on the MOST
    # RECENT verification, not "did anything ever fail since the write" —
    # otherwise a single self-corrected command typo blocks completion
    # forever, exactly the paralysis trap the plan doc warns against.
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="patch_file", arguments={"path": "a.py"},
                            result_text="Wrote 'a.py' (5 bytes).", post_content="x = 2")
    store.record_tool_call(
        iteration=2, tool_name="run_shell", arguments={"command": "python -c 'x'"},
        result_text="Exit code: 127\nSTDOUT:\n\nSTDERR:\n/bin/sh: python: command not found",
    )
    store.record_tool_call(
        iteration=3, tool_name="run_shell", arguments={"command": "python3 -c 'x'"},
        result_text="Exit code: 0\nSTDOUT:\nok\nSTDERR:\n",
    )
    store.record_tool_call(iteration=4, tool_name="git_diff", arguments={"root": "."}, result_text="diff --git ...")
    gate = evaluate_completion_gate(store.run_dir)
    assert gate["allowed"] is True, f"an earlier, superseded failure must not block completion: {gate['reasons']}"
    assert gate["outcome"] == "unverified"


def test_completion_gate_rejects_failed_diff_review_call(tmp_dir):
    # Regression test for a second real gap the same live run surfaced:
    # git_diff was called against a non-git directory, failed outright,
    # and an earlier (unfixed) version of this check still counted the
    # attempt as review performed. A failed call must not satisfy it.
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="patch_file", arguments={"path": "a.py"},
                            result_text="Wrote 'a.py' (5 bytes).", post_content="x = 2")
    store.record_tool_call(iteration=2, tool_name="run_tests", arguments={"path": "."},
                            result_text="(True, 'Ran 3 tests: 3 passed, 0 failed, 0 errors')")
    store.record_tool_call(iteration=3, tool_name="git_diff", arguments={"root": "."},
                            result_text="ERROR: '.' is not inside a git working tree")
    gate = evaluate_completion_gate(store.run_dir)
    assert gate["allowed"] is False
    assert "hasn't been reviewed" in gate["reasons"][0]


def test_completion_gate_accepts_reread_as_diff_review_substitute(tmp_dir):
    # The generic (non-git) substitute for diff review: re-reading every
    # changed file after its write is accepted, since diff_files needs a
    # second file to compare against and git_diff doesn't work outside a
    # git repo — without this, the predicate would be unsatisfiable for a
    # plain project.
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="patch_file", arguments={"path": "a.py"},
                            result_text="Wrote 'a.py' (5 bytes).", post_content="x = 2")
    store.record_tool_call(iteration=2, tool_name="run_tests", arguments={"path": "."},
                            result_text="(True, 'Ran 3 tests: 3 passed, 0 failed, 0 errors')")
    store.record_tool_call(iteration=3, tool_name="read_file", arguments={"path": "a.py"},
                            result_text="--- a.py ---\nx = 2")
    gate = evaluate_completion_gate(store.run_dir)
    assert gate["allowed"] is True, gate["reasons"]
    assert gate["outcome"] == "unverified"


def test_completion_gate_rejects_without_diff_review(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="patch_file", arguments={"path": "a.py"},
                            result_text="Wrote 'a.py' (5 bytes).", post_content="x = 2")
    store.record_tool_call(iteration=2, tool_name="run_tests", arguments={"path": "."},
                            result_text="(True, 'Ran 3 tests: 3 passed, 0 failed, 0 errors')")
    gate = evaluate_completion_gate(store.run_dir)
    assert gate["allowed"] is False
    assert "final diff hasn't been reviewed" in gate["reasons"][0]


def test_completion_gate_allows_and_labels_unverified(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="patch_file", arguments={"path": "a.py"},
                            result_text="Wrote 'a.py' (5 bytes).", post_content="x = 2")
    store.record_tool_call(iteration=2, tool_name="run_tests", arguments={"path": "."},
                            result_text="(True, 'Ran 3 tests: 3 passed, 0 failed, 0 errors')")
    store.record_tool_call(iteration=3, tool_name="git_diff", arguments={"root": "."}, result_text="diff --git ...")
    gate = evaluate_completion_gate(store.run_dir)
    assert gate["allowed"] is True
    assert gate["outcome"] == "unverified", "no external verifier exists yet — must never claim resolved"


def test_completion_gate_rejects_forbidden_path(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="patch_file", arguments={"path": "tests/test_a.py"},
                            result_text="Wrote 'tests/test_a.py' (5 bytes).", post_content="x = 2")
    store.record_tool_call(iteration=2, tool_name="run_tests", arguments={"path": "."},
                            result_text="(True, 'Ran 3 tests: 3 passed, 0 failed, 0 errors')")
    store.record_tool_call(iteration=3, tool_name="git_diff", arguments={"root": "."}, result_text="diff --git ...")
    gate = evaluate_completion_gate(store.run_dir, forbidden_paths=["tests/*"])
    assert gate["allowed"] is False
    assert "forbidden path" in gate["reasons"][0]


# ---- subgoals.py ----

def test_subgoal_complete_rejected_without_progress(tmp_dir):
    store = _new_store(tmp_dir)
    create, complete = _make_subgoal_tools_for_test(store)

    _call_and_record(store, 1, create, "subgoal_create", {"goal": "find the bug", "success_condition": "root cause named"})
    result = _call_and_record(store, 2, complete, "subgoal_complete",
                               {"subgoal_id": "sg-01", "conclusion": "I think it's in utils.py", "evidence_ids": ""})
    assert result.startswith("REJECTED"), "no real progress happened between create and complete — must reject"


def test_subgoal_complete_allowed_after_real_progress(tmp_dir):
    store = _new_store(tmp_dir)
    create, complete = _make_subgoal_tools_for_test(store)

    _call_and_record(store, 1, create, "subgoal_create", {"goal": "find the bug", "success_condition": "root cause named"})
    store.record_tool_call(iteration=2, tool_name="read_file", arguments={"path": "utils.py"},
                            result_text="--- utils.py ---\ndef f(): pass")
    result = _call_and_record(store, 3, complete, "subgoal_complete",
                               {"subgoal_id": "sg-01", "conclusion": "found it in utils.py", "evidence_ids": ""})
    assert result.startswith("'sg-01' marked complete")


def test_subgoal_complete_rejects_unknown_and_double_completion(tmp_dir):
    store = _new_store(tmp_dir)
    create, complete = _make_subgoal_tools_for_test(store)

    result = complete(subgoal_id="sg-99", conclusion="x", evidence_ids="")
    assert result.startswith("ERROR: unknown subgoal_id")

    _call_and_record(store, 1, create, "subgoal_create", {"goal": "g", "success_condition": "c"})
    store.record_tool_call(iteration=2, tool_name="read_file", arguments={"path": "a.py"}, result_text="ok")
    _call_and_record(store, 3, complete, "subgoal_complete", {"subgoal_id": "sg-01", "conclusion": "done", "evidence_ids": ""})
    second = complete(subgoal_id="sg-01", conclusion="again", evidence_ids="")
    assert "already marked complete" in second


def test_ledger_ignores_a_failed_subgoal_complete_call(tmp_dir):
    # Regression test for a real bug a live run surfaced: a
    # subgoal_complete call with a bad extra kwarg fails with a TypeError
    # (caught by dispatch.py) before the function body runs, but the
    # event still records subgoal_id in its arguments. The ledger must
    # not treat that as a real completion, or a later VALID completion
    # attempt gets wrongly rejected as "already marked complete" and the
    # subgoal never gets a real episode.
    store = _new_store(tmp_dir)
    create, complete = _make_subgoal_tools_for_test(store)
    _call_and_record(store, 1, create, "subgoal_create", {"goal": "g", "success_condition": "c"})
    store.record_tool_call(iteration=2, tool_name="read_file", arguments={"path": "a.py"}, result_text="ok")
    # Simulates dispatch.py's own TypeError handling for a bad-kwarg call —
    # the event log shape produced when subgoal_complete(**bad_args) fails.
    store.record_tool_call(
        iteration=3, tool_name="subgoal_complete", arguments={"subgoal_id": "sg-01", "goal": "unexpected extra arg"},
        result_text="ERROR: bad arguments for subgoal_complete: subgoal_complete() got an unexpected keyword argument 'goal'",
    )
    assert subgoal_ledger(store.run_dir)["sg-01"]["status"] == "open", "a failed call must not mark it completed"

    result = _call_and_record(store, 4, complete, "subgoal_complete",
                               {"subgoal_id": "sg-01", "conclusion": "done properly", "evidence_ids": ""})
    assert result.startswith("'sg-01' marked complete"), "the real, valid completion attempt must succeed"
    assert len(list_episodes(store.run_dir)) == 1


def test_subgoal_ids_are_sequential(tmp_dir):
    store = _new_store(tmp_dir)
    create, _ = _make_subgoal_tools_for_test(store)
    r1 = _call_and_record(store, 1, create, "subgoal_create", {"goal": "a", "success_condition": "x"})
    r2 = _call_and_record(store, 2, create, "subgoal_create", {"goal": "b", "success_condition": "y"})
    assert "sg-01" in r1 and "sg-02" in r2


def test_open_subgoals_with_progress_excludes_untouched_subgoals(tmp_dir):
    store = _new_store(tmp_dir)
    create, _ = _make_subgoal_tools_for_test(store)
    _call_and_record(store, 1, create, "subgoal_create", {"goal": "a", "success_condition": "x"})
    # Nothing has happened since sg-01 was created — not eligible yet.
    assert open_subgoals_with_progress(store.run_dir) == {}

    store.record_tool_call(iteration=2, tool_name="read_file", arguments={"path": "a.py"}, result_text="ok")
    assert list(open_subgoals_with_progress(store.run_dir).keys()) == ["sg-01"]

    # sg-02 is created AFTER that read — nothing has happened since ITS
    # creation, so it must not inherit sg-01's earlier progress. A single
    # global progress event only counts for subgoals that PREDATE it.
    _call_and_record(store, 3, create, "subgoal_create", {"goal": "b", "success_condition": "y"})
    with_progress = open_subgoals_with_progress(store.run_dir)
    assert list(with_progress.keys()) == ["sg-01"], "sg-02 has no progress since ITS creation and must not qualify"


def test_auto_close_open_subgoals_closes_only_ones_with_progress(tmp_dir):
    store = _new_store(tmp_dir)
    create, _ = _make_subgoal_tools_for_test(store)
    _call_and_record(store, 1, create, "subgoal_create", {"goal": "a", "success_condition": "x"})
    store.record_tool_call(iteration=2, tool_name="read_file", arguments={"path": "a.py"}, result_text="ok")
    # sg-02 created after the only progress event so far — has none of its own.
    _call_and_record(store, 3, create, "subgoal_create", {"goal": "b", "success_condition": "y"})

    closed = auto_close_open_subgoals(store, TEST_MODEL, iteration=4, reason="test transition")
    assert closed == ["sg-01"], "sg-02 has no progress yet — must stay open, not force-closed into a garbage episode"

    ledger = subgoal_ledger(store.run_dir)
    assert ledger["sg-01"]["status"] == "completed" and ledger["sg-01"]["auto_closed"] is True
    assert ledger["sg-02"]["status"] == "open"

    episodes = list_episodes(store.run_dir)
    assert len(episodes) == 1 and episodes[0]["subgoal_id"] == "sg-01" and episodes[0]["auto_closed"] is True


def test_auto_close_open_subgoals_is_idempotent(tmp_dir):
    store = _new_store(tmp_dir)
    create, _ = _make_subgoal_tools_for_test(store)
    _call_and_record(store, 1, create, "subgoal_create", {"goal": "a", "success_condition": "x"})
    store.record_tool_call(iteration=2, tool_name="read_file", arguments={"path": "a.py"}, result_text="ok")

    first = auto_close_open_subgoals(store, TEST_MODEL, iteration=3, reason="t1")
    second = auto_close_open_subgoals(store, TEST_MODEL, iteration=4, reason="t2")
    assert first == ["sg-01"]
    assert second == [], "an already-completed subgoal must not be closed again"


# ---- hypotheses.py ----

def test_hypothesis_resolve_requires_real_postdating_evidence(tmp_dir):
    store = _new_store(tmp_dir)
    record, resolve = make_hypothesis_tools(store)

    r = _call_and_record(store, 1, record, "hypothesis_record",
                          {"claim": "cwd bug", "prediction": "fixing it passes the test", "falsifier": "test still fails"})
    assert "hyp-01" in r

    # Citing a made-up event_id must fail.
    bad = resolve(hypothesis_id="hyp-01", status="prediction_observed", evidence_id="evt-999999")
    assert bad.startswith("ERROR: evidence_id")

    # Citing the hypothesis's own creation event (or anything before it) must fail.
    self_cite = resolve(hypothesis_id="hyp-01", status="prediction_observed", evidence_id="evt-000001")
    assert self_cite.startswith("ERROR:") and "predates" in self_cite

    # A real, later event succeeds.
    store.record_tool_call(iteration=2, tool_name="run_tests", arguments={"path": "."},
                            result_text="(True, 'Ran 1 tests: 1 passed, 0 failed, 0 errors')")
    real_event_id = "evt-000002"
    ok = resolve(hypothesis_id="hyp-01", status="prediction_observed", evidence_id=real_event_id)
    assert ok.startswith("'hyp-01' resolved: prediction_observed")


def test_hypothesis_resolve_rejects_bad_status(tmp_dir):
    store = _new_store(tmp_dir)
    record, resolve = make_hypothesis_tools(store)
    _call_and_record(store, 1, record, "hypothesis_record", {"claim": "c", "prediction": "p", "falsifier": "f"})
    result = resolve(hypothesis_id="hyp-01", status="confirmed", evidence_id="evt-000001")
    assert result.startswith("ERROR: status must be")


# ---- progress.py ----

def test_stagnation_nudge_fires_after_threshold(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "a.py"}, result_text="ok")
    assert stagnation_nudge(store.run_dir, current_iteration=1) is None
    assert stagnation_nudge(store.run_dir, current_iteration=10) is None
    nudge = stagnation_nudge(store.run_dir, current_iteration=20, threshold=15)
    assert nudge is not None and "turns have passed" in nudge


def test_stagnation_nudge_resets_on_new_progress(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "a.py"}, result_text="ok")
    store.record_tool_call(iteration=18, tool_name="read_file", arguments={"path": "b.py"}, result_text="ok")
    assert stagnation_nudge(store.run_dir, current_iteration=20, threshold=15) is None


def _run_self_test():
    test_derive_phase_progression()
    print("OK   test_derive_phase_progression")

    original_chat = summaries_module.chat
    summaries_module.chat = lambda **kwargs: _FakeResponse("stubbed episode summary")

    tests_needing_tmp_dir = [
        test_completion_gate_rejects_with_no_changes,
        test_completion_gate_rejects_without_verification_after_write,
        test_completion_gate_rejects_with_failure_after_write,
        test_completion_gate_ignores_superseded_earlier_failure,
        test_completion_gate_rejects_failed_diff_review_call,
        test_completion_gate_accepts_reread_as_diff_review_substitute,
        test_completion_gate_rejects_without_diff_review,
        test_completion_gate_allows_and_labels_unverified,
        test_completion_gate_rejects_forbidden_path,
        test_subgoal_complete_rejected_without_progress,
        test_subgoal_complete_allowed_after_real_progress,
        test_subgoal_complete_rejects_unknown_and_double_completion,
        test_ledger_ignores_a_failed_subgoal_complete_call,
        test_subgoal_ids_are_sequential,
        test_open_subgoals_with_progress_excludes_untouched_subgoals,
        test_auto_close_open_subgoals_closes_only_ones_with_progress,
        test_auto_close_open_subgoals_is_idempotent,
        test_hypothesis_resolve_requires_real_postdating_evidence,
        test_hypothesis_resolve_rejects_bad_status,
        test_stagnation_nudge_fires_after_threshold,
        test_stagnation_nudge_resets_on_new_progress,
    ]
    try:
        for test_fn in tests_needing_tmp_dir:
            tmp_dir = tempfile.mkdtemp(prefix="controller_test_")
            try:
                test_fn(tmp_dir)
                print(f"OK   {test_fn.__name__}")
            finally:
                os.environ.pop("EVOLUTION_RUNS_ROOT", None)
                shutil.rmtree(tmp_dir, ignore_errors=True)
    finally:
        summaries_module.chat = original_chat

    print("\nAll controller/ self-tests passed.")


if __name__ == "__main__":
    _run_self_test()
