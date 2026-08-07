"""Merges the trusted kernel tools with model-authored tools that have
graduated from workspace/tools/ into one list ready for ollama.chat(tools=...).

A tool only appears here via promote() — being written to workspace/tools/
is not enough on its own. The promotion decision (does it pass its
curriculum scenario test?) belongs to harness.py, not this module.

Path confinement for graduated tools lives here too, not in the tools
themselves. A tool declares which of its parameters are filesystem paths
via `path_params` at promotion time; _load_graduated_tools wraps the
function so those parameters get sandboxed to os.getcwd() before the
model-authored code ever sees them. This is trusted code checking
untrusted code's inputs, not untrusted code checking itself.
"""

import functools
import importlib.util
import json
import os

from kernel.io_tools import read_file, write_file, patch_file, list_workspace
from kernel.exec_tools import run_shell

KERNEL_TOOLS = [read_file, write_file, patch_file, list_workspace, run_shell]

STATE_DIR = "./state"
MANIFEST_PATH = os.path.join(STATE_DIR, "registry_manifest.json")
os.makedirs(STATE_DIR, exist_ok=True)


def _load_manifest() -> dict:
    if not os.path.exists(MANIFEST_PATH):
        return {}
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_manifest(manifest: dict) -> None:
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def _confine(root: str, value: str) -> str:
    if not isinstance(value, str):
        return value
    resolved = os.path.realpath(value) if os.path.isabs(value) else os.path.realpath(os.path.join(root, value))
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(f"path escapes the sandbox root ({root}): {value}")
    return resolved


def _wrap_with_confinement(fn, path_params):
    """Sandbox the named kwargs to os.getcwd() before calling fn. Resolved
    fresh per call, not at wrap time, since the sandbox root is wherever the
    process happens to be running from when the tool call actually happens."""
    if not path_params:
        return fn

    @functools.wraps(fn)
    def wrapped(**kwargs):
        root = os.path.realpath(os.getcwd())
        for param in path_params:
            if param in kwargs:
                kwargs[param] = _confine(root, kwargs[param])
        return fn(**kwargs)

    return wrapped


def _load_graduated_tools(manifest: dict) -> list:
    tools = []
    for tool_name, entry in manifest.items():
        spec = importlib.util.spec_from_file_location(tool_name, entry["module_path"])
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            fn = getattr(module, entry["function_name"])
            tools.append(_wrap_with_confinement(fn, entry.get("path_params")))
        except Exception as e:
            print(f"⚠️  Skipping graduated tool '{tool_name}': failed to load ({e})")
    return tools


def load_registry() -> list:
    """Returns every callable — kernel plus graduated — to hand to ollama.chat."""
    return KERNEL_TOOLS + _load_graduated_tools(_load_manifest())


def is_promoted(tool_name: str) -> bool:
    return tool_name in _load_manifest()


def promote(tool_name: str, module_path: str, function_name: str, description: str = "", path_params=None) -> None:
    """Record a validated workspace/tools/*.py module as a permanent tool."""
    manifest = _load_manifest()
    manifest[tool_name] = {
        "module_path": module_path,
        "function_name": function_name,
        "description": description,
        "path_params": path_params or [],
    }
    _save_manifest(manifest)


def verify_and_promote(tool_name: str, module_path: str, function_name: str, description: str = "", path_params=None):
    """Import module_path and confirm function_name actually exists and is
    callable before writing it into the manifest. Returns (ok, error)."""
    spec = importlib.util.spec_from_file_location(tool_name, module_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        fn = getattr(module, function_name)
    except Exception as e:
        return False, f"could not import '{function_name}' from {module_path}: {e}"
    if not callable(fn):
        return False, f"'{function_name}' in {module_path} exists but is not callable"

    promote(tool_name, module_path, function_name, description, path_params)
    return True, None
