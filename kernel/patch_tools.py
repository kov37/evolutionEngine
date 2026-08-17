"""Atomic, multi-file patch primitive for the model-facing editor surface.

The model submits one bounded ``*** Begin Patch`` document.  The host parses
all affected paths, validates every change in memory, and only then writes the
files.  This keeps a cross-file edit from leaving half of the patch on disk
when one hunk is stale or one Python file becomes syntactically invalid.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from kernel.io_tools import validate_python_syntax
from kernel.sandbox import confine, get_root


MAX_PATCH_CHARS = 40_000
MAX_PATCH_FILES = 20


def _safe_relative(raw: str) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if value.startswith(("/", "~")):
        raise ValueError(f"absolute paths are not allowed: {raw}")
    if value.startswith(("a/", "b/")):
        value = value[2:]
    parts = Path(value).parts
    if not value or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"invalid workspace-relative path: {raw}")
    return Path(value).as_posix()


def patch_paths(patch: str) -> tuple[str, ...]:
    """Return normalized paths named by a patch, without touching disk."""
    paths = []
    for line in str(patch or "").splitlines():
        match = re.match(r"\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$", line)
        if match:
            path = _safe_relative(match.group(1))
            if path not in paths:
                paths.append(path)
    return tuple(paths)


def _apply_update(path: str, content: str, lines: list[str]) -> str:
    current = content.splitlines()
    original_ended = content.endswith(("\n", "\r"))
    index = 0
    search_from = 0
    saw_hunk = False
    while index < len(lines):
        header = lines[index]
        if not header.startswith("@@"):
            index += 1
            continue
        saw_hunk = True
        index += 1
        hunk = []
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line.startswith((" ", "+", "-")):
                hunk.append(line)
            elif line.strip() in {"\\ No newline at end of file", ""}:
                # Empty patch lines are valid context only when prefixed with
                # a space. Ignore a bare separator rather than guessing.
                pass
            else:
                raise ValueError(f"malformed hunk line for '{path}': {line}")
            index += 1
        old = [line[1:] for line in hunk if line.startswith((" ", "-"))]
        new = [line[1:] for line in hunk if line.startswith((" ", "+"))]
        if not old:
            raise ValueError(f"hunk for '{path}' has no context or removals")
        match_at = None
        for candidate in range(search_from, len(current) - len(old) + 1):
            if current[candidate:candidate + len(old)] == old:
                match_at = candidate
                break
        if match_at is None:
            raise ValueError(f"patch context was not found in '{path}'; reread the current file")
        current[match_at:match_at + len(old)] = new
        search_from = match_at + len(new)
    if not saw_hunk:
        raise ValueError(f"update for '{path}' contains no @@ hunk")
    rendered = "\n".join(current)
    return rendered + ("\n" if original_ended or current else "")


def _parse(patch: str) -> list[tuple[str, str, list[str]]]:
    text = str(patch or "")
    if len(text) > MAX_PATCH_CHARS:
        raise ValueError(f"patch is too large; maximum is {MAX_PATCH_CHARS} characters")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise ValueError("patch must start with '*** Begin Patch' and end with '*** End Patch'")
    operations = []
    index = 1
    while index < len(lines) - 1:
        match = re.match(r"\*\*\* (Update|Add|Delete) File: (.+?)\s*$", lines[index])
        if not match:
            raise ValueError(f"expected a file operation at patch line {index + 1}")
        operation, raw_path = match.groups()
        path = _safe_relative(raw_path)
        index += 1
        body = []
        while index < len(lines) - 1 and not lines[index].startswith("*** "):
            body.append(lines[index])
            index += 1
        operations.append((operation, path, body))
        if len(operations) > MAX_PATCH_FILES:
            raise ValueError(f"patch touches more than {MAX_PATCH_FILES} files")
    if not operations:
        raise ValueError("patch contains no file operations")
    return operations


def apply_patch(patch: str) -> str:
    """Apply one atomic multi-file patch inside the active workspace.

    Use this standard form:

    ``*** Begin Patch``
    ``*** Update File: src/module.py``
    ``@@``
    ``-old line``
    ``+new line``
    ``*** End Patch``

    Add multiple ``Update File`` blocks to edit a cross-file change together.
    Use ``Add File`` for new files. Existing files are never overwritten as a
    side effect of an ``Add File`` operation.
    """
    try:
        operations = _parse(patch)
        operation_paths = [path for _, path, _ in operations]
        if len(operation_paths) != len(set(operation_paths)):
            raise ValueError("each file may appear only once in an atomic patch")
        root = get_root()
        staged: dict[str, str | None] = {}
        for operation, path, body in operations:
            full = confine(path)
            exists = os.path.exists(full)
            if operation == "Add":
                if exists:
                    raise ValueError(f"cannot add existing file '{path}'; use Update File")
                content = "\n".join(line[1:] if line.startswith("+") else line for line in body)
                if content or body:
                    content += "\n"
            elif operation == "Delete":
                if not exists:
                    raise ValueError(f"cannot delete missing file '{path}'")
                content = None
            else:
                if not exists:
                    raise ValueError(f"cannot update missing file '{path}'; use Add File")
                with open(full, "r", encoding="utf-8") as stream:
                    before = stream.read()
                content = _apply_update(path, before, body)
            if content is not None:
                valid, error = validate_python_syntax(full, content)
                if not valid:
                    raise ValueError(error)
            staged[path] = content

        # All parsing, path checks, context checks, and Python syntax checks
        # passed. Write through temporary files, then replace each target.
        temporary: list[tuple[str, str]] = []
        try:
            for path, content in staged.items():
                if content is None:
                    continue
                full = confine(path)
                fd, temp_path = tempfile.mkstemp(prefix=".agent-patch-", dir=os.path.dirname(full))
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(content)
                temporary.append((temp_path, full))
            for temp_path, full in temporary:
                os.replace(temp_path, full)
            for path, content in staged.items():
                if content is None:
                    os.remove(confine(path))
        except Exception:
            for temp_path, _ in temporary:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise
        return "Applied atomic patch to: " + ", ".join(staged)
    except (OSError, TypeError, ValueError) as exc:
        return f"ERROR: apply_patch rejected: {exc}"
