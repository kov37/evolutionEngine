"""Kernel tier: hand-written, trusted file I/O primitives.

These are the only tools available before anything else exists — the model
cannot bootstrap a single file without write_file, or safely edit one
without read_file/patch_file. Nothing here is model-authored, and this
module should stay small and easy to audit by eye.
"""

import ast
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


def read_file(path: str) -> str:
    """Read and return the exact current contents of a file in the workspace.
    Always check this before patch_file — its search text must match verbatim.

    Args:
      path: Filename to read, e.g. 'patch_validator.py'.
    """
    full_path = _resolve(path)
    if not os.path.exists(full_path):
        return f"ERROR: '{path}' does not exist in the workspace."
    with open(full_path, "r", encoding="utf-8") as f:
        content = f.read()
    return f"--- {path} ({len(content)} chars) ---\n{content}"


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


def patch_file(path: str, search: str, replace: str) -> str:
    """Surgically replace one exact substring inside a file that already
    exists in the workspace. Use this for small, targeted edits instead of
    rewriting the whole file. Call read_file first to see the exact current
    text — the search string must match verbatim, including whitespace.

    Args:
      path: Filename to patch, e.g. 'patch_validator.py'. Must already exist.
      search: Exact text to find, including whitespace — must match verbatim.
      replace: Text to substitute in place of the search match.
    """
    full_path = _resolve(path)
    if not os.path.exists(full_path):
        return f"ERROR: '{path}' does not exist yet — use write_file to create it first."

    with open(full_path, "r", encoding="utf-8") as f:
        content = f.read()

    search = _strip_code_fences(search)
    replace = _strip_code_fences(replace)

    if search not in content:
        return f"ERROR: search text was not found verbatim in '{path}'. Call read_file to see the exact current contents, then retry."

    new_content = content.replace(search, replace, 1)
    valid, err = validate_python_syntax(full_path, new_content)
    if not valid:
        return f"REJECTED (invalid syntax, nothing written): {err}"

    with open(full_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return _test_after_write(full_path)
