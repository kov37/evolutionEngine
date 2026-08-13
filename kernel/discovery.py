"""Small, bounded workspace-discovery primitive used by the kernel."""

import fnmatch
import os

from kernel.sandbox import confine


SKIP_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    ".mypy_cache", ".tox", ".ruff_cache",
}
MAX_FIND_RESULTS = 200


def find_files(pattern: str = "*", path: str = ".", max_results: int = 200) -> str:
    """Return bounded, relative paths matching a glob pattern.

    This is intentionally narrower than shell ``find``: it cannot execute
    anything, skips common dependency/build noise, and always reports when
    the result was capped.
    """
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
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                matches.append(rel)
                if len(matches) == limit:
                    return "\n".join(matches) + f"\n...[truncated at {limit} matches]"
    return "\n".join(matches) if matches else "No files matched."
