"""Self-test for Phase 5 (episodes, retrieval, memory tools) — run
directly: `python3 memory/test_phase5.py`, exit 0 iff every assertion
passes.

create_episode()'s LLM call is stubbed here (memory.summaries.chat
monkeypatched) so the surrounding logic — event-range gathering, fidelity
checking, episode persistence — is tested deterministically without
depending on a live model response; the real call is validated separately
against the actual model (see IMPLEMENTATION_LOG.md).

Covers Phase 5's stated acceptance tests: relevant prior evidence is
retrieved under a bounded budget, irrelevant distractors are filtered,
exact source expansion works, and retrieval does not resurrect stale
pre-edit facts.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory.summaries as summaries_module
from memory.episodes import create_episode, list_episodes
from memory.retrieval import retrieve
from memory.store import RunStore
from memory.tools import make_memory_tools


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeResponse:
    def __init__(self, content):
        self.message = _FakeMessage(content)


def _stub_chat(fixed_reply):
    def fake_chat(**kwargs):
        return _FakeResponse(fixed_reply)
    return fake_chat


def _new_store(tmp_dir):
    os.environ["EVOLUTION_RUNS_ROOT"] = tmp_dir
    return RunStore(
        task_id="phase5-test", task_text="Fix the thing.", model="qwen3.6:35b-mlx",
        model_options={}, project_root="/tmp/fake-project", memory_policy="bounded-structured", iteration_budget=50,
    )


# ---- episodes.py / summaries.py ----

def test_check_fidelity_passes_with_no_paths_mentioned():
    assert summaries_module.check_fidelity("Investigated the issue and confirmed the cause.", "irrelevant raw text")


def test_check_fidelity_passes_when_mentioned_paths_are_real():
    raw = "[evt-000002] read_file({'path': 'utils.py'}) -> ok"
    assert summaries_module.check_fidelity("Read utils.py and confirmed the bug there.", raw)


def test_check_fidelity_fails_on_hallucinated_path():
    raw = "[evt-000002] read_file({'path': 'utils.py'}) -> ok"
    assert not summaries_module.check_fidelity("Read config.yaml and confirmed the bug there.", raw)


def test_create_episode_end_to_end(tmp_dir):
    store = _new_store(tmp_dir)
    original_chat = summaries_module.chat
    summaries_module.chat = _stub_chat("Read utils.py, found the bug, and fixed it there.")
    try:
        store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "utils.py"},
                                result_text="--- utils.py ---\ndef f(): pass")
        from_id = "evt-000001"
        store.record_tool_call(iteration=2, tool_name="patch_file", arguments={"path": "utils.py"},
                                result_text="Wrote 'utils.py' (10 bytes).", post_content="def f(): return 1")
        to_id = "evt-000002"

        episode = create_episode(store.run_dir, "sg-01", "fix utils.py", "f() returns 1", "done",
                                  from_id, to_id, model="qwen3.6:35b-mlx")
        assert episode["fidelity_ok"] is True
        assert episode["summary"] == "Read utils.py, found the bug, and fixed it there."

        reloaded = list_episodes(store.run_dir)
        assert len(reloaded) == 1 and reloaded[0]["subgoal_id"] == "sg-01"
    finally:
        summaries_module.chat = original_chat


def test_create_episode_flags_hallucinated_summary(tmp_dir):
    store = _new_store(tmp_dir)
    original_chat = summaries_module.chat
    summaries_module.chat = _stub_chat("Read totally_unrelated_file.py and fixed the issue there.")
    try:
        store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "utils.py"},
                                result_text="--- utils.py ---\nok")
        episode = create_episode(store.run_dir, "sg-01", "g", "c", "done",
                                  "evt-000001", "evt-000001", model="qwen3.6:35b-mlx")
        assert episode["fidelity_ok"] is False
    finally:
        summaries_module.chat = original_chat


# ---- retrieval.py ----

def _seed_episode(store, subgoal_id, goal, summary, fidelity_ok=True):
    from memory.episodes import episodes_dir
    import json
    path = os.path.join(episodes_dir(store.run_dir), f"{subgoal_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"subgoal_id": subgoal_id, "goal": goal, "summary": summary,
                   "fidelity_ok": fidelity_ok, "conclusion": "", "success_condition": "",
                   "from_event_id": "evt-000001", "to_event_id": "evt-000001"}, f)


def test_retrieve_ranks_relevant_results_and_ignores_distractors(tmp_dir):
    store = _new_store(tmp_dir)
    _seed_episode(store, "sg-01", "fix annotation parsing", "Fixed get_annotation in utils.py")
    store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "unrelated_networking.py"},
                            result_text="ok")
    results = retrieve(store.run_dir, "annotation parsing utils", max_tokens=1000)
    assert any("annotation" in r["text"].lower() or "utils" in r["text"].lower() for r in results)
    assert not any("networking" in r["text"].lower() for r in results), "irrelevant distractor was not filtered"


def test_retrieve_respects_token_budget(tmp_dir):
    store = _new_store(tmp_dir)
    for i in range(20):
        _seed_episode(store, f"sg-{i:02d}", "widget task", f"Did widget work number {i} on widget.py")
    results = retrieve(store.run_dir, "widget", max_tokens=20)
    from context.budget import estimate_tokens
    total = sum(estimate_tokens(r["text"]) for r in results)
    assert total <= 20
    assert len(results) < 20, "a tiny budget must not return every candidate"


def test_retrieve_excludes_stale_entities(tmp_dir):
    store = _new_store(tmp_dir)
    store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "config.py"}, result_text="ok")
    store.record_tool_call(iteration=2, tool_name="write_file", arguments={"path": "config.py", "content": "x"},
                            result_text="Wrote 'config.py' (1 bytes).", post_content="x")
    results = retrieve(store.run_dir, "config", max_tokens=1000)
    stale_hits = [r for r in results if r["kind"] == "entity" and "STALE" in r["text"]]
    assert stale_hits == [], "a stale pre-edit fact must not be resurrected by retrieval"
    assert any(r["kind"] == "entity" and "config.py" in r["text"] for r in results), "the current (non-stale) fact should still be found"


# ---- tools.py ----

def test_memory_expand_accepts_event_id_and_artifact_id(tmp_dir):
    store = _new_store(tmp_dir)
    record = store.record_tool_call(iteration=1, tool_name="read_file", arguments={"path": "a.py"},
                                     result_text="full content of a.py, longer than any preview")
    _, _, memory_expand = make_memory_tools(store)

    by_event = memory_expand(ref=record["event_id"])
    assert by_event == "full content of a.py, longer than any preview"

    by_artifact = memory_expand(ref=record["artifact_id"])
    assert by_artifact == by_event

    missing = memory_expand(ref="evt-999999")
    assert missing.startswith("ERROR")


def test_memory_status_reports_phase_and_ledgers(tmp_dir):
    store = _new_store(tmp_dir)
    memory_status, _, _ = make_memory_tools(store)
    status_before = memory_status()
    assert "Phase: orient" in status_before

    from controller.subgoals import make_subgoal_tools
    create, _ = make_subgoal_tools(store, model="qwen3.6:35b-mlx")
    result = create(goal="investigate", success_condition="root cause named")
    store.record_tool_call(iteration=1, tool_name="subgoal_create",
                            arguments={"goal": "investigate", "success_condition": "root cause named"},
                            result_text=result)
    status_after = memory_status()
    assert "sg-01" in status_after and "[open]" in status_after


def _run_self_test():
    test_check_fidelity_passes_with_no_paths_mentioned()
    print("OK   test_check_fidelity_passes_with_no_paths_mentioned")
    test_check_fidelity_passes_when_mentioned_paths_are_real()
    print("OK   test_check_fidelity_passes_when_mentioned_paths_are_real")
    test_check_fidelity_fails_on_hallucinated_path()
    print("OK   test_check_fidelity_fails_on_hallucinated_path")

    tests_needing_tmp_dir = [
        test_create_episode_end_to_end,
        test_create_episode_flags_hallucinated_summary,
        test_retrieve_ranks_relevant_results_and_ignores_distractors,
        test_retrieve_respects_token_budget,
        test_retrieve_excludes_stale_entities,
        test_memory_expand_accepts_event_id_and_artifact_id,
        test_memory_status_reports_phase_and_ledgers,
    ]
    for test_fn in tests_needing_tmp_dir:
        tmp_dir = tempfile.mkdtemp(prefix="phase5_test_")
        try:
            test_fn(tmp_dir)
            print(f"OK   {test_fn.__name__}")
        finally:
            os.environ.pop("EVOLUTION_RUNS_ROOT", None)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print("\nAll Phase 5 self-tests passed.")


if __name__ == "__main__":
    _run_self_test()
