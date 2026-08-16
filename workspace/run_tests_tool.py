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
import importlib.util
import signal
import re
import hashlib
from io import StringIO


RUN_TESTS_TIMEOUT_SECONDS = 30


class _TestRunTimeout(BaseException):
    pass


def _alarm_handler(signum, frame):
    raise _TestRunTimeout


def _project_import_root(absolute_path: str) -> str:
    """Return the active project root for imports during test discovery.

    The agent confines a graduated ``run_tests`` path to the active project,
    but unittest normally puts only the test directory on ``sys.path``. That
    breaks ordinary source-tree layouts such as ``project/pkg/tests`` where
    tests import ``pkg`` from the project root. Keep this helper independent
    of language or package names: use the orchestrator's sandbox root when
    available, and fall back to the path's containing directory for direct
    standalone use.
    """
    try:
        from kernel.sandbox import get_root
        configured = os.path.realpath(get_root())
        candidate = os.path.realpath(absolute_path)
        if os.path.commonpath((configured, candidate)) == configured:
            return configured
    except (ImportError, OSError, ValueError):
        pass
    fallback = os.path.dirname(absolute_path) if os.path.isfile(absolute_path) else absolute_path
    return os.path.realpath(fallback)


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
    # Keep both the first failure (usually the actionable assertion) and the
    # final summary. Taking only the tail can discard the actual traceback and
    # leave the repair agent with warnings or unrelated later failures.
    if len(output) > 3600:
        output = output[:1800] + "\n... [pytest output truncated by host] ...\n" + output[-1800:]
    if probe.returncode == 0:
        return True, f"pytest passed: {output or 'tests passed'}"
    if probe.returncode == 5 and re.search(r"no tests? ran|no tests? collected", output, re.I):
        return None
    return False, f"pytest failed (exit {probe.returncode}): {output or 'see pytest output'}"


def _function_style_fallback(path: str, absolute_path: str) -> tuple[bool, str] | None:
    """Run plain ``test_*`` functions without requiring pytest.

    A workspace may use pytest's function collection convention while the
    execution environment deliberately has no pytest installation.  The
    agent's validation contract still needs an honest result in that case.
    This fallback implements the dependency-free part of that convention:
    import each matching module, collect zero-argument top-level functions
    named ``test_*``, and execute them through unittest's result machinery.
    It does not emulate pytest fixtures or plugins; unsupported signatures
    fail as ordinary test errors instead of being silently counted as passes.
    """
    if os.path.isfile(absolute_path):
        test_files = [absolute_path]
    elif os.path.isdir(absolute_path):
        test_files = []
        for root, dirs, files in os.walk(absolute_path):
            dirs[:] = sorted(name for name in dirs if name != "__pycache__")
            test_files.extend(
                os.path.join(root, name)
                for name in sorted(files)
                if name.startswith("test") and name.endswith(".py")
            )
        test_files.sort()
    else:
        return None

    if not test_files:
        return None

    project_root = _project_import_root(absolute_path)
    import_roots = sorted({project_root, *(os.path.dirname(filename) for filename in test_files)})
    old_sys_path = list(sys.path)
    for root in reversed(import_roots):
        if root and root not in sys.path:
            sys.path.insert(0, root)

    suite = unittest.TestSuite()
    load_errors: list[str] = []
    try:
        for index, filename in enumerate(test_files):
            digest = hashlib.sha1(os.path.realpath(filename).encode("utf-8")).hexdigest()[:12]
            module_name = f"_novelty_function_tests_{digest}_{index}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, filename)
                if spec is None or spec.loader is None:
                    raise ImportError("could not create an import spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            except BaseException as exc:
                load_errors.append(f"{os.path.basename(filename)}: {type(exc).__name__}: {exc}")
                continue

            for name, value in sorted(vars(module).items()):
                if name.startswith("test_") and callable(value) and not isinstance(value, type):
                    suite.addTest(unittest.FunctionTestCase(value, description=f"{filename}:{name}"))
    finally:
        import_roots_real = [os.path.realpath(root) for root in import_roots if root]
        for module_name, module in list(sys.modules.items()):
            module_file = getattr(module, "__file__", None)
            if not module_file:
                continue
            try:
                module_real = os.path.realpath(module_file)
                if any(os.path.commonpath((root, module_real)) == root for root in import_roots_real):
                    sys.modules.pop(module_name, None)
            except (OSError, ValueError):
                continue
        sys.path[:] = old_sys_path

    if load_errors:
        excerpt = " | ".join(load_errors)[:1800]
        return False, f"function-style test collection failed: {excerpt}"
    if suite.countTestCases() == 0:
        return None

    stream = StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=0, warnings=None)
    result = runner.run(suite)
    tests_run = result.testsRun
    fail_count = len(result.failures)
    error_count = len(result.errors)
    pass_count = tests_run - fail_count - error_count
    summary = (
        f"Ran {tests_run} function-style tests: "
        f"{pass_count} passed, {fail_count} failed, {error_count} errors"
    )
    details: list[str] = []
    for label, cases in (("FAIL", result.failures), ("ERROR", result.errors)):
        for test_case, traceback_text in cases[:4]:
            lines = [line.strip() for line in traceback_text.splitlines() if line.strip()]
            evidence = [line for line in lines if (
                line.startswith(("-", "+"))
                or line.startswith("File ")
                or "AssertionError" in line
                or "TypeError" in line
                or "SyntaxError" in line
            )]
            evidence.extend(lines[-3:])
            details.append(f"{label} {test_case}: {' | '.join(dict.fromkeys(evidence))[:1800]}")
    if details:
        summary += " — " + " || ".join(details)[:1800]
    return pass_count == tests_run and tests_run > 0, summary


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
    project_root = _project_import_root(absolute_path)
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

    # unittest's discover imports a file such as ``test_api.py`` under its
    # short module name.  That name can survive after a temporary workspace
    # is removed, causing the next workspace with the same filename to fail
    # with "module incorrectly imported" before collection begins.  Evict
    # matching discovered test modules regardless of their stale directory.
    if os.path.isfile(absolute_path):
        discovered_basenames = {os.path.basename(absolute_path)}
        project_basenames = {
            os.path.basename(absolute_path),
            *(
                filename
                for filename in os.listdir(os.path.dirname(absolute_path) or os.curdir)
                if filename.endswith(".py")
            ),
        }
    elif os.path.isdir(absolute_path):
        discovered_basenames = {
            filename
            for root, dirs, files in os.walk(absolute_path)
            for filename in files
            if filename.startswith("test") and filename.endswith(".py")
        }
        project_basenames = {
            filename
            for root, dirs, files in os.walk(absolute_path)
            for filename in files
            if filename.endswith(".py")
        }
    else:
        discovered_basenames = set()
        project_basenames = set()
    if discovered_basenames or project_basenames:
        # Never evict every loaded package whose filename is ``__init__.py``.
        # Nested project tests commonly contain that file, and basename-only
        # cleanup would otherwise remove pytest/unittest packages from
        # sys.modules while the agent's own test process is still running.
        stale_basenames = (discovered_basenames | project_basenames) - {"__init__.py"}
        for module_name, module in list(sys.modules.items()):
            module_file = getattr(module, "__file__", None)
            if module_file and os.path.basename(module_file) in stale_basenames:
                sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    if os.path.isfile(absolute_path):
        start_dir = os.path.realpath(os.path.dirname(absolute_path) or os.curdir)
        pattern = os.path.basename(absolute_path)
        top_level_dir = project_root if start_dir != project_root else start_dir
    else:
        # Normalize macOS /var -> /private/var symlinks before unittest's
        # containment assertion compares start_dir with top_level_dir.
        start_dir = os.path.realpath(absolute_path)
        pattern = "test*.py"
        top_level_dir = project_root if start_dir != project_root else start_dir
    previous_sys_path = list(sys.path)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    try:
        suite = loader.discover(start_dir=start_dir, pattern=pattern, top_level_dir=top_level_dir)
    finally:
        sys.path[:] = previous_sys_path

    # Filter out non-test suites to get the real test count
    # loader.countTestCases() already excludes empty sub-suites
    actual_test_count = suite.countTestCases()

    if actual_test_count == 0:
        pytest_result = _pytest_fallback(path, absolute_path)
        if pytest_result is not None:
            return pytest_result
        function_result = _function_style_fallback(path, absolute_path)
        if function_result is not None:
            return function_result
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
                or line.startswith("File ")
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
