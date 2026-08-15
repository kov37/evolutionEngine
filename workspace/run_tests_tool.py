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
import subprocess
import sys
import tempfile
import unittest
import importlib
import signal
import re
from io import StringIO


RUN_TESTS_TIMEOUT_SECONDS = 30


class _TestRunTimeout(BaseException):
    pass


def _alarm_handler(signum, frame):
    raise _TestRunTimeout


def _pytest_fallback(path: str, absolute_path: str) -> tuple[bool, str] | None:
    """Run pytest when unittest found no cases, if pytest is already present.

    Coding workspaces commonly use function-style ``test_*.py`` modules that
    unittest can import but cannot discover.  The agent should not need to
    guess a second runner in that situation.  This fallback is deliberately
    bounded and non-mutating: it never installs dependencies, and ``None``
    means the existing ``no tests discovered`` result should be preserved.
    """
    if importlib.util.find_spec("pytest") is None:
        return None
    target = absolute_path if os.path.isfile(absolute_path) else os.path.abspath(path)
    try:
        probe = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", target],
            cwd=os.path.dirname(target) if os.path.isfile(target) else target,
            text=True,
            capture_output=True,
            timeout=RUN_TESTS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"pytest test run timed out after {RUN_TESTS_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return None

    output = "\n".join(part.strip() for part in (probe.stdout, probe.stderr) if part.strip())
    output = output[-1800:]
    if probe.returncode == 0:
        return True, f"pytest passed: {output or 'tests passed'}"
    if probe.returncode == 5 and re.search(r"no tests? ran|no tests? collected", output, re.I):
        return None
    return False, f"pytest failed (exit {probe.returncode}): {output or 'see pytest output'}"


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
    # Agents naturally pass either a project directory or the focused test
    # file they just inspected. unittest.discover only accepts directories;
    # normalize a file target into its parent directory plus an exact pattern
    # so the tool contract remains ergonomic and deterministic.
    absolute_path = os.path.abspath(path)
    # This tool is called repeatedly inside one long-lived agent process.
    # unittest imports test modules normally, so a second validation can
    # otherwise reuse the first version of the implementation from
    # sys.modules after the agent has edited it. Remove modules belonging to
    # the target tree before discovery so validation observes the filesystem,
    # not Python's previous in-process snapshot.
    target_root = os.path.realpath(
        absolute_path if os.path.isdir(absolute_path) else os.path.dirname(absolute_path)
    )
    for module_name, module in list(sys.modules.items()):
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        try:
            module_real = os.path.realpath(module_file)
            if os.path.commonpath((target_root, module_real)) == target_root:
                del sys.modules[module_name]
        except (OSError, ValueError):
            continue
    importlib.invalidate_caches()
    if os.path.isfile(absolute_path):
        start_dir = os.path.dirname(absolute_path) or os.curdir
        pattern = os.path.basename(absolute_path)
        top_level_dir = start_dir
    else:
        start_dir = path
        pattern = "test*.py"
        top_level_dir = path
    suite = loader.discover(start_dir=start_dir, pattern=pattern, top_level_dir=top_level_dir)

    # Filter out non-test suites to get the real test count
    # loader.countTestCases() already excludes empty sub-suites
    actual_test_count = suite.countTestCases()

    if actual_test_count == 0:
        pytest_result = _pytest_fallback(path, absolute_path)
        if pytest_result is not None:
            return pytest_result
        return (False, "Ran 0 tests: no tests discovered")

    # Run via TextTestRunner — its .run() returns a TestResult object
    stream = StringIO()
    runner = unittest.TextTestRunner(
        stream=stream,
        verbosity=0,
        warnings=None,  # suppress warning filter in Python 3.8+
    )
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, RUN_TESTS_TIMEOUT_SECONDS)
    try:
        result: unittest.TestResult = runner.run(suite)
    except _TestRunTimeout:
        return (False, f"Test run timed out after {RUN_TESTS_TIMEOUT_SECONDS}s; the implementation may be stuck")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)

    tests_run: int = result.testsRun
    fail_count: int = len(result.failures)
    error_count: int = len(result.errors)
    if any("_TestRunTimeout" in traceback_text for _, traceback_text in result.errors):
        return (False, f"Test run timed out after {RUN_TESTS_TIMEOUT_SECONDS}s; the implementation may be stuck")
    skip_count: int = len(result.skipped) if hasattr(result, "skipped") else 0
    pass_count: int = tests_run - fail_count - error_count

    summary = (
        f"Ran {tests_run} tests: "
        f"{pass_count} passed, {fail_count} failed, "
        f"{error_count} errors"
    )

    # A count-only failure is not actionable feedback for an agent. Preserve
    # the compact headline, then include bounded failure/error evidence so the
    # next repair turn can target the implementation instead of guessing or
    # rewriting the probe. Keep this provider-neutral and cap the payload so a
    # pathological traceback cannot consume the agent's context window.
    details: list[str] = []
    for label, cases in (("FAIL", result.failures), ("ERROR", result.errors)):
        for test_case, traceback_text in cases[:4]:
            detail = [line.strip() for line in traceback_text.strip().splitlines() if line.strip()]
            # Keep assertion direction and values, not just the traceback
            # tail. The tail often contains only the last compared field and
            # hides the actual/expected contract that the repair actor needs.
            evidence = [line for line in detail if (
                line.startswith(("-", "+"))
                or "AssertionError" in line
                or "TypeError" in line
                or "SyntaxError" in line
            )]
            evidence.extend(detail[-3:])
            excerpt = " | ".join(dict.fromkeys(evidence))[:1800]
            details.append(f"{label} {test_case}: {excerpt}")
    if details:
        summary += " — " + " || ".join(details)[:1800]

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
        file_success, file_summary = run_tests(module_path)
        if file_success is not True or "Ran 2 tests" not in file_summary:
            errors.append(
                f"Scenario 2 FAILED — file-targeted discovery should pass two tests. "
                f"Got success={file_success!r}, summary={file_summary}"
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
