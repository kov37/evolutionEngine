#!/usr/bin/env python3
"""Standalone file-comparison utility using difflib (standard library only).

Exposes `diff_files(path_a, path_b) -> str` returning a unified diff of two files.
When run as a script with two file-path arguments, prints the diff and exits
with an appropriate code.  With zero arguments it runs an embedded self-test.
"""

from __future__ import annotations

import difflib
import os
import sys
import tempfile


def diff_files(path_a: str, path_b: str) -> str:
    """Return a unified diff between *path_a* and *path_b*.

    Parameters
    ----------
    path_a : str
        Path to the first (old / "from") file.
    path_b : str
        Path to the second (new / "to") file.

    Returns
    -------
    str
        The unified diff as a single string, or ``""`` if the files are
        byte-for-byte identical.
    """
    with open(path_a, "r", errors="replace") as fh:
        lines_a = fh.readlines()
    with open(path_b, "r", errors="replace") as fh:
        lines_b = fh.readlines()

    # difflib.unified_diff expects lists of strings (one element per line).
    diff_iter = difflib.unified_diff(
        lines_a,
        lines_b,
        fromfile=path_a,
        tofile=path_b,
    )
    result = "".join(diff_iter)

    # unified_diff may return a header-only diff when files are identical;
    # strip it so the contract guarantees "" for identical files.
    if not result or all(
        line.startswith("---") or line.startswith("+++") or line.startswith("@@")
        for line in result.splitlines()
    ):
        return ""

    return result


def _run_self_test() -> None:
    """Create temporary files and assert the self-test scenario passes.

    Exit code 0 on success, 1 on assertion failure.
    """
    # -- helpers ---------------------------------------------------------
    def _write(path: str, content: str) -> None:
        with open(path, "w") as fh:
            fh.write(content)

    _tmp_files: list[str] = []

    try:
        # 1. Identical pair ------------------------------------------------
        tmpl_ident = tempfile.NamedTemporaryFile(
            suffix=".txt", delete=False
        )
        path_a = tmpl_ident.name
        _tmp_files.append(path_a)

        common_text = "hello\nworld\nfoo\nbar\n"
        _write(path_a, common_text)

        # Second file — exact copy
        path_b = path_a + ".copy"
        _tmp_files.append(path_b)
        _write(path_b, common_text)

        result = diff_files(path_a, path_b)
        assert result == "", (
            f"Identical files should yield empty diff; got:\n{result!r}"
        )

        # 2. Differing pair -----------------------------------------------
        path_c = tempfile.mktemp(suffix=".txt")
        _tmp_files.append(path_c)
        _write(
            path_c,
            "alpha\nbeta\ngamma\n",
        )

        path_d = tempfile.mktemp(suffix=".txt")
        _tmp_files.append(path_d)
        # Changed "beta" -> "changed",  removed "gamma", added "delta"
        _write(
            path_d,
            "alpha\nchanged\ndelta\n",
        )

        diff_result = diff_files(path_c, path_d)

        assert diff_result != "", (
            f"Different files should produce non-empty diff; got: {diff_result!r}"
        )

        # Assert the expected markers are present.
        lines = diff_result.splitlines()
        found_minus_beta = any("-beta" in line for line in lines)
        found_plus_changed = any("+changed" in line for line in lines)
        found_plus_delta = any("+delta" in line for line in lines)

        assert found_minus_beta, f"Missing removed marker: {diff_result}"
        assert found_plus_changed, f"Missing changed marker: {diff_result}"
        assert found_plus_delta, f"Missing added marker: {diff_result}"

    finally:
        # Clean up all temp files.
        for p in _tmp_files:
            try:
                os.unlink(p)
            except OSError:
                pass


def main() -> None:
    """CLI entry-point: print unified diff or run self-test."""
    if len(sys.argv) == 1:
        # Zero arguments → self-test.
        try:
            _run_self_test()
        except AssertionError as exc:
            print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if len(sys.argv) != 3:
        print(
            f"Usage: {sys.argv[0]} <file_a> <file_b>", file=sys.stderr
        )
        sys.exit(2)

    path_a, path_b = sys.argv[1], sys.argv[2]

    # Check existence first.
    if not os.path.isfile(path_a):
        print(f"File not found: {path_a}", file=sys.stderr)
        sys.exit(2)
    if not os.path.isfile(path_b):
        print(f"File not found: {path_b}", file=sys.stderr)
        sys.exit(2)

    try:
        diff_output = diff_files(path_a, path_b)
    except OSError as exc:
        print(f"Error reading files: {exc}", file=sys.stderr)
        sys.exit(2)

    if diff_output:
        sys.stdout.write(diff_output)
        sys.exit(1)  # files differ
    else:
        sys.exit(0)  # files identical


if __name__ == "__main__":
    main()
