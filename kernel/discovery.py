"""Small, bounded workspace-discovery primitive used by the kernel."""

import fnmatch
import os
from typing import Optional

from kernel.sandbox import confine


SKIP_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    ".mypy_cache", ".tox", ".ruff_cache",
}
MAX_FIND_RESULTS = 200


def _matches_pattern(relative_path: str, filename: str, pattern: str) -> bool:
    """Match shell-like patterns consistently at the workspace root.

    ``fnmatch`` treats ``**/`` literally, so ``**/*.py`` does not match a
    root-level ``cache.py`` even though users and models expect recursive
    glob syntax to include the root. Keep the existing bounded traversal and
    also test the pattern without its optional recursive prefix.
    """
    candidates = (relative_path, filename)
    if fnmatch.fnmatchcase(relative_path, pattern) or fnmatch.fnmatchcase(filename, pattern):
        return True
    # Python's fnmatch does not give ``**/`` its usual glob meaning: it
    # requires at least one directory.  Test the form with each recursive
    # segment removed as well, which makes that segment mean zero or more
    # directories while preserving ordinary fnmatch behavior everywhere
    # else.  This covers both ``**/*.py`` at the workspace root and
    # ``src/**/*.py`` directly below ``src``.
    optional_recursive = pattern
    while "**/" in optional_recursive:
        optional_recursive = optional_recursive.replace("**/", "", 1)
        if any(fnmatch.fnmatchcase(candidate, optional_recursive) for candidate in candidates):
            return True
    return False


def find_files(pattern: Optional[str] = "*", path: Optional[str] = ".",
               max_results: Optional[int] = 200) -> str:
    """Return bounded, relative paths matching a glob pattern.

    This is intentionally narrower than shell ``find``: it cannot execute
    anything, skips common dependency/build noise, and always reports when
    the result was capped.

    Args:
      pattern: Shell-style file pattern. ``**/`` means zero or more directories.
      path: Workspace-relative directory to search. Defaults to the workspace root.
      max_results: Maximum number of matching files to return, capped at 200.
    """
    pattern = pattern or "*"
    path = path or "."
    if max_results is None:
        max_results = 200
    root = confine(path)
    if not os.path.isdir(root):
        return f"ERROR: '{path}' is not a directory."
    try:
        limit = max(1, min(int(max_results), MAX_FIND_RESULTS))
    except (TypeError, ValueError):
        return "ERROR: max_results must be an integer."

    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if _matches_pattern(rel, name, pattern):
                matches.append(rel)
                if len(matches) == limit:
                    return "\n".join(matches) + f"\n...[truncated at {limit} matches]"
    return "\n".join(matches) if matches else "No files matched."
