"""Kernel tier: hand-written, trusted file I/O primitives.

These are the only tools available before anything else exists — the model
cannot bootstrap a single file without write_file, or safely edit one
without read_file/patch_file. Nothing here is model-authored, and this
module should stay small and easy to audit by eye.
"""

import ast
import difflib
import hashlib
import os
import re
import subprocess
import sys
from typing import Optional

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

# The single-file editor(s) actually registered on the model-facing tool
# surface for this run. registry.load_registry() sets this to match its
# --editor choice; write_file's existing-file rejection must only name a
# tool the model can actually call this turn, not every editor the kernel
# happens to implement.
ACTIVE_EDITORS = {"names": ("patch_file",)}


def set_active_editors(names) -> None:
    ACTIVE_EDITORS["names"] = tuple(names) or ("patch_file",)


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
        hint = ""
        if "generator expression must be parenthesized" in e.msg.lower():
            hint = " Hint: wrap the generator expression in parentheses before passing another keyword argument."
        # A line/column number alone gives a stuck model nothing to correct
        # against — it can only guess again. Show the actual offending text
        # (whitespace made visible, the same convention every other
        # rejection in this file uses) so a repeated identical rejection is
        # diagnosable instead of a silent loop.
        content_lines = content.splitlines()
        excerpt = ""
        if e.lineno and 1 <= e.lineno <= len(content_lines):
            bad_line = content_lines[e.lineno - 1]
            caret_pos = max(0, (e.offset or 1) - 1)
            excerpt = (
                f"\n--- offending line {e.lineno} ---\n"
                f"{_visible_whitespace(bad_line)}\n"
                f"{' ' * caret_pos}^"
            )
        return False, f"SyntaxError: {e.msg} (line {e.lineno}, offset {e.offset}).{hint}{excerpt}"


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


def read_file(path: str, offset: Optional[int] = 1, limit: Optional[int] = None) -> str:
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
    if offset is None:
        offset = 1
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
    if limit is not None and limit <= 0:
        # A small model sometimes emits ``limit: 0`` as if it meant "read the
        # whole file". Returning an empty window wastes a turn; normalize that
        # unambiguous zero/negative bound to the existing full-file behavior.
        limit = None
    end_idx = total_lines if limit is None else min(total_lines, start_idx + limit)
    content = "".join(lines[start_idx:end_idx])

    window_note = ""
    if offset != 1 or end_idx != total_lines:
        window_note = f" [lines {offset}-{end_idx} of {total_lines}]"
    # Snapshot hash: the model can pass this token back to edit_range so a
    # stale edit is rejected instead of landing on a drifted file.
    digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()[:12]
    return f"--- {path}{window_note} ({len(content)} chars) [snapshot {digest}] ---\n{content}"


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
    """Create a new file inside the workspace.

    Existing files are protected: use the currently offered edit tool for
    revisions so the model must identify the current block or line range it
    intends to change.

    Args:
      path: Filename to write, e.g. 'patch_validator.py'.
      content: The full source code that should become the file's contents.
    """
    full_path = _resolve(path)
    if os.path.exists(full_path):
        editors = " or ".join(ACTIVE_EDITORS["names"])
        return (
            f"REJECTED: '{path}' already exists. write_file only creates new files. "
            f"To edit this file, use {editors}. "
            "To create a different file, choose a filename that does not exist yet."
        )
    content = _strip_code_fences(content)

    valid, err = validate_python_syntax(full_path, content)
    if not valid:
        return f"REJECTED (invalid syntax, nothing written): {err}"

    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")

    return _test_after_write(full_path)


def edit_range(path: str, edits: list, snapshot: str | None = None) -> str:
    """Apply batched, line-anchored replacements to an existing file.

    This is the line-anchored editor: the model names the line ranges it
    wants to replace (as shown by read_file's window headers) and supplies
    the new text for each — no verbatim quoting of the old text is required.
    Edits are applied atomically and bottom-up, so all line numbers stay
    valid within one batch regardless of earlier edits in the same call. An
    optional short ``expect`` token on any edit lets the model confirm it is
    editing the region it thinks it is: if the token is absent from the
    current lines, the whole batch is rejected and the host returns the
    actual region with real line numbers — a free, self-correcting re-read
    instead of a silent wrong edit. The optional ``snapshot`` token from
    read_file verifies the whole file is unchanged since it was read.

    Args:
      path: Filename to edit, e.g. 'patch_validator.py'. Must already exist.
      edits: List of dicts with keys:
        start_line (int, 1-based inclusive),
        end_line (int, 1-based inclusive),
        replacement (str, text that replaces the range),
        expect (str, optional; a short token that must appear in the range).
      snapshot: Optional [snapshot ...] token from a read_file header.
    """
    full_path = _resolve(path)
    if not os.path.exists(full_path):
        return f"ERROR: '{path}' does not exist yet — use write_file to create it first."

    with open(full_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if snapshot is not None:
        # The model passes back the [snapshot ...] token read_file printed.
        # Any file change since that read invalidates every line anchor.
        current_digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()[:12]
        if not snapshot.startswith(current_digest):
            return (
                f"REJECTED: snapshot mismatch — '{path}' has changed since the "
                f"lines were read (current snapshot {current_digest}). Re-read the "
                "file, then retry with fresh line numbers and the new snapshot."
            )

    if not isinstance(edits, list) or not edits:
        return "REJECTED: edits must be a non-empty list of edit objects."
    if len(edits) > 8:
        return f"REJECTED: at most 8 edits per batch, got {len(edits)}."

    parsed = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return f"REJECTED: edit {index + 1} is not an object."
        unknown = set(edit) - {"start_line", "end_line", "replacement", "expect"}
        if unknown:
            return f"REJECTED: edit {index + 1} has unknown fields: {sorted(unknown)}."
        start_line = edit.get("start_line")
        end_line = edit.get("end_line")
        for value in (start_line, end_line):
            if not isinstance(value, int) or isinstance(value, bool):
                return (
                    f"REJECTED: edit {index + 1} — start_line and end_line must be "
                    f"integers, got {type(value).__name__}."
                )
        if start_line < 1 or end_line < start_line:
            return (
                f"REJECTED: edit {index + 1} — invalid line range {start_line}-"
                f"{end_line}; start_line must be >= 1 and end_line >= start_line."
            )
        if end_line > len(lines):
            return (
                f"REJECTED: edit {index + 1} — range {start_line}-{end_line} exceeds "
                f"the current file length ({len(lines)} lines). Re-read the file to "
                "get current line numbers, then retry."
            )
        expect = edit.get("expect")
        if expect is not None:
            if not isinstance(expect, str) or not expect.strip():
                return f"REJECTED: edit {index + 1} — expect must be a short string."
            region_text = "".join(lines[start_line - 1: end_line])
            if expect.strip() not in region_text:
                bounded = "".join(lines[max(0, start_line - 5): min(len(lines), end_line + 5)])
                return (
                    f"REJECTED: edit {index + 1} — expected token {expect.strip()[:80]!r} "
                    f"was not found in lines {start_line}-{end_line}. The file has drifted; "
                    f"the actual region is:\n{bounded[:1200]}"
                )
        replacement = str(edit.get("replacement", ""))
        # Remove code fences without stripping meaningful newlines: the
        # replacement is spliced between existing lines, so a lost trailing
        # newline would merge lines together.
        replacement = re.sub(r"^```(?:python)?\s*\n?", "", replacement, flags=re.IGNORECASE)
        replacement = re.sub(r"\n?```$", "", replacement)
        replacement_lines = replacement.splitlines(keepends=True)
        if replacement_lines and not replacement_lines[-1].endswith("\n"):
            replacement_lines[-1] += "\n"
        parsed.append((index + 1, start_line, end_line, replacement_lines))

    # Bottom-up application keeps every line number valid within the batch.
    applied_lines = list(lines)
    results = []
    for index, start_line, end_line, replacement_lines in sorted(
        parsed, key=lambda item: item[1], reverse=True
    ):
        applied_lines[start_line - 1: end_line] = replacement_lines
        results.append((index, start_line, end_line, replacement_lines))

    new_content = "".join(applied_lines)
    valid, err = validate_python_syntax(full_path, new_content)
    if not valid:
        return f"REJECTED (invalid syntax, nothing written): {err}"

    with open(full_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    # Report the new line map so the model can anchor its next batch
    # without a full re-read, plus a bounded preview of the last edit.
    line_map = []
    for index, start_line, end_line, replacement_lines in results:
        new_end = start_line + len(replacement_lines)
        line_map.append(f"edit {index}: lines {start_line}-{end_line} -> {start_line}-{new_end - 1}")
    last_start, last_repl = results[-1][1], results[-1][3]
    preview_start = max(1, last_start - 1)
    preview = "".join(applied_lines[preview_start - 1: preview_start - 1 + len(last_repl) + 2])
    new_digest = hashlib.sha256("".join(applied_lines).encode("utf-8")).hexdigest()[:12]
    return (
        f"Applied {len(results)} edit(s) to '{path}' ({len(applied_lines)} lines now) "
        f"[snapshot {new_digest}]. "
        + "; ".join(line_map)
        + f"\n--- preview around the last edit (lines {preview_start}-{preview_start + len(last_repl) + 1}) ---\n"
        + preview[:800]
    )


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
    if not search_lines:
        return ""

    if len(content_lines) > max_lines_scanned:
        # Full fuzzy matching is intentionally bounded for large source
        # files, but returning no location makes the next model turn guess
        # again. Find one distinctive exact line from the requested block and
        # show a small current-file neighborhood around it. This is a tool
        # feedback improvement, not a language- or task-specific repair rule.
        normalized = [line.rstrip() for line in content_lines]
        candidates = []
        for search_index, search_line in enumerate(search_lines):
            needle = search_line.rstrip()
            if len(needle.strip()) < 12:
                continue
            matches = [index for index, line in enumerate(normalized) if line == needle]
            if len(matches) == 1:
                candidates.append((len(needle), search_index, matches[0]))
        if not candidates:
            return ""
        _, search_index, match_index = max(candidates)
        radius = min(8, max(3, len(search_lines) // 2))
        start = max(0, match_index - radius)
        end = min(len(content_lines), match_index + radius + 1)
        snippet = "\n".join(content_lines[start:end])
        return (
            f"\n\nA distinctive line from your search exists, but the full block is stale. "
            f"Current file excerpt at lines {start + 1}-{end} (the matching search line was "
            f"line {search_index + 1} of your request):\n"
            f"--- actual current file content ---\n{_visible_whitespace(snippet)}\n"
            "Use this current excerpt to construct a new patch; do not replay the old block."
        )

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


def _find_outer_indent_match(content: str, search: str):
    """Match a uniquely identified block when the caller omitted one common
    outer indentation level, returning its span and the indentation to add to
    a replacement. Internal indentation remains significant."""
    search_lines = search.splitlines()
    if not search_lines or any(line and line[0].isspace() for line in search_lines):
        return None
    content_lines = content.splitlines(keepends=True)
    candidates = []
    for start in range(0, len(content_lines) - len(search_lines) + 1):
        actual = [line.rstrip("\r\n").rstrip() for line in content_lines[start:start + len(search_lines)]]
        if len(actual) != len(search_lines):
            continue
        if all(a.lstrip() == s.rstrip() for a, s in zip(actual, search_lines)):
            candidates.append(start)
    if len(candidates) != 1:
        return None
    start = candidates[0]
    first = content_lines[start].rstrip("\r\n")
    indent = first[:len(first) - len(first.lstrip())]
    char_start = sum(len(line) for line in content_lines[:start])
    char_end = char_start + sum(len(line) for line in content_lines[start:start + len(search_lines)])
    return char_start, char_end, indent


def patch_file(path: str, find_exact_block: str, replace_with_block: str) -> str:
    """Surgically replace one exact substring inside a file that already
    exists in the workspace. Use this for small, targeted edits instead of
    rewriting the whole file. Call read_file first to see the exact current
    text. The exact block's real content and indentation must match
    verbatim — trailing whitespace and line-ending differences are tolerated,
    everything else is not.

    Args:
      path: Filename to patch, e.g. 'patch_validator.py'. Must already exist.
      find_exact_block: Exact text to find — content and indentation must match verbatim
        (trailing whitespace/line endings are tolerated).
      replace_with_block: Text to substitute in place of the exact block.
    """
    full_path = _resolve(path)
    if not os.path.exists(full_path):
        return f"ERROR: '{path}' does not exist yet — use write_file to create it first."

    with open(full_path, "r", encoding="utf-8") as f:
        content = f.read()

    search = _strip_code_fences(find_exact_block)
    replace = _strip_code_fences(replace_with_block)

    if search in content:
        occurrences = content.count(search)
        if occurrences > 1:
            # Silent first-match replacement corrupts the wrong location.
            # Refuse ambiguity and tell the model where the matches are.
            starts = []
            pos = 0
            while True:
                index = content.find(search, pos)
                if index == -1:
                    break
                starts.append(index)
                pos = index + 1
            line_numbers = [
                str(content.count("\n", 0, index) + 1) for index in starts[:5]
            ]
            return (
                f"REJECTED: the block appears {occurrences} times in '{path}' "
                f"(first matches near lines {', '.join(line_numbers)}). "
                "Make the block longer or unique, or use edit_range with explicit line numbers."
            )
        new_content = content.replace(search, replace, 1)
    else:
        match = _find_whitespace_tolerant_match(content, search)
        if match is None:
            outer_match = _find_outer_indent_match(content, search)
            if outer_match is not None:
                start, end, indent = outer_match
                replacement_lines = replace.splitlines(keepends=True)
                if replacement_lines and not replacement_lines[0].lstrip().startswith(('#', '```')):
                    replace = "".join(
                        (indent + line if line.strip() else line)
                        for line in replacement_lines
                    )
                match = (start, end)
        if match is None:
            hint = _find_closest_match_hint(content, search)
            return (
                f"ERROR: exact block was not found verbatim in '{path}' (checked exact match and "
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
