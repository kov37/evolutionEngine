"""Kernel tier: hand-written, trusted file I/O primitives.

These are the only tools available before anything else exists — the model
cannot bootstrap a single file without write_file, or safely edit one
without read_file/patch_file. Nothing here is model-authored, and this
module should stay small and easy to audit by eye.
"""

import ast
import difflib
import os
import re
import subprocess
import sys

from kernel.sandbox import confine, get_root

# Mutable state write_file/patch_file report back into. Kept out of the tool
# signatures on purpose: extra params would leak into the auto-generated
# JSON schema Ollama builds from these functions.
RUN_STATE = {"goal_met": False, "target_file": None}

# Whether write_file/patch_file auto-execute a just-written .py file as a
# verification step. True for harness.py's curriculum-building (a
# standalone tool SHOULD run standalone — that's the whole contract).
# agent.py turns this off: a real project's files aren't necessarily meant
# to run standalone, and auto-executing whatever the model just touched is
# not something you want against a real project's file with side effects.
AUTO_RUN_AFTER_WRITE = {"enabled": True}


def _resolve(path: str) -> str:
    """Confine a model-supplied path to the current sandbox root. Allows
    subdirectories — only genuine escapes are rejected. See kernel/sandbox.py."""
    return confine(path)


def validate_python_syntax(file_path, content):
    """Gate: reject invalid Python before it ever touches disk."""
    if not file_path.endswith(".py"):
        return True, None
    try:
        ast.parse(content)
        return True, None
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno}, offset {e.offset})"


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:python)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _test_after_write(full_path: str) -> str:
    """Run the file in a sandbox subprocess and report the outcome. Marks
    RUN_STATE['goal_met'] on a clean exit so the harness loop knows to stop."""
    if not full_path.endswith(".py") or not AUTO_RUN_AFTER_WRITE["enabled"]:
        return f"Wrote '{os.path.basename(full_path)}' ({os.path.getsize(full_path)} bytes)."

    print(f"⚙️  Running sandbox execution check on '{os.path.basename(full_path)}'...")
    try:
        result = subprocess.run(
            [sys.executable, full_path],
            capture_output=True, text=True, timeout=10, cwd=get_root(),
        )
    except subprocess.TimeoutExpired:
        print("⏳ TIMEOUT ERROR")
        return "Execution timed out after 10s. The script likely hung (e.g. blocking on input())."

    if result.returncode == 0:
        print("🟢 EXECUTION CLEAN (Exit Code 0)")
        RUN_STATE["goal_met"] = True
        # Relative to root, not just the basename — a leftover from before
        # subdirectories were allowed. Losing the subdirectory prefix here
        # is exactly what broke promotion for a nested path once _resolve()
        # stopped flattening paths to their basename.
        RUN_STATE["target_file"] = os.path.relpath(full_path, get_root())
        return f"Ran '{os.path.basename(full_path)}' successfully (exit 0). Output:\n{result.stdout}"

    print("🔴 RUNTIME CRASH DETECTED")
    return (
        f"Wrote '{os.path.basename(full_path)}' but it crashed "
        f"(exit {result.returncode}):\n{result.stderr}\n{result.stdout}"
    )


def read_file(path: str, offset: int = 1, limit: int = None) -> str:
    """Read and return the exact current contents of a file in the workspace,
    optionally windowed to a range of lines. Always check this before
    patch_file — its search text must match verbatim (a windowed read still
    returns exact, verbatim content for those lines, not a summary).

    For a small-to-moderate file (up to ~2000 lines), call this WITHOUT
    limit and read the whole thing in one call — you don't yet know which
    section you need until you've seen the file, so windowing blind forces
    many small reads to sweep something you could see in one. Only use
    offset/limit for a genuinely huge file, or to re-read a specific section
    you've already located (e.g. right before a patch_file call, to confirm
    exact current whitespace).

    Args:
      path: Filename to read, e.g. 'patch_validator.py'.
      offset: 1-indexed line number to start reading from. Default 1 (the
        start of the file).
      limit: Maximum number of lines to return, starting at offset. Default
        None, meaning read to the end of the file.
    """
    full_path = _resolve(path)
    if not os.path.exists(full_path):
        return f"ERROR: '{path}' does not exist in the workspace."
    with open(full_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total_lines = len(lines)
    if offset < 1:
        offset = 1
    start_idx = offset - 1
    if total_lines and start_idx >= total_lines:
        return f"ERROR: offset {offset} is past the end of '{path}' ({total_lines} lines total)."
    end_idx = total_lines if limit is None else min(total_lines, start_idx + limit)
    content = "".join(lines[start_idx:end_idx])

    window_note = ""
    if offset != 1 or end_idx != total_lines:
        window_note = f" [lines {offset}-{end_idx} of {total_lines}]"
    return f"--- {path}{window_note} ({len(content)} chars) ---\n{content}"


def list_workspace() -> str:
    """List every file currently in the workspace, with size in bytes."""
    root = get_root()
    entries = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if os.path.isdir(full):
            entries.append(f"{name}/ (dir)")
        else:
            entries.append(f"{name} ({os.path.getsize(full)} bytes)")
    return "\n".join(entries) if entries else "Workspace is empty."


def write_file(path: str, content: str) -> str:
    """Create a new file inside the workspace, or completely overwrite an
    existing one. Use this for brand new files or a full rewrite.

    Args:
      path: Filename to write, e.g. 'patch_validator.py'.
      content: The full source code that should become the file's contents.
    """
    full_path = _resolve(path)
    content = _strip_code_fences(content)

    valid, err = validate_python_syntax(full_path, content)
    if not valid:
        return f"REJECTED (invalid syntax, nothing written): {err}"

    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")

    return _test_after_write(full_path)


def _visible_whitespace(text: str) -> str:
    """Render whitespace visibly (·  for space, → for tab) so a model
    comparing two near-identical blocks can actually SEE which invisible
    character differs, instead of guessing. Built after a real failure: a
    patch_file search text was rejected for a single-space indentation
    difference (9 spaces vs. the file's real 8) that was invisible in a
    normal text diff but obvious once whitespace is rendered."""
    return text.replace("\t", "→").replace(" ", "·")


def _find_closest_match_hint(content: str, search: str, max_lines_scanned: int = 3000) -> str:
    """Best-effort fuzzy-match hint appended to a failed patch_file search.
    A near-miss (stale whitespace, one changed line, a slightly-off quote)
    is the common case for a failed search — pointing at the closest real
    block saves a wasted read_file/patch_file round-trip instead of leaving
    the model to guess blind. Returns "" if the file's too large to scan
    cheaply or nothing found is close enough to be useful noise-free."""
    content_lines = content.splitlines()
    search_lines = search.splitlines()
    if not search_lines or len(content_lines) > max_lines_scanned:
        return ""

    window = len(search_lines)
    best_ratio = 0.0
    best_start = None
    for start in range(0, max(1, len(content_lines) - window + 1)):
        candidate = "\n".join(content_lines[start:start + window])
        ratio = difflib.SequenceMatcher(None, search, candidate).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start

    if best_start is None or best_ratio < 0.6:
        return ""

    snippet = "\n".join(content_lines[best_start:best_start + window])
    return (
        f"\n\nClosest match found (lines {best_start + 1}-{best_start + window}, "
        f"{best_ratio:.0%} similar) — compare it against your search text for the "
        f"exact difference. Whitespace shown visibly below (· = space, → = tab) since "
        f"that's the most common cause of a near-miss:\n"
        f"--- your search text ---\n{_visible_whitespace(search)}\n"
        f"--- actual file content at that location ---\n{_visible_whitespace(snippet)}"
    )


def _find_whitespace_tolerant_match(content: str, search: str):
    """Locate `search` as a contiguous run of lines in `content`, ignoring
    trailing whitespace and \\r differences per line — NOT leading/internal
    whitespace, which stays strictly required (Python is indentation-
    sensitive, so silently tolerating that would risk applying a
    replacement that reads correctly to a diff tool but is wrong at
    runtime). Trailing-whitespace tolerance is safe with no such risk: the
    matched region is replaced wholesale by `replace`, so nothing about the
    original trailing whitespace is ever preserved or needs reconciling.

    Returns (start_char_offset, end_char_offset) into `content` for the
    real matched text, or None. Caller substitutes content[start:end] with
    `replace` — same effect as content.replace(search, replace, 1) but
    tolerant of the one real failure mode observed live (a trailing-space/
    line-ending mismatch causing an otherwise-correct search to be
    rejected)."""
    search_lines = search.splitlines()
    if not search_lines:
        return None
    content_lines = content.splitlines(keepends=True)
    norm_search = [line.rstrip() for line in search_lines]
    norm_content = [line.rstrip("\r\n").rstrip() for line in content_lines]

    window = len(search_lines)
    for start in range(0, len(content_lines) - window + 1):
        if norm_content[start:start + window] == norm_search:
            char_start = sum(len(l) for l in content_lines[:start])
            char_end = char_start + sum(len(l) for l in content_lines[start:start + window])
            return char_start, char_end
    return None


def patch_file(path: str, search: str, replace: str) -> str:
    """Surgically replace one exact substring inside a file that already
    exists in the workspace. Use this for small, targeted edits instead of
    rewriting the whole file. Call read_file first to see the exact current
    text. The search string's real content and indentation must match
    verbatim — trailing whitespace and line-ending differences are tolerated,
    everything else is not.

    Args:
      path: Filename to patch, e.g. 'patch_validator.py'. Must already exist.
      search: Exact text to find — content and indentation must match verbatim
        (trailing whitespace/line endings are tolerated).
      replace: Text to substitute in place of the search match.
    """
    full_path = _resolve(path)
    if not os.path.exists(full_path):
        return f"ERROR: '{path}' does not exist yet — use write_file to create it first."

    with open(full_path, "r", encoding="utf-8") as f:
        content = f.read()

    search = _strip_code_fences(search)
    replace = _strip_code_fences(replace)

    if search in content:
        new_content = content.replace(search, replace, 1)
    else:
        match = _find_whitespace_tolerant_match(content, search)
        if match is None:
            hint = _find_closest_match_hint(content, search)
            return (
                f"ERROR: search text was not found verbatim in '{path}' (checked exact match and "
                f"trailing-whitespace-tolerant match — indentation and content still must match "
                f"exactly). Call read_file to see the exact current contents, then retry.{hint}"
            )
        start, end = match
        new_content = content[:start] + replace + content[end:]
    valid, err = validate_python_syntax(full_path, new_content)
    if not valid:
        return f"REJECTED (invalid syntax, nothing written): {err}"

    with open(full_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return _test_after_write(full_path)
