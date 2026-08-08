#!/usr/bin/env python3
"""Standalone test-runner utility. Prefers pytest, falls back to unittest.

Usage:
    python run_tests_tool.py [directory_path]

Exposes `run_tests(path: str = ".") -> tuple[bool, str]` which discovers and runs
all tests under *path*, returning a (success, summary) tuple.

When executed directly with an optional directory argument (default "."), it prints
the summary to stdout and exits with code 0 on success, 1 if any test failed or
errored, and 2 if no tests were discovered at all.

Originally unittest-only (unittest.TestLoader.discover(), which only collects
unittest.TestCase subclasses). That silently reports "0 tests discovered" — a
false negative, not an empty suite — against ANY codebase using pytest's own
plain `def test_*():` function style, which is most real-world Python
projects (sympy, django, requests, flask, pytest itself...). Confirmed live:
a real overnight agent run against sympy never called run_tests even once in
3,379 turns, instead improvising ad hoc verification scripts via run_shell —
and this tool genuinely does return "Ran 0 tests" against sympy's real test
files, so that wasn't tool avoidance, it was the tool being useless on this
codebase. pytest can ALSO collect unittest.TestCase-based tests, so
preferring it is strictly more compatible, not a tradeoff — unittest
discovery is kept only as a fallback for the case pytest isn't installed at
all in the target project's environment.

Includes an internal self-test in __main__ that validates core behaviour via
throwaway unittest AND pytest-style test suites, plus the pytest-unavailable
fallback path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from io import StringIO

PYTEST_TIMEOUT_SECONDS = 300  # generous vs. run_shell's 15s — this is a
                               # dedicated, purpose-built call, not a
                               # general shell command, and the in-process
                               # unittest path this replaces had NO timeout
                               # at all (a hang there could hang the whole
                               # agent loop) — this is strictly safer than
                               # what it replaces, not a new risk.


def run_tests(path: str = ".") -> tuple[bool, str]:
    """Discover and run all tests under *path*.

    Args:
        path: Directory to discover test modules in. Defaults to ".".

    Returns:
        A tuple of (success, summary_string) where:
            success  — True only if every discovered test passed AND at
                       least one test ran.
            summary  — Short human-readable string such as
                       "Ran 5 tests: 4 passed, 1 failed, 0 errors".
    """
    pytest_result = _run_via_pytest(path)
    if pytest_result is not None:
        return pytest_result
    return _run_via_unittest(path)


def _run_via_pytest(path: str):
    """None means "pytest itself couldn't run" (not installed, bad
    invocation) — the caller falls back to unittest discovery in that
    case. Any other outcome, including a real empty suite, is returned as
    a normal result; the caller must NOT fall back to unittest just
    because pytest found nothing, or a genuinely test-free directory
    would silently get a second (also empty) run for no reason.

    Runs as a subprocess of sys.executable specifically so it uses
    whichever interpreter/venv this process is already running under —
    the same one the project's own tests expect."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", path, "-q", "--no-header", "-p", "no:cacheprovider"],
            capture_output=True, text=True, timeout=PYTEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (False, f"Ran 0 tests: pytest timed out after {PYTEST_TIMEOUT_SECONDS}s")
    except OSError:
        return None

    output = proc.stdout + "\n" + proc.stderr
    if "no module named pytest" in output.lower():
        return None  # pytest genuinely isn't installed here — fall back

    passed, failed, errors = _parse_pytest_summary(output)
    total = passed + failed + errors
    if total == 0:
        return (False, "Ran 0 tests: no tests discovered")
    return (failed == 0 and errors == 0, f"Ran {total} tests: {passed} passed, {failed} failed, {errors} errors")


def _parse_pytest_summary(output: str):
    """Pulls counts from pytest's own final summary line (e.g. "1 failed,
    2 passed in 0.03s"). Per-category regexes rather than one combined
    pattern — pytest's own ordering and presence of each category varies
    (skipped/xfail/warnings can appear or not) and only the three
    categories run_tests's own contract already promises are needed."""
    def _count(word_pattern):
        m = re.search(rf"(\d+) {word_pattern}", output)
        return int(m.group(1)) if m else 0
    return _count("passed"), _count("failed"), _count("errors?")


def _run_via_unittest(path: str) -> tuple[bool, str]:
    """The original implementation — unittest.TestLoader.discover() only
    finds unittest.TestCase subclasses, so this is now a fallback for
    projects that don't have pytest installed, not the primary path."""
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

    # -- Scenario 4: pytest-style bare functions, mixed pass/fail -----------
    # Regression test for the real bug: unittest.TestLoader.discover()
    # only collects unittest.TestCase subclasses, so it silently reported
    # "0 tests discovered" against plain `def test_*():` functions — the
    # style sympy, django, requests, flask, and pytest itself all use.
    # Confirmed live: a real overnight agent run against sympy never
    # called run_tests once in 3,379 turns, because it genuinely returned
    # nothing useful on that codebase.
    tmpdir_4 = tempfile.mkdtemp(prefix="run_tests_tool_selftest_4_")
    try:
        module_path = os.path.join(tmpdir_4, "test_pytest_style.py")
        with open(module_path, "w", encoding="utf-8") as f:
            f.write("""\
def test_bare_function_passes():
    assert 1 + 1 == 2

def test_bare_function_fails():
    assert 1 + 1 == 3
""")
        success, summary = run_tests(tmpdir_4)
        if success is not False:
            errors.append(
                f"Scenario 4 FAILED — pytest-style bare functions must be discovered and run "
                f"(one deliberately fails), got success={success!r}. Summary: {summary}"
            )
        if "0 tests discovered" in summary:
            errors.append(
                f"Scenario 4 FAILED — this is the exact regression: plain `def test_*():` "
                f"functions were not discovered at all. Summary: {summary}"
            )
        if "1 passed" not in summary or "1 failed" not in summary:
            errors.append(f"Scenario 4 FAILED — expected 1 passed and 1 failed. Summary: {summary}")
    finally:
        shutil.rmtree(tmpdir_4, ignore_errors=True)

    # -- Scenario 5: pytest-style bare functions, all passing ---------------
    tmpdir_5 = tempfile.mkdtemp(prefix="run_tests_tool_selftest_5_")
    try:
        module_path = os.path.join(tmpdir_5, "test_pytest_style_pass.py")
        with open(module_path, "w", encoding="utf-8") as f:
            f.write("""\
def test_one():
    assert "hello".isalpha()

def test_two():
    assert sorted([3, 1, 2]) == [1, 2, 3]
""")
        success, summary = run_tests(tmpdir_5)
        if success is not True:
            errors.append(
                f"Scenario 5 FAILED — expected success=True (both pytest-style tests pass), "
                f"got {success!r}. Summary: {summary}"
            )
    finally:
        shutil.rmtree(tmpdir_5, ignore_errors=True)

    # -- Scenario 6: pytest unavailable falls back to unittest discovery ----
    tmpdir_6 = tempfile.mkdtemp(prefix="run_tests_tool_selftest_6_")
    try:
        module_path = os.path.join(tmpdir_6, "test_fallback.py")
        with open(module_path, "w", encoding="utf-8") as f:
            f.write("""\
import unittest

class TestFallback(unittest.TestCase):
    def test_passes(self):
        self.assertEqual(2 + 2, 4)
""")
        real_run = subprocess.run

        def _pytest_not_installed(args, **kwargs):
            if len(args) >= 3 and args[1:3] == ["-m", "pytest"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="No module named pytest\n")
            return real_run(args, **kwargs)

        subprocess.run = _pytest_not_installed
        try:
            success, summary = run_tests(tmpdir_6)
        finally:
            subprocess.run = real_run

        if success is not True:
            errors.append(
                f"Scenario 6 FAILED — with pytest unavailable, must fall back to unittest "
                f"discovery and still find/run the real test, got success={success!r}. Summary: {summary}"
            )
    finally:
        shutil.rmtree(tmpdir_6, ignore_errors=True)

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
