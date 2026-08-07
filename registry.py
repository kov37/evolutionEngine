"""Merges the trusted kernel tools with model-authored tools that have
graduated from workspace/tools/ into one list ready for ollama.chat(tools=...).

A tool only appears here via promote() — being written to workspace/tools/
is not enough on its own. The promotion decision (does it pass its
curriculum scenario test?) belongs to harness.py, not this module.

Path confinement for graduated tools lives here too, not in the tools
themselves. A tool declares which of its parameters are filesystem paths
via `path_params` at promotion time; _load_graduated_tools wraps the
function so those parameters get sandboxed via kernel.sandbox.confine()
before the model-authored code ever sees them. This is trusted code
checking untrusted code's inputs, not untrusted code checking itself.

STATE_DIR is anchored to this file's own location, not os.getcwd() and not
kernel.sandbox's root — the manifest tracks evolutionEngine's own tool
registry, which must stay put regardless of what project agent.py is
currently pointed at via --project.
"""

import functools
import importlib.util
import json
import os

from kernel.io_tools import read_file, write_file, patch_file, list_workspace
from kernel.exec_tools import run_shell
from kernel.sandbox import confine

KERNEL_TOOLS = [read_file, write_file, patch_file, list_workspace, run_shell]

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
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


def _wrap_with_confinement(fn, path_params):
    """Sandbox the named kwargs via kernel.sandbox.confine() before calling
    fn. Resolved fresh per call, not at wrap time, against whatever root is
    currently set — the default workspace under harness.py, or wherever
    agent.py's --project pointed it."""
    if not path_params:
        return fn

    @functools.wraps(fn)
    def wrapped(**kwargs):
        for param in path_params:
            if param in kwargs and isinstance(kwargs[param], str):
                kwargs[param] = confine(kwargs[param])
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


def verify(module_path: str, function_name: str):
    """Import module_path and confirm function_name actually exists and is
    callable. Returns (ok, error). Read-only — does not touch the manifest,
    so harness.py can use this mid-loop to check a candidate before treating
    it as a real win, not just after the loop's already ended."""
    if not os.path.exists(module_path):
        return False, f"{module_path} does not exist"
    spec = importlib.util.spec_from_file_location(function_name, module_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        fn = getattr(module, function_name)
    except Exception as e:
        return False, f"could not import '{function_name}' from {module_path}: {e}"
    if not callable(fn):
        return False, f"'{function_name}' in {module_path} exists but is not callable"
    return True, None


def verify_and_promote(tool_name: str, module_path: str, function_name: str, description: str = "", path_params=None):
    """verify() a candidate, and if it passes, write it into the manifest."""
    ok, err = verify(module_path, function_name)
    if not ok:
        return False, err

    promote(tool_name, module_path, function_name, description, path_params)
    return True, None
