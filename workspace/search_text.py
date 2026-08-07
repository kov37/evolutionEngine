#!/usr/bin/env python3
"""Standalone text-search utility.

Usage:
    python search_text.py <pattern> <filepath>

Exits 0 if at least one matching line is found, non-zero otherwise.
"""

import os
import sys
import tempfile


def search_file(pattern: str, filepath: str) -> list[tuple[int, str]]:
    """Scan *filepath* line by line and return every 1-indexed line that
    contains *pattern* together with its content.

    Returns a list of ``(line_number, line_content)`` tuples.
    ``line_content`` still carries its trailing newline if the file had one.
    """
    results: list[tuple[int, str]] = []
    with open(filepath, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if pattern in line:
                results.append((lineno, line))
    return results


def _run_self_test() -> None:
    """Run internal self-tests and exit with 0 only if every assertion passes."""

    # ------------------------------------------------------------------ #
    # 1) Build a temporary file with a KNOWN marker on a KNOWN line
    # ------------------------------------------------------------------ #
    needle = "NEEDLE_7f3a9"
    filler_count = 42  # plenty of unrelated lines before and after

    tmp_lines: list[str] = []
    for i in range(filler_count):
        tmp_lines.append(f"FILLER_LINE_{i:04d}\n")
    tmp_lines.append(f"--- {needle} ---\n")          # known line at index filler_count (0-based)
    for i in range(10):
        tmp_lines.append(f"AFTER_{i:04d}\n")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.writelines(tmp_lines)
        tmp_path = fh.name

    try:
        # ------------------------------------------------------------------ #
        # 2) Assert the core function returns exactly one match on the
        #    correct (1-indexed) line number with the expected content.
        # ------------------------------------------------------------------ #
        results = search_file(needle, tmp_path)

        expected_lineno = filler_count + 1  # +1 because enumerate starts at 1
        assert len(results) == 1, (
            f"Expected exactly 1 match but got {len(results)}"
        )
        lineno, content = results[0]
        assert lineno == expected_lineno, (
            f"Expected line number {expected_lineno} but got {lineno}"
        )
        assert needle in content, (
            f"Pattern '{needle}' not found in matched line: {content!r}"
        )

        # ------------------------------------------------------------------ #
        # 3) Assert that a pattern guaranteed to be absent yields empty list.
        # ------------------------------------------------------------------ #
        no_match = search_file("DOES_NOT_EXIST_XYZ", tmp_path)
        assert len(no_match) == 0, "Expected no matches for a missing pattern"

        # ------------------------------------------------------------------ #
        # 4) Assert that an empty file yields empty results.
        # ------------------------------------------------------------------ #
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            empty_path = fh.name
        empty_results = search_file("anything", empty_path)
        assert len(empty_results) == 0, "Expected no matches in an empty file"
        os.unlink(empty_path)

        # ------------------------------------------------------------------ #
        # 5) Assert multiline pattern matching works.
        # ------------------------------------------------------------------ #
        multi_path = tmp_path + ".multi"
        with open(multi_path, "w", encoding="utf-8") as fh:
            fh.write("abc\n")
            fh.write("def\n")
            fh.write("abc\n")  # line 3
            fh.write("ghi\n")
        multi_results = search_file("abc", multi_path)
        assert len(multi_results) == 2, "Expected 2 matches for 'abc'"
        assert multi_results[0][0] == 1, "First match should be on line 1"
        assert multi_results[1][0] == 3, "Second match should be on line 3"
        os.unlink(multi_path)

    finally:
        # Clean up the temporary file we created in step 1.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    print("Self-test passed successfully.")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # No arguments → run self-test only.
        _run_self_test()
        sys.exit(0)
    elif len(sys.argv) == 3:
        pattern = sys.argv[1]
        filepath = sys.argv[2]
        results = search_file(pattern, filepath)
        for lineno, content in results:
            print(f"Line {lineno}: {content}", end="")
        sys.exit(0 if results else 1)
    else:
        print(
            f"Usage: python {sys.argv[0]} <pattern> <filepath>",
            file=sys.stderr,
        )
        sys.exit(2)
