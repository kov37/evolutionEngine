#!/usr/bin/env python3
"""Recursive directory-listing utility.

Exposes ``list_dir(root: str) -> list[tuple[str, str]]`` which walks an
entire directory tree and returns ``(kind, relative_path)`` tuples sorted
alphabetically by *relative_path* (using forward-slash separators).

When executed as ``python list_dir_tool.py <dir>`` it prints one line per
entry in the form ``<kind>: <relative_path>`` and exits 0.
Running with zero arguments runs an internal self-test via assert.
"""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
from typing import List, Tuple


def list_dir(path: str) -> List[Tuple[str, str]]:
    """Walk *path* recursively and return sorted (kind, relative_path) tuples.

    Parameters
    ----------
    path : str
        Path to the directory to walk.

    Returns
    -------
    list[tuple[str, str]]
        Each tuple is ('dir' | 'file', <relative_path_using_forward_slashes>).
        Sorted alphabetically by the relative-path string.
    """
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Root directory does not exist: {path}")

    results: List[Tuple[str, str]] = []

    def _walk(dirpath: str) -> None:
        try:
            entries = os.listdir(dirpath)
        except OSError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            return

        for name in entries:
            full_path = os.path.join(dirpath, name)
            if os.path.isdir(full_path):
                rel = os.path.relpath(full_path, path).replace(os.sep, "/")
                results.append(("dir", rel))
                _walk(full_path)
            elif os.path.isfile(full_path):
                rel = os.path.relpath(full_path, path).replace(os.sep, "/")
                results.append(("file", rel))

    _walk(path)
    results.sort(key=lambda t: t[1])
    return results


def _run_self_test() -> None:
    """Create a temporary tree and assert list_dir behaves correctly."""
    tmp_root = tempfile.mkdtemp(prefix="list_dir_tool_test_")

    try:
        # Build the tree:
        #   <tmp_root>/
        #     alpha.txt
        #     beta/
        #         gamma.txt
        #         deep/
        #             delta.txt
        os.makedirs(os.path.join(tmp_root, "beta", "deep"))

        for name in ("alpha.txt", "beta/gamma.txt", "beta/deep/delta.txt"):
            full = os.path.join(tmp_root, name)
            with open(full, "w") as fh:
                fh.write("")

        entries = list_dir(tmp_root)

        # After alphabetical sorting by relative_path the expected order is:
        #   alpha.txt             (file)
        #   beta                  (dir)
        #   beta/deep             (dir)
        #   beta/deep/delta.txt  (file)
        #   beta/gamma.txt       (file)
        expected_entries: List[Tuple[str, str]] = [
            ("file", "alpha.txt"),
            ("dir", "beta"),
            ("dir", "beta/deep"),
            ("file", "beta/deep/delta.txt"),
            ("file", "beta/gamma.txt"),
        ]

        assert entries == expected_entries, (
            f"Expected {expected_entries}\nGot         {entries}"
        )

        # Verify error path for non-existent root.
        try:
            list_dir(
                "/tmp/this_directory_definitely_does_not_exist_xyz123"
            )
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            pass  # expected

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print("self-test passed", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        _run_self_test()
        sys.exit(0)

    root_path = sys.argv[1]

    try:
        results = list_dir(root_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except NotADirectoryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    for kind, rel_path in results:
        print(f"{kind}: {rel_path}")
    sys.exit(0)
