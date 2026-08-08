"""Self-test for dispatch.py — run directly:
`python3 test_dispatch.py`, exit 0 iff every assertion passes.

No dedicated test file existed for this before — it's shared, foundational
code (harness.py and agent.py both depend on it) that had never been
tested in isolation. Covers the pre-existing truncation/error behavior
plus the new memory_expand truncation hint, added after a live run got
stuck repeatedly re-fetching a file that kept getting truncated instead
of using the tool built for exactly that.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dispatch import MAX_MESSAGE_CONTENT_CHARS, dispatch_tool_calls


class _FakeCall:
    def __init__(self, name, arguments):
        class _Fn:
            pass
        self.function = _Fn()
        self.function.name = name
        self.function.arguments = arguments


def test_unknown_tool_returns_error_without_raising():
    result = dispatch_tool_calls([_FakeCall("nonexistent_tool", {})], tool_map={})
    assert len(result) == 1
    assert result[0]["content"].startswith("ERROR: unknown tool")


def test_bad_arguments_caught_as_error():
    def fn(a, b):
        return a + b
    result = dispatch_tool_calls([_FakeCall("fn", {"a": 1, "c": 2})], tool_map={"fn": fn})
    assert result[0]["content"].startswith("ERROR: bad arguments")


def test_value_error_becomes_rejected():
    def fn():
        raise ValueError("nope")
    result = dispatch_tool_calls([_FakeCall("fn", {})], tool_map={"fn": fn})
    assert result[0]["content"] == "REJECTED: nope"


def test_unexpected_exception_caught_not_raised():
    def fn():
        raise FileNotFoundError("missing")
    result = dispatch_tool_calls([_FakeCall("fn", {})], tool_map={"fn": fn})
    assert result[0]["content"].startswith("ERROR: unexpected exception in fn: FileNotFoundError")


def test_non_string_result_stringified():
    def fn():
        return (True, "summary")
    result = dispatch_tool_calls([_FakeCall("fn", {})], tool_map={"fn": fn})
    assert result[0]["content"] == "(True, 'summary')"


def test_short_result_not_truncated():
    def fn():
        return "short"
    result = dispatch_tool_calls([_FakeCall("fn", {})], tool_map={"fn": fn})
    assert result[0]["content"] == "short"


def test_long_result_truncated_without_recorder():
    def fn():
        return "x" * (MAX_MESSAGE_CONTENT_CHARS + 500)
    result = dispatch_tool_calls([_FakeCall("fn", {})], tool_map={"fn": fn})
    content = result[0]["content"]
    assert len(content) < MAX_MESSAGE_CONTENT_CHARS + 500
    assert "truncated" in content
    assert "memory_expand" not in content, "no recorder means no event_id, so no expand hint can be given"


def test_long_result_truncation_points_at_memory_expand_with_real_event_id():
    def fn():
        return "y" * (MAX_MESSAGE_CONTENT_CHARS + 500)

    def recorder(tool_name, arguments, full_result):
        assert len(full_result) == MAX_MESSAGE_CONTENT_CHARS + 500, "recorder must see the FULL untruncated result"
        return {"event_id": "evt-000042"}

    result = dispatch_tool_calls([_FakeCall("fn", {})], tool_map={"fn": fn}, recorder=recorder)
    content = result[0]["content"]
    assert "memory_expand(ref='evt-000042')" in content


def test_recorder_without_event_id_gives_no_hint():
    def fn():
        return "z" * (MAX_MESSAGE_CONTENT_CHARS + 100)

    def recorder(tool_name, arguments, full_result):
        return None  # e.g. harness.py-style callers that don't return an event record

    result = dispatch_tool_calls([_FakeCall("fn", {})], tool_map={"fn": fn}, recorder=recorder)
    assert "memory_expand" not in result[0]["content"]


def _run_self_test():
    tests = [
        test_unknown_tool_returns_error_without_raising,
        test_bad_arguments_caught_as_error,
        test_value_error_becomes_rejected,
        test_unexpected_exception_caught_not_raised,
        test_non_string_result_stringified,
        test_short_result_not_truncated,
        test_long_result_truncated_without_recorder,
        test_long_result_truncation_points_at_memory_expand_with_real_event_id,
        test_recorder_without_event_id_gives_no_hint,
    ]
    for test_fn in tests:
        test_fn()
        print(f"OK   {test_fn.__name__}")
    print("\nAll dispatch.py self-tests passed.")


if __name__ == "__main__":
    _run_self_test()
