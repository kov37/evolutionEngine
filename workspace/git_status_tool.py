#!/usr/bin/env python3
"""Self-contained git-status utility — shelling out to `git status --porcelain=v1`."""

import os
import subprocess
import sys
import tempfile
import shutil


def git_status(root: str = ".") -> list[tuple[str, str]]:
    """Run ``git status --porcelain=v1`` in *root* and return parsed entries.

    Returns a list of ``(status_code, path)`` tuples.
    Returns ``"ERROR: not a git repository."`` when the directory is not under
    version control (checked by caller logic; this function raises on bad input).
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except Exception:
        return "ERROR: not a git repository."

    if result.returncode != 0:
        # Some non-zero exits are fine (e.g. index locked), but a failure
        # because we're outside a repo should be caught by the stderr check.
        stderr = (result.stderr or "").strip()
        if any(msg in stderr for msg in ("not a git repository", "fatal:", "error:")):
            return "ERROR: not a git repository."
        # For other non-zero exits with no clear error, treat as empty.
        return []

    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip("\r\n")
        if not line:
            continue
        # Porcelain v1 format: 2-char status at start, then space, then path.
        # For renamed/deleted-with-replacement lines the index+worktree codes
        # can be followed by " -> " and a new name.
        if len(line) < 3:
            continue
        status_code = line[:2]
        rest = line[2:].lstrip(" ")
        entries.append((status_code, rest))

    return entries


def _run_self_test() -> bool:
    """Internal self-test.  Returns True when every assertion passes."""
    tmpdir = tempfile.mkdtemp(prefix="git_status_tool_test_")
    try:
        # --- setup a real git repo inside tempdir ---
        subprocess.run(
            ["git", "init"], cwd=tmpdir, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "test"], cwd=tmpdir,
            capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=tmpdir,
            capture_output=True, check=True
        )

        # untracked file
        tracked = os.path.join(tmpdir, "tracked.txt")
        with open(tracked, "w") as fh:
            fh.write("committed content\n")
        subprocess.run(
            ["git", "add", "tracked.txt"], cwd=tmpdir,
            capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"], cwd=tmpdir,
            capture_output=True, check=True
        )

        # untracked file
        untracked = os.path.join(tmpdir, "untracked.txt")
        with open(untracked, "w") as fh:
            fh.write("not tracked\n")

        # modify the committed file so it shows as modified (M)
        with open(tracked, "a") as fh:
            fh.write("modified\n")

        # --- call git_status ---
        result = git_status(tmpdir)
        assert isinstance(result, list), f"expected list, got {type(result)}"

        # collect paths by status
        untracked_entries = [p for s, p in result if "??".endswith(s)]
        modified_paths = [p for s, p in result if "M" in s]

        # verify untracked file appears with ?? status
        assert any("untracked.txt" == sp for sp in untracked_entries), (
            f"expected 'untracked.txt' with ??. got {result}"
        )

        # verify modified file appears with M status
        assert any("tracked.txt" == mp for mp in modified_paths), (
            f"expected 'tracked.txt' with M. got {result}"
        )

        # only our two files should be present (no unexpected entries)
        all_paths = [p for _, p in result]
        expected_names = {"untracked.txt", "tracked.txt"}
        assert set(all_paths) == expected_names, (
            f"unexpected paths: {set(all_paths) ^ expected_names}"
        )

        # --- test non-git directory returns error string ---
        plain_dir = tempfile.mkdtemp(prefix="git_status_tool_nogit_")
        err_result = git_status(plain_dir)
        assert err_result == "ERROR: not a git repository.", (
            f"expected error string, got {err_result!r}"
        )

        # --- clean up the plain dir too ---
        shutil.rmtree(plain_dir)

        return True

    finally:
        if os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir)


if __name__ == "__main__":
    # If arguments provided, treat them as root directories and print results.
    import argparse

    parser = argparse.ArgumentParser(description="Print git-status entries.")
    parser.add_argument(
        "root", nargs="?", default=".",
        help="Root directory to inspect (default: current directory).",
    )
    args = parser.parse_args()

    root_dir = args.root

    result = git_status(root_dir)
    if result == "ERROR: not a git repository.":
        print("ERROR: not a git repository.")
        sys.exit(2)

    for status_code, path in result:
        print(f"{status_code} {path}")
    sys.exit(0)
