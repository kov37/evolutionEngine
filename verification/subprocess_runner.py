"""Runs entirely as a subprocess, invoked by registry.verify() — never
imported directly. Loads the candidate module and (optionally) checks its
signature and runs a named checker, all inside this separate process so
import-time side effects in the candidate never touch the trusted
orchestrator's memory or its already-loaded `registry` module.

Deliberately never adds the repo root to sys.path, so `import registry`
from candidate code fails here with ModuleNotFoundError — this is what
actually defeats the exploit in verification/test_bypasses.py scenario G
(a candidate whose import-time code does `import registry;
registry.promote(...)`). checkers.py is loaded as a flat sibling import
(`from checkers import CHECKERS`), which works because Python puts this
script's own directory on sys.path[0] automatically when run directly —
no repo-root exposure needed for that.

NOT a hard security boundary — see VERIFICATION_ASSESSMENT.md finding 3
and the module docstring in registry.py's SUBPROCESS_RUNNER usage. A
sufficiently deliberate candidate that hardcodes the real manifest's
absolute path could still reach it via plain file I/O, since this
subprocess runs as the same OS user with the same filesystem permissions.
Actual OS-level sandboxing (container, restricted user, chroot) is what
would close that; this closes the demonstrated exploit and every
equivalent "reach the trusted module via a normal import" attempt, plus
adds a hard timeout the old in-process exec_module() never had.
"""
import argparse
import importlib.util
import inspect
import json

from checkers import CHECKERS


def _emit(result: dict) -> None:
    print("RESULT_JSON: " + json.dumps(result))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--module-path", required=True)
    parser.add_argument("--function-name", required=True)
    parser.add_argument("--expected-params", default=None, help="JSON list, or omitted to skip the check")
    parser.add_argument("--checker-name", default=None, help="Key into CHECKERS, or omitted to skip the check")
    args = parser.parse_args()

    try:
        spec = importlib.util.spec_from_file_location(args.function_name, args.module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, args.function_name)
    except Exception as e:
        _emit({"ok": False, "error": f"could not import '{args.function_name}' from {args.module_path}: {e}"})
        return

    if not callable(fn):
        _emit({"ok": False, "error": f"'{args.function_name}' in {args.module_path} exists but is not callable"})
        return

    if args.expected_params is not None:
        expected = json.loads(args.expected_params)
        try:
            actual = list(inspect.signature(fn).parameters.keys())
        except (TypeError, ValueError) as e:
            _emit({"ok": False, "error": f"could not inspect the signature of '{args.function_name}': {e}"})
            return
        if actual != expected:
            _emit({"ok": False, "error": f"'{args.function_name}' has parameters {actual}, expected exactly {expected}"})
            return

    if args.checker_name:
        checker = CHECKERS.get(args.checker_name)
        if checker is None:
            _emit({"ok": False, "error": f"no checker registered for '{args.checker_name}'"})
            return
        passed, detail = checker(fn)
        if not passed:
            _emit({"ok": False, "error": f"independent behavioral checker failed: {detail}"})
            return

    _emit({"ok": True, "error": None})


if __name__ == "__main__":
    main()
