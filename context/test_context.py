"""Self-test for context/ — run directly:
`python3 context/test_context.py`, exit 0 iff every assertion passes.

Covers Phase 3's stated acceptance tests: context remains bounded over
200 synthetic turns, the task contract is never lost, a test failure
survives compaction, and every rendered reference is traceable back to a
real event/artifact.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context.budget import estimate_tokens
from context.compiler import compile_context
from context.policies import group_into_turns
from memory.events import read_events
from memory.store import RunStore


SYSTEM = {"role": "system", "content": "You are a Principal Software Engineer. SYSTEM PROMPT CONTENT."}
TASK = {"role": "user", "content": "Fix the exact bug described in issue #4551, byte for byte this text matters."}


def _assistant(text, tool_calls=None):
    # A plain dict stand-in for an Ollama Message object — policies.py's
    # _role() handles both; a dict is simpler to construct for a synthetic
    # test and behaves identically for every code path exercised here.
    return {"role": "assistant", "content": text, "tool_calls": tool_calls or []}


def _tool(name, content):
    return {"role": "tool", "tool_name": name, "content": content}


def test_append_all_is_unchanged_baseline():
    tail = [_assistant("a"), _tool("read_file", "x"), _assistant("b"), _tool("read_file", "y")]
    messages = [SYSTEM, TASK] + tail
    compiled = compile_context("append-all", messages, run_dir=None)
    assert compiled == messages, "append-all must be byte-identical to the full messages list"


def test_group_into_turns_keeps_pairs_intact():
    tail = [
        _assistant("t1"), _tool("read_file", "a"), _tool("read_file", "b"),
        _assistant("t2"), _tool("patch_file", "c"),
        _assistant("t3"),
    ]
    blocks = group_into_turns(tail)
    assert len(blocks) == 3
    assert [m["content"] for m in blocks[0]] == ["t1", "a", "b"]
    assert [m["content"] for m in blocks[1]] == ["t2", "c"]
    assert [m["content"] for m in blocks[2]] == ["t3"]


def test_sliding_window_never_splits_a_turn():
    tail = []
    for i in range(10):
        tail.append(_assistant(f"turn {i}"))
        tail.append(_tool("read_file", f"result {i}"))
    messages = [SYSTEM, TASK] + tail
    compiled = compile_context("sliding-window", messages, run_dir=None)

    assert compiled[0] == SYSTEM and compiled[1] == TASK, "task contract must survive unchanged"
    kept_tail = compiled[2:]
    # Every tool message's immediately preceding message must be an
    # assistant message — never orphaned mid-pair.
    for i, m in enumerate(kept_tail):
        if m["role"] == "tool":
            assert kept_tail[i - 1]["role"] == "assistant"
    # Default window is 6 turns = 12 messages (assistant+tool per turn).
    assert len(kept_tail) == 12


def _build_run_with_a_failure_then_two_hundred_noise_turns(tmp_dir):
    os.environ["EVOLUTION_RUNS_ROOT"] = tmp_dir
    store = RunStore(
        task_id="ctx-test", task_text=TASK["content"], model="qwen3.6:35b-mlx", model_options={},
        project_root="/tmp/fake", memory_policy="bounded-structured", iteration_budget=200,
    )
    # A real, meaningful failure early in the run.
    store.record_tool_call(iteration=3, tool_name="run_tests", arguments={"path": "."},
                            result_text="(False, 'Ran 4 tests: 3 passed, 1 failed, 0 errors')")
    # 200 turns of unrelated noise after it, each a real event.
    messages = [SYSTEM, TASK]
    for i in range(4, 204):
        messages.append(_assistant(f"investigating turn {i}"))
        result = f"content for turn {i} " + ("noise " * 50)
        messages.append(_tool("read_file", result))
        store.record_tool_call(iteration=i, tool_name="read_file", arguments={"path": f"file_{i}.py"},
                                result_text=result)
    return store, messages


def test_bounded_policies_stay_bounded_over_200_turns():
    tmp_dir = tempfile.mkdtemp(prefix="context_test_")
    try:
        store, messages = _build_run_with_a_failure_then_two_hundred_noise_turns(tmp_dir)

        append_all_tokens = estimate_tokens(compile_context("append-all", messages, run_dir=store.run_dir))
        window_tokens = estimate_tokens(compile_context("sliding-window", messages, run_dir=store.run_dir))
        structured_tokens = estimate_tokens(compile_context("bounded-structured", messages, run_dir=store.run_dir))

        assert append_all_tokens > 10_000, "sanity check: 200 turns of real content should be large under append-all"
        assert window_tokens < append_all_tokens / 5, "sliding-window must not grow with total turn count"
        assert structured_tokens < append_all_tokens / 5, "bounded-structured must not grow with total turn count"
    finally:
        os.environ.pop("EVOLUTION_RUNS_ROOT", None)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_task_contract_never_lost_over_200_turns():
    tmp_dir = tempfile.mkdtemp(prefix="context_test_")
    try:
        store, messages = _build_run_with_a_failure_then_two_hundred_noise_turns(tmp_dir)
        for policy in ("append-all", "sliding-window", "bounded-structured"):
            compiled = compile_context(policy, messages, run_dir=store.run_dir)
            assert compiled[0] == SYSTEM, f"{policy}: system prompt must survive"
            assert compiled[1] == TASK, f"{policy}: task text must survive byte-identical"
    finally:
        os.environ.pop("EVOLUTION_RUNS_ROOT", None)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_failure_survives_compaction_past_the_recent_window():
    tmp_dir = tempfile.mkdtemp(prefix="context_test_")
    try:
        store, messages = _build_run_with_a_failure_then_two_hundred_noise_turns(tmp_dir)
        compiled = compile_context("bounded-structured", messages, run_dir=store.run_dir)
        # The turn-3 failure is 200 turns before the recent-tail window
        # (default recent_turns=4) — it must still appear via the
        # structured-state block, not the raw tail.
        state_block = next(m["content"] for m in compiled if isinstance(m, dict) and "Current state" in m.get("content", ""))
        assert "product_failure" in state_block
        assert "1 passed" not in state_block  # sanity: not accidentally matching an unrelated string
        assert "3/4 passed" in state_block
    finally:
        os.environ.pop("EVOLUTION_RUNS_ROOT", None)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_state_block_references_are_traceable_to_real_events():
    tmp_dir = tempfile.mkdtemp(prefix="context_test_")
    try:
        store, messages = _build_run_with_a_failure_then_two_hundred_noise_turns(tmp_dir)
        compiled = compile_context("bounded-structured", messages, run_dir=store.run_dir)
        state_block = next(m["content"] for m in compiled if isinstance(m, dict) and "Current state" in m.get("content", ""))

        import re
        refs = re.findall(r"ref=(evt-\d+)", state_block)
        assert refs, "state block should include at least one traceable event reference"
        real_event_ids = {r["event_id"] for r in read_events(store.run_dir) if r.get("event_type") != "corrupt_event"}
        for ref in refs:
            assert ref in real_event_ids, f"reference '{ref}' in the rendered state does not point to a real event"
    finally:
        os.environ.pop("EVOLUTION_RUNS_ROOT", None)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_self_test():
    tests = [
        test_append_all_is_unchanged_baseline,
        test_group_into_turns_keeps_pairs_intact,
        test_sliding_window_never_splits_a_turn,
        test_bounded_policies_stay_bounded_over_200_turns,
        test_task_contract_never_lost_over_200_turns,
        test_failure_survives_compaction_past_the_recent_window,
        test_state_block_references_are_traceable_to_real_events,
    ]
    for test_fn in tests:
        test_fn()
        print(f"OK   {test_fn.__name__}")
    print("\nAll context/ self-tests passed.")


if __name__ == "__main__":
    _run_self_test()
