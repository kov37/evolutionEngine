#!/usr/bin/env python3
"""
git_diff_tool.py — A standalone utility that shells out to `git diff HEAD`
to show staged+unstaged changes against the last commit.

Usage:
    python git_diff_tool.py [root_dir]

Returns exit code 0 on success (even with no changes), exit code 2 on error.
"""

import os
import sys
import subprocess
import tempfile
import shutil


def git_diff(root: str = ".") -> str:
    """Run `git diff HEAD --no-color` in *root* and return the raw diff text.

    Returns
    -------
    str
        The raw unified-diff output from ``git diff``.  An empty string
        means there are no differences from HEAD (success, not an error).

    Raises / returns error markers on the following conditions:
        - ``root`` is not inside a git working tree       → "ERROR:" msg
        - ``root`` has no commits yet (no HEAD commit)   → "ERROR:" msg
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--no-color"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=30,
        )
    except (FileNotFoundError, OSError) as exc:
        return f"ERROR: failed to run git binary — {exc}"

    if result.returncode == 0:
        # git diff returned 0 → success. stdout holds either nothing or the diff.
        return result.stdout

    stderr = result.stderr.strip()

    # Detect "not a git repository" vs "no commits / no HEAD".
    stderr_lower = stderr.lower()

    if "not a git repository" in stderr_lower:
        return (
            f"ERROR: '{os.path.abspath(root)}' is not inside a git working tree"
        )

    # Additional heuristics for edge-case errors that mean "no HEAD / fresh repo":
    #   - fatal: ambiguous argument 'HEAD': unknown revision or path …
    #   - fatal: bad default revision 'HEAD'
    #   - fatal: the empty repository has no commits to diff against
    if (
        "fatal:" in stderr_lower
        and ("head" in stderr_lower)
        and (
            "no commits" in stderr_lower
            or "bad default" in stderr_lower
            or "unknown revision" in stderr_lower
            or "ambiguous argument" in stderr_lower
        )
    ):
        return f"ERROR: '{os.path.abspath(root)}' has no HEAD commit yet (fresh repo)"

    # If git returned non-zero but we can't classify it — still treat as error.
    if stderr:
        return f"ERROR: git failed with code {result.returncode}: {stderr}"

    return f"ERROR: git diff returned non-zero exit code {result.returncode} (no output)"


def main() -> int:
    """Entry point when run as a script.  Prints the diff and exits accordingly."""
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    diff_text = git_diff(root)

    if diff_text.startswith("ERROR:"):
        print(diff_text, file=sys.stderr)
        return 2

    # On success (even empty), exit 0 after printing.
    if diff_text:
        print(diff_text)
    return 0


# ---------------------------------------------------------------------------
# Self-test — executed when the script is run directly with no args (or --selftest).
# ---------------------------------------------------------------------------
def _run_self_test() -> int:
    """Return 0 if every assertion passes, non-zero otherwise."""
    import pathlib

    errors = 0

    def _fail(label: str, got) -> None:
        nonlocal errors
        print(f"FAIL {label}: {got}", file=sys.stderr)
        errors += 1

    # -- Setup a temp directory for all tests ----
    tmp = tempfile.mkdtemp(prefix="git_diff_selftest_")
    try:
        # =================================================================
        # TEST 1 : non-git directory → must return an ERROR: string
        # =================================================================
        never_inited = os.path.join(tmp, "never_inited")
        os.makedirs(never_inited, exist_ok=True)
        result = git_diff(never_inited)
        if not result.startswith("ERROR:"):
            _fail("TEST_1_not_git", f"expected ERROR:, got {result!r}")

        # =================================================================
        # TEST 2 : fresh repo (git init but no commits) → must return ERROR:
        # =================================================================
        fresh = os.path.join(tmp, "fresh_repo")
        os.makedirs(fresh, exist_ok=True)
        subprocess.run(["git", "init"], cwd=fresh, capture_output=True, check=False)
        result = git_diff(fresh)
        if not result.startswith("ERROR:"):
            _fail("TEST_2_fresh_repo", f"expected ERROR:, got {result!r}")

        # =================================================================
        # TEST 3 : committed + modified file → non-empty diff with +/- lines
        # =================================================================
        working = os.path.join(tmp, "working_repo")
        os.makedirs(working)
        subprocess.run(["git", "init"], cwd=working, capture_output=True, check=False)

        target_file = os.path.join(working, "hello.txt")
        committed_content = "hello world\n"
        with open(target_file, "w") as fh:
            fh.write(committed_content)

        subprocess.run(
            ["git", "add", "hello.txt"], cwd=working, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"], cwd=working, capture_output=True
        )

        # Now modify the file
        with open(target_file, "w") as fh:
            fh.write("hello world — MODIFIED\n")

        result = git_diff(working)
        if not result:
            _fail("TEST_3_nonempty", "expected non-empty diff string")
        elif "-hello world" not in result:
            _fail("TEST_3_minus_line", f"expected '-' line with old content, got {result!r}")
        elif "+hello world — MODIFIED" not in result:
            _fail(
                "TEST_3_plus_line",
                f"expected '+' line with new content, got {result!r}",
            )

        # =================================================================
        # TEST 4 : no changes → empty string (still success)
        # =================================================================
        stable = os.path.join(tmp, "stable_repo")
        os.makedirs(stable)
        subprocess.run(["git", "init"], cwd=stable, capture_output=True, check=False)

        target_file2 = os.path.join(stable, "unchanged.txt")
        with open(target_file2, "w") as fh:
            fh.write("I am unchanged\n")
        subprocess.run(
            ["git", "add", "unchanged.txt"], cwd=stable, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "baseline"], cwd=stable, capture_output=True
        )

        result = git_diff(stable)
        if result != "":
            _fail("TEST_4_empty", f"expected '', got {result!r}")

        # =================================================================
        print(f"\nSelf-test complete: {errors} failure(s).")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return errors


if __name__ == "__main__":
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] in ("--selftest",)):
        # Run internal self-test.
        errs = _run_self_test()
        sys.exit(0 if errs == 0 else 1)
    else:
        # Normal invocation: print diff & exit properly.
        sys.exit(main())
