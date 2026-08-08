"""Self-test for kernel/io_tools.py — run directly (no pytest dependency,
matching this project's GRADUATION_CONTRACT convention):
`python3 kernel/test_io_tools.py`, exit 0 iff every assertion passes.

Covers patch_file's whitespace-tolerant fallback, added after a live
smoke-test run against the real model (see IMPLEMENTATION_LOG.md) showed
the exact-match-only version rejecting a correct edit over one stray space
before a trailing comment — the single most common real failure logged
across this whole project's SWE-bench runs.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kernel.io_tools import patch_file, write_file, AUTO_RUN_AFTER_WRITE
from kernel.sandbox import set_root


def _with_scratch_root(fn):
    tmp_dir = tempfile.mkdtemp(prefix="io_tools_test_")
    try:
        set_root(tmp_dir)
        AUTO_RUN_AFTER_WRITE["enabled"] = False  # test files aren't meant to run standalone
        fn(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_exact_match_unchanged(tmp_dir):
    write_file("a.py", "def add(a, b):\n    return a - b\n")
    result = patch_file("a.py", search="    return a - b", replace="    return a + b")
    with open(os.path.join(tmp_dir, "a.py")) as f:
        assert f.read() == "def add(a, b):\n    return a + b\n"
    assert "ERROR" not in result and "REJECTED" not in result


def test_whitespace_tolerant_fallback(tmp_dir):
    # Real scenario from the live smoke test: file has two spaces before the
    # comment, model's search string has three.
    write_file("buggy.py", "def add(a, b):\n    return a - b  # bug: should be addition\n")
    result = patch_file(
        "buggy.py",
        search="    return a - b   # bug: should be addition",  # 3 spaces, file has 2
        replace="    return a + b",
    )
    with open(os.path.join(tmp_dir, "buggy.py")) as f:
        content = f.read()
    assert "ERROR" not in result and "REJECTED" not in result, f"fallback should have matched: {result}"
    assert content == "def add(a, b):\n    return a + b\n"


def test_ambiguous_fallback_is_rejected_not_guessed(tmp_dir):
    # Neither line matches the search text exactly (2 and 3 spaces before
    # '#', search has 4) — but both collapse to the same fuzzy pattern, so
    # the fallback must refuse rather than silently pick one.
    write_file("dup.py", "x = 1  # note\ny = 1   # note\n")
    result = patch_file("dup.py", search="= 1    # note", replace="= 2  # note")
    assert "ERROR" in result, "an ambiguous whitespace-tolerant match must be rejected, not guessed"
    with open(os.path.join(tmp_dir, "dup.py")) as f:
        assert f.read() == "x = 1  # note\ny = 1   # note\n", "file must be untouched on rejection"


def test_genuinely_not_found_still_errors(tmp_dir):
    write_file("c.py", "def f():\n    pass\n")
    result = patch_file("c.py", search="this text does not exist anywhere", replace="x")
    assert "ERROR" in result


def _run_self_test():
    tests = [
        test_exact_match_unchanged,
        test_whitespace_tolerant_fallback,
        test_ambiguous_fallback_is_rejected_not_guessed,
        test_genuinely_not_found_still_errors,
    ]
    for test_fn in tests:
        _with_scratch_root(test_fn)
        print(f"OK   {test_fn.__name__}")
    print("\nAll kernel/io_tools.py self-tests passed.")


if __name__ == "__main__":
    _run_self_test()
