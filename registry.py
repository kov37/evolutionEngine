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
import subprocess
import sys

from kernel.io_tools import read_file, write_file, patch_file, list_workspace
from kernel.exec_tools import run_shell
from kernel.sandbox import confine

KERNEL_TOOLS = [read_file, write_file, patch_file, list_workspace, run_shell]

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
MANIFEST_PATH = os.path.join(STATE_DIR, "registry_manifest.json")
os.makedirs(STATE_DIR, exist_ok=True)

# verify() never imports a candidate directly into this process anymore —
# see VERIFICATION_ASSESSMENT.md finding 3 and subprocess_runner.py's own
# docstring for exactly what this does and doesn't defend against.
SUBPROCESS_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verification", "subprocess_runner.py")
CANDIDATE_TIMEOUT_SECONDS = 30


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
    """A tool only counts as promoted if its manifest entry still resolves
    to a real, loadable, callable function — not merely if the key exists.
    See VERIFICATION_ASSESSMENT.md finding 6: a missing or unloadable
    module used to remain classified as promoted forever."""
    manifest = _load_manifest()
    entry = manifest.get(tool_name)
    if entry is None:
        return False
    ok, _ = verify(entry["module_path"], entry["function_name"])
    return ok


def _check_name_collision(tool_name: str, function_name: str):
    """Reject a promotion whose function_name would collide with a trusted
    kernel tool or an already-promoted graduated tool. harness.py builds
    its dispatch map as {fn.__name__: fn for fn in tools}, with kernel
    tools listed first — an unchecked collision lets a later-loaded
    graduated tool silently replace a trusted kernel implementation in that
    map. See VERIFICATION_ASSESSMENT.md finding 6."""
    kernel_names = {fn.__name__ for fn in KERNEL_TOOLS}
    if function_name in kernel_names:
        return f"function_name '{function_name}' collides with a kernel tool — promotion rejected"

    manifest = _load_manifest()
    for existing_tool_name, entry in manifest.items():
        if existing_tool_name == tool_name:
            continue  # re-promoting the same curriculum entry (e.g. a fixed version) is fine
        if entry["function_name"] == function_name:
            return (
                f"function_name '{function_name}' collides with already-promoted tool "
                f"'{existing_tool_name}' — promotion rejected"
            )
    return None


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


def verify(module_path: str, function_name: str, expected_params=None, checker_name=None, expected_filename=None):
    """Confirm function_name exists, is callable, and — when the caller
    supplies them — matches an expected parameter list and passes an
    independent behavioral checker. Returns (ok, error). Read-only — does
    not touch the manifest, so harness.py can use this mid-loop to check a
    candidate before treating it as a real win, and is_promoted() can use
    it to re-validate an existing entry.

    The actual import + signature check + checker call all happen in a
    fresh subprocess (verification/subprocess_runner.py), never in this
    process — see that module's docstring for exactly what that does and
    doesn't defend against. checker_name is a key into
    verification.checkers.CHECKERS (a string, not a live function object):
    a function reference can't cross the subprocess boundary, and passing
    a name instead means the subprocess looks the checker up from its own
    trusted copy of checkers.py rather than receiving anything from this
    process that a compromised candidate could have already touched.

    expected_params/checker_name/expected_filename are optional (None
    skips that check) so this stays usable for ad hoc verification without
    a full curriculum entry on hand — but curriculum-driven graduation
    always supplies all three. See VERIFICATION_ASSESSMENT.md findings 1,
    2, 3, 6: "exists, imports, and is callable" — checked by directly
    importing the candidate into this trusted process — was previously the
    entire bar for promotion, and the candidate's own self-test was the
    only behavioral evidence ever consulted.
    """
    if not os.path.exists(module_path):
        return False, f"{module_path} does not exist"

    if expected_filename and os.path.basename(module_path) != expected_filename:
        return False, (
            f"expected the candidate to be named '{expected_filename}', got "
            f"'{os.path.basename(module_path)}' — promotion requires the curriculum-declared "
            f"filename, not whichever file most recently happened to run cleanly"
        )

    cmd = [sys.executable, SUBPROCESS_RUNNER, "--module-path", module_path, "--function-name", function_name]
    if expected_params is not None:
        cmd += ["--expected-params", json.dumps(expected_params)]
    if checker_name:
        cmd += ["--checker-name", checker_name]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CANDIDATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, f"candidate verification timed out after {CANDIDATE_TIMEOUT_SECONDS}s — likely hung"

    result = None
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT_JSON: "):
            result = json.loads(line[len("RESULT_JSON: "):])
    if result is None:
        return False, (
            f"candidate verification subprocess produced no result (exit {proc.returncode}); "
            f"stderr: {proc.stderr[-500:]}"
        )

    return result["ok"], result.get("error")


def verify_and_promote(tool_name: str, module_path: str, function_name: str, description: str = "",
                        path_params=None, expected_params=None, checker_name=None, expected_filename=None):
    """verify() a candidate against every check the caller supplies, reject
    on any kernel/graduated-tool name collision, and only then write it
    into the manifest."""
    ok, err = verify(module_path, function_name, expected_params, checker_name, expected_filename)
    if not ok:
        return False, err

    collision_err = _check_name_collision(tool_name, function_name)
    if collision_err:
        return False, collision_err

    promote(tool_name, module_path, function_name, description, path_params)
    return True, None
