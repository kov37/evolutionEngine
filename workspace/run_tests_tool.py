#!/usr/bin/env python3
"""Standalone test-runner utility using unittest's TestLoader/TestRunner APIs.

Usage:
    python run_tests_tool.py [directory_path]

Exposes `run_tests(path: str = ".") -> tuple[bool, str]` which discovers and runs
all tests under *path* via unittest, returning a (success, summary) tuple.

When executed directly with an optional directory argument (default "."), it prints
the summary to stdout and exits with code 0 on success, 1 if any test failed or
errored, and 2 if no tests were discovered at all.

Includes an internal self-test in __main__ that validates core behaviour via a
throwaway unittest suite containing one passing and one failing test case.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from io import StringIO


def run_tests(path: str = ".") -> tuple[bool, str]:
    """Discover and run all tests under *path* using unittest APIs.

    Args:
        path: Directory to discover test modules in. Defaults to ".".

    Returns:
        A tuple of (success, summary_string) where:
            success  — True only if every discovered test passed AND at
                       least one test ran.
            summary  — Short human-readable string such as
                       "Ran 5 tests: 4 passed, 1 failed, 0 errors".
    """
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=path, top_level_dir=path)

    # Filter out non-test suites to get the real test count
    # loader.countTestCases() already excludes empty sub-suites
    actual_test_count = suite.countTestCases()

    if actual_test_count == 0:
        return (False, "Ran 0 tests: no tests discovered")

    # Run via TextTestRunner — its .run() returns a TestResult object
    stream = StringIO()
    runner = unittest.TextTestRunner(
        stream=stream,
        verbosity=0,
        warnings=None,  # suppress warning filter in Python 3.8+
    )
    result: unittest.TestResult = runner.run(suite)

    tests_run: int = result.testsRun
    fail_count: int = len(result.failures)
    error_count: int = len(result.errors)
    skip_count: int = len(result.skipped) if hasattr(result, "skipped") else 0
    pass_count: int = tests_run - fail_count - error_count

    summary = (
        f"Ran {tests_run} tests: "
        f"{pass_count} passed, {fail_count} failed, "
        f"{error_count} errors"
    )

    success = pass_count == tests_run and tests_run > 0
    return (success, summary)


# ---------------------------------------------------------------------------
# Self-test  (runs when the script is executed directly with no args)
# ---------------------------------------------------------------------------

def _run_self_test() -> bool:
    """Internal self-test that validates run_tests behaviour.

    Returns:
        True if every assertion passes, False otherwise.
    """
    errors: list[str] = []

    # -- Scenario 1: mixed success/failure -----------------------------------
    tmpdir_1 = tempfile.mkdtemp(prefix="run_tests_tool_selftest_1_")
    try:
        module_path = os.path.join(tmpdir_1, "test_mixed.py")
        with open(module_path, "w", encoding="utf-8") as f:
            f.write("""\
import unittest

class TestPassing(unittest.TestCase):
    def test_always_passes(self):
        self.assertTrue(True)

class TestFailing(unittest.TestCase):
    def test_intentionally_fails(self):
        self.fail("This was meant to fail")
""")

        success, summary = run_tests(tmpdir_1)
        if success is not False:
            errors.append(
                f"Scenario 1 FAILED — expected success=False "
                f"(inner test deliberately failed), got {success!r}. "
                f"Summary: {summary}"
            )
        if "0 failed" in summary.lower() or "1 passed" not in summary:
            errors.append(
                f"Scenario 1 FAILED — summary should mention "
                f"a passed count and a non-zero failed count. Got: {summary}"
            )
    finally:
        shutil.rmtree(tmpdir_1, ignore_errors=True)

    # -- Scenario 2: all passing tests ---------------------------------------
    tmpdir_2 = tempfile.mkdtemp(prefix="run_tests_tool_selftest_2_")
    try:
        module_path = os.path.join(tmpdir_2, "test_only_pass.py")
        with open(module_path, "w", encoding="utf-8") as f:
            f.write("""\
import unittest

class TestAllPass(unittest.TestCase):
    def test_first(self):
        self.assertEqual(1 + 1, 2)

    def test_second(self):
        self.assertTrue("hello".isalpha())
""")

        success, summary = run_tests(tmpdir_2)
        if success is not True:
            errors.append(
                f"Scenario 2 FAILED — expected success=True "
                f"(all tests pass), got {success!r}. Summary: {summary}"
            )
    finally:
        shutil.rmtree(tmpdir_2, ignore_errors=True)

    # -- Scenario 3: no tests discovered -------------------------------------
    tmpdir_3 = tempfile.mkdtemp(prefix="run_tests_tool_selftest_3_")
    try:
        success, summary = run_tests(tmpdir_3)
        if success is not False:
            errors.append(
                f"Scenario 3 FAILED — expected success=False "
                f"(no tests discovered), got {success!r}. Summary: {summary}"
            )
    finally:
        shutil.rmtree(tmpdir_3, ignore_errors=True)

    if errors:
        for e in errors:
            print(f"SELF-TEST ERROR: {e}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    # If called with a directory argument, run that suite; otherwise run
    # the internal self-test.
    if len(sys.argv) > 1:
        target_dir = sys.argv[1]
        success, summary = run_tests(target_dir)
        print(summary)
        sys.exit(0 if success else 1)
    else:
        # Run internal self-test
        ok = _run_self_test()
        if ok:
            print("Self-test passed — all assertions hold.", file=sys.stderr)
        else:
            print("Self-test FAILED.", file=sys.stderr)
        sys.exit(0 if ok else 1)
