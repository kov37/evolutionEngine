"""Kernel tier: the single source of truth for where the toolbelt is
allowed to touch the filesystem.

Every path-taking tool — kernel or graduated — resolves through confine()
here, against whatever root is currently set. This replaces what used to be
three separate, slightly different mechanisms: a hardcoded WORKSPACE_DIR in
kernel/io_tools.py, a duplicate hardcoded WORKSPACE_DIR in
kernel/exec_tools.py, and os.getcwd() in registry.py's confinement wrapper.

set_root() is NOT exposed as a tool and must never be. It's called by an
orchestrator (agent.py's --project flag; harness.py never calls it, so it
always sees the default) before the tool list is built. If the model could
call this itself, it could redirect its own sandbox anywhere — that
defeats the entire point of confinement.
"""

import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_ROOT = os.path.join(_REPO_ROOT, "workspace")

_state = {"root": _DEFAULT_ROOT}
os.makedirs(_state["root"], exist_ok=True)


def set_root(path: str) -> None:
    """Point the entire toolbelt at `path`. Orchestrator-only."""
    resolved = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(resolved):
        raise NotADirectoryError(f"project root does not exist or is not a directory: {path}")
    _state["root"] = resolved


def get_root() -> str:
    return _state["root"]


def confine(path: str) -> str:
    """Resolve `path` against the current root; raise ValueError if it
    escapes. Subdirectories are allowed — only genuine escapes are rejected."""
    root = _state["root"]
    resolved = os.path.realpath(path) if os.path.isabs(path) else os.path.realpath(os.path.join(root, path))
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(f"path escapes the sandbox root ({root}): {path}")
    return resolved
