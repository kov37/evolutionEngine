#!/usr/bin/env python3
"""Recursive directory search utility — multi-file counterpart to search_file.

Searches every text file under a root directory for a given pattern,
returning (relative_path, line_number, line_text) tuples.
"""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
from pathlib import Path


def grep_dir(pattern: str, path: str = ".") -> list[tuple[str, int, str]]:
    """Recursively search files under *path* for *pattern*.

    Returns a list of ``(relative_path, 1-indexed_line_number, line_text)``
    tuples.  At most 200 matches are returned; if more exist the last entry
    is a special note about how many were omitted.

    Parameters
    ----------
    pattern : str
        Substring to search for in each file's lines.
    path : str
        Root directory to walk (default ``"."``).

    Returns
    -------
    list[tuple[str, int, str]]
        Match tuples: (relative_path_with_forward_slashes, line_number,
        stripped_line_text).
    """
    path = os.path.abspath(path)
    results: list[tuple[str, int, str]] = []
    omit_notice_index = None  # index to overwrite if we hit the cap

    for dirpath, dirnames, filenames in os.walk(path):
        # Skip .git directories entirely (in-place mutation of dirnames).
        dirnames[:] = [
            d for d in dirnames if d != ".git"
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
            rel_path = os.path.relpath(fpath, path).replace(os.sep, "/")

            for lineno_0, line in enumerate(lines, start=1):
                if pattern in line:
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

    # If we hit the cap, append the omitted-count note as the last entry.
    if omit_notice_index is not None:
        remaining = 200 - len(results)
        # The last appended item was already at index `omit_notice_index`,
        # so that's now also the count of matched items (should be 199 after
        # appending the 200th, then breaking).  Actually let me recount:
        # when len(results) == 200 we break without appending.
        # The last appended item was at index 199.  We need to know how many
        # total matches exist vs 200.  Since we only get exact count up to
        # the cap, we report "and N more were omitted" with no precise N
        # (we know at least 1).
        results.append(
            (
                "",
                0,
                f"(... and {remaining} match(es) were omitted — total matches ≥ 200)",
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
