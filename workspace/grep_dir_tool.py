#!/usr/bin/env python3
"""Recursive directory search utility — multi-file counterpart to search_file.

Searches every text file under a root directory for a given pattern,
returning (relative_path, line_number, line_text) tuples.
"""

import os
import re
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Optional

# Duplicated from list_dir_tool.py's SKIP_DIR_NAMES rather than imported —
# these workspace/ tools are each standalone, independently runnable
# scripts by design (see their own `__main__` blocks). Same real bug fixed
# in both: only skipping .git meant a virtualenv or node_modules under the
# search root got walked and grepped through in full, real noise observed
# live in a SWE-bench checkout with a `.venv`.
SKIP_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    ".mypy_cache", ".tox", ".ruff_cache",
}


def grep_dir(pattern: str, path: Optional[str] = "."):
    """Recursively search files under *path* for *pattern*. If *path* is a
    single file rather than a directory, searches just that file instead of
    erroring — the common case of pointing grep_dir at one known file works
    the way you'd expect, not an empty result with no explanation.

    *pattern* is a regex (Python ``re`` syntax, e.g. ``class .*Distribution``
    or ``foo|bar``) — matched with ``re.search`` against each line, same as
    real ``grep``. A pattern that fails to compile as regex (e.g. an
    unescaped bracket meant literally) falls back to plain substring
    matching instead of erroring, so a simple literal string still works
    either way.

    Returns a list of ``(relative_path, 1-indexed_line_number, line_text)``
    tuples.  At most 200 matches are returned; if more exist the last entry
    is a special note about how many were omitted.

    Parameters
    ----------
    pattern : str
        Regex to search for in each file's lines (falls back to a plain
        substring match if *pattern* isn't valid regex).
    path : str
        Directory to walk, or a single file to search directly (default
        ``"."``).

    Returns
    -------
    list[tuple[str, int, str]]
        Match tuples: (relative_path_with_forward_slashes, line_number,
        stripped_line_text). On error, a string starting with "ERROR:"
        instead.

    Args:
      pattern: Regular expression or literal fallback to find.
      path: Workspace-relative directory or file to scan. Defaults to ".".
    """
    path = path or "."
    original_path = path
    path = os.path.abspath(path)

    if not os.path.exists(path):
        return f"ERROR: '{original_path}' does not exist."

    try:
        matcher = re.compile(pattern)
        line_matches = lambda line: matcher.search(line) is not None
    except re.error:
        # Not valid regex (e.g. an unescaped literal bracket) — fall back to
        # a plain substring check rather than erroring on a reasonable
        # literal-string search.
        line_matches = lambda line: pattern in line

    results: list[tuple[str, int, str]] = []
    omit_notice_index = None  # index to overwrite if we hit the cap

    if os.path.isfile(path):
        # A single file, not a directory: search just this file. Mimic
        # os.walk()'s per-directory tuple shape so the loop below is
        # unchanged either way.
        walk_iter = [(os.path.dirname(path), [], [os.path.basename(path)])]
        base_for_relpath = os.path.dirname(path)
    else:
        walk_iter = os.walk(path)
        base_for_relpath = path

    for dirpath, dirnames, filenames in walk_iter:
        # Skip noise directories entirely (in-place mutation of dirnames,
        # required for os.walk to actually not descend into them).
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIR_NAMES and not d.endswith(".egg-info")
        ]

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)

            # Read file as UTF-8; silently skip binary files.
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()
            except (UnicodeDecodeError, OSError):
                continue

            # Compute relative path using forward slashes.
            rel_path = os.path.relpath(fpath, base_for_relpath).replace(os.sep, "/")

            for lineno_0, line in enumerate(lines, start=1):
                if line_matches(line):
                    match_info: tuple[str, int, str] = (
                        rel_path,
                        lineno_0,
                        line.rstrip("\n\r"),
                    )
                    if len(results) == 200:
                        # We've hit the cap; record where to put the
                        # omit notice and stop collecting further matches.
                        omit_notice_index = len(results)
                        break
                    results.append(match_info)

            # If we broke out early due to the cap, stop processing more files.
            if omit_notice_index is not None:
                break
        else:
            continue  # inner for completed normally — keep walking
        break  # inner for hit the break; we're done with os.walk loop

    # If we hit the cap, append an honest truncation note. We intentionally
    # stop at the cap rather than doing a second full traversal just to count
    # matches in a potentially huge repository.
    if omit_notice_index is not None:
        results.append(
            (
                "",
                0,
                "(... additional matches omitted — total matches >= 200)",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Self-test  (executed when run directly with no CLI arguments)
# ---------------------------------------------------------------------------

def _self_test() -> bool:
    """Run internal assertions; return True on success."""
    # Build a temporary directory tree.
    tmp_root = tempfile.mkdtemp(prefix="grep_dir_selftest_")
    try:
        # Create structure across ≥ 2 nesting levels with 3 files.
        sub_a = os.path.join(tmp_root, "a_sub")
        sub_b = os.path.join(sub_a, "b_sub")
        os.makedirs(sub_b)

        f1_path = os.path.join(tmp_root, "a.txt")
        f2_path = os.path.join(sub_a, "b.txt")
        f3_path = os.path.join(sub_b, "c.txt")

        marker = "NEEDLE_9f2c1"

        # File 1: marker on line 5
        with open(f1_path, "w", encoding="utf-8") as fh:
            for i in range(1, 6):
                fh.write(f"line {i}\n")
            fh.write(marker + "\n")

        # File 2: marker on line 3
        with open(f2_path, "w", encoding="utf-8") as fh:
            for i in range(1, 4):
                fh.write(f"sub_a_line_{i}\n")
            fh.write(marker + "\n")

        # File 3: no marker at all
        with open(f3_path, "w", encoding="utf-8") as fh:
            fh.write("just a normal file\n")
            fh.write("nothing special here\n")

        # --- assertions ---------------------------------------------------
        matches = grep_dir(marker, path=tmp_root)
        assert len(matches) == 2, (
            f"Expected exactly 2 matches but got {len(matches)}"
        )

        paths_and_lines = {(p, ln) for p, ln, _ in matches}
        # The two expected files must each contribute one match.
        assert ("a.txt", 6) in paths_and_lines, (
            f"a.txt line 6 not found among {paths_and_lines}"
        )
        assert ("a_sub/b.txt", 4) in paths_and_lines, (
            f"a_sub/b.txt line 4 not found among {paths_and_lines}"
        )

        # Verify line content contains the marker.
        for p, ln, text in matches:
            assert marker in text, (
                f"Marker '{marker}' not in line of {p}:{ln}: {text!r}"
            )

        # Pattern known not to exist → empty list.
        no_match = grep_dir("XYZNONEXISTENT_abc123", path=tmp_root)
        assert no_match == [], (
            f"Expected [] for non-existent pattern but got {no_match}"
        )

        # A single FILE path (not a directory) — must search that file
        # directly instead of silently returning []. This is the exact
        # gap found live against sympy-13878: the model pointed grep_dir at
        # one known file and got an unexplained empty result 9 times in a
        # row before giving up and switching tools.
        file_matches = grep_dir(marker, path=f1_path)
        assert len(file_matches) == 1, (
            f"Expected exactly 1 match searching a single file but got {file_matches}"
        )
        assert file_matches[0][0] == "a.txt", (
            f"Expected relative path 'a.txt' but got {file_matches[0][0]!r}"
        )
        assert file_matches[0][1] == 6, (
            f"Expected line 6 but got {file_matches[0][1]}"
        )

        # A path that doesn't exist at all → clear error, not silent [].
        missing = grep_dir(marker, path=os.path.join(tmp_root, "does_not_exist"))
        assert isinstance(missing, str) and missing.startswith("ERROR:"), (
            f"Expected an ERROR string for a nonexistent path but got {missing!r}"
        )

        # Regex support — the exact failure mode found live against
        # sympy-13878: a pipe-alternation pattern must actually match, not
        # silently return [] because "|" was treated as a literal character.
        regex_matches = grep_dir(r"a_sub/b\.txt line 4 does not exist|" + marker, path=tmp_root)
        assert len(regex_matches) == 2, (
            f"Expected alternation pattern to match both real occurrences, got {regex_matches}"
        )

        # Regex anchors/wildcards must work too, not be treated as literal chars.
        anchor_matches = grep_dir(r"^line \d$", path=f1_path)
        assert len(anchor_matches) == 5, (
            f"Expected 5 single-digit 'line N' matches via regex anchors, got {anchor_matches}"
        )

        # A pattern that ISN'T valid regex must fall back to a literal
        # substring search instead of erroring.
        with open(os.path.join(tmp_root, "bracket.txt"), "w", encoding="utf-8") as fh:
            fh.write("value = arr[unclosed\n")
        literal_matches = grep_dir("arr[unclosed", path=os.path.join(tmp_root, "bracket.txt"))
        assert len(literal_matches) == 1, (
            f"Invalid-regex pattern should fall back to literal substring match, got {literal_matches}"
        )

        # Noise directories (.venv, __pycache__, etc.) must not be walked at
        # all — real failure observed live: grep_dir against a SWE-bench
        # checkout with a virtualenv matched noise inside .venv's own
        # installed packages instead of just the real project source.
        for noisy in (".venv", "__pycache__", "node_modules"):
            noisy_dir = os.path.join(tmp_root, noisy)
            os.makedirs(noisy_dir)
            with open(os.path.join(noisy_dir, "noise.txt"), "w", encoding="utf-8") as fh:
                fh.write(marker + "\n")
        noise_matches = grep_dir(marker, path=tmp_root)
        assert len(noise_matches) == 2, (
            f"Noise directories should be skipped — expected still exactly 2 real matches, "
            f"got {len(noise_matches)}: {noise_matches}"
        )

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return True


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> int:
    """Run grep_dir from the command line and print results."""
    if len(sys.argv) < 2:
        # No arguments → run self-test instead.
        ok = _self_test()
        return 0 if ok else 1
    pattern = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else "."

    matches = grep_dir(pattern, path=target)
    for path, lineno, text in matches:
        print(f"{path}:{lineno}: {text}")
    return 0 if matches else 1


if __name__ == "__main__":
    sys.exit(main())
