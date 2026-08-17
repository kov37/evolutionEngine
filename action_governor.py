"""Piece 1 of a general progress-governance system (design from a pasted
proposal, tonight's session): classify every tool call into a capability
class (OBSERVE/PLAN/MUTATE/VALIDATE/DELIVER) and track OBSERVE/VALIDATE
"evidence" by content fingerprint, so a repeated call that returns
byte-identical content to something already seen is recognized as
producing ZERO new evidence — independent of tool name or arguments.

This is the foundation later pieces (task contracts, adaptive budgets,
graduated escalation) build on, and it's deliberately built and verified in
isolation first: pure deterministic bookkeeping, no model call, no
integration into agent.py's control flow yet. Matches this project's own
established pattern all night — verify a mechanism against real, already-
observed data before trusting it to act on anything live.

Real motivating case (naive-baseline-verified-35b, run 10, tonight):
read_file(offset=1) was called at iterations 1, 5, and 11, against a file
that was never modified in between — three calls, one genuinely new piece
of evidence. A tool-name-only heuristic ("don't call read_file twice")
can't distinguish that from "read_file called on a file that DID change in
between" (legitimate, real new evidence) — content fingerprinting can.

run_shell is classified by content, not name — it was observed live
(runs 7/8) being used as a workaround to reimplement read_file/grep_dir
(`cat -n`, `sed -n`, `grep -n`) once those tools were removed by the
now-reverted forced-edit gating. A name-only classifier would have missed
that entirely and scored those turns as "progress" just because the tool
name differed.
"""

import hashlib
import re
import shlex

from lifecycle_policy import is_output_only_command

CAPABILITY_CLASS = {
    "read_file": "OBSERVE",
    "grep_dir": "OBSERVE",
    "list_dir": "OBSERVE",
    "list_symbols": "OBSERVE",
    "list_workspace": "OBSERVE",
    "search_file": "OBSERVE",
    "git_status": "OBSERVE",
    "git_diff": "OBSERVE",
    "diff_files": "OBSERVE",
    "web_search": "OBSERVE",
    "fetch": "OBSERVE",
    "find_files": "OBSERVE",
    "recall": "OBSERVE",
    "patch_file": "MUTATE",
    "apply_patch": "MUTATE",
    "write_file": "MUTATE",
    "patch_product_file": "MUTATE",
    "write_product_file": "MUTATE",
    "run_tests": "VALIDATE",
    "finish_task": "DELIVER",
}

_SHELL_VALIDATE_RE = re.compile(
    r"\b(pytest|py\.test|python3?\s+-m\s+pytest|unittest|"
    r"assert(?:ion)?\s+|curl|wget|urllib|fetch|websocket|health)\b",
    re.IGNORECASE,
)
_ASSERTIVE_CHECK_RE = re.compile(
    r"\b(assert|check|verify|test|pytest|unittest|curl|wget|http|urllib|health|status|exit\s+code)\b|"
    r"(?:^|[\s/])test[_-][^\s/]+",
    re.IGNORECASE,
)
# (?!&) excludes fd-to-fd redirects (2>&1, 1>&2) — extremely common in any
# shell command that wants combined stdout/stderr, NOT a file write. Found
# live: a real `python3 -m pytest ... 2>&1 | head -40` VALIDATE command was
# misclassified as MUTATE because the old pattern only excluded `>=`, not
# `>&` — and since classify_run_shell checks MUTATE before VALIDATE, that
# false match pre-empted the correct pytest-based VALIDATE classification
# entirely for every command piping stderr to stdout, which is most of them.
#
# (?!\s*/dev/null) excludes the sibling case, found live in Run 16
# (working_state_enabled): `find ... -name "..." 2>/dev/null | head -10` —
# a completely read-only command that just discards stderr — was
# misclassified as MUTATE. That single false positive satisfied
# task_contract's MUTATE requirement (capabilities_seen now contained
# "MUTATE") despite ZERO real edits ever happening, which made level 4's
# build_intervention treat MUTATE as already-satisfied and offer only
# VALIDATE/DELIVER tools — excluding patch_file/write_file entirely for a
# code_change task that hadn't written anything yet. Writing to /dev/null
# can never be a real mutation of anything the task cares about, so it's
# safe to exclude unconditionally.
_SHELL_MUTATE_RE = re.compile(
    r"(?:sed\s+-i|\bcp\b|\bmv\b|\btouch\b|\brm\b|"
    r"\btee\b|perl\s+[^\n]*-(?:p?i|in-place)\b|"
    r"(?:writefilesync|appendfilesync|unlinksync|mkdirsync|"
    r"(?:fs\.)?promises?\.(?:writefile|appendfile|unlink|mkdir))|"
    r"[\"'](?:writefilesync|appendfilesync|writefile|appendfile|unlink|mkdir)[\"']\s*\]|"
    r"(?:fs\.)?(?:writefile|appendfile)\s*\(|\.write_text\s*\(|\.write\s*\()",
    re.IGNORECASE,
)


def _has_shell_file_redirect(command: str) -> bool:
    """Detect real shell redirects without scanning quoted program text.

    A raw ``>`` search mistakes Python comparisons, HTML strings, and printed
    arrows for redirects. ``shlex`` with shell punctuation preserves quoted
    interpreter code as one token while exposing operators such as ``>`` and
    ``>>``. File writes to /dev/null and fd-to-fd redirects remain harmless.
    """
    try:
        lexer = shlex.shlex(str(command or ""), posix=True, punctuation_chars="><|;&")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    for index, token in enumerate(tokens[:-1]):
        if token not in {">", ">>"}:
            continue
        previous = tokens[index - 1] if index else ""
        target = tokens[index + 1]
        if previous in {"=", "-"} or target == "/dev/null":
            continue
        return True
    return False


def _shell_wrapper_body(command: str):
    """Return the body of a quoted ``sh -c``/``bash -c`` command, if any."""
    if isinstance(command, (list, tuple)):
        argv = [str(part) for part in command]
    else:
        try:
            argv = shlex.split(str(command or ""))
        except ValueError:
            return None
    if not argv or argv[0].rsplit("/", 1)[-1].lower() not in {"sh", "bash", "zsh", "fish"}:
        return None
    try:
        index = argv.index("-c")
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


_INTERPRETER_WRITE_RE = re.compile(
    r"\bopen\s*\([^)]*,\s*['\"][wax+]",
    re.IGNORECASE,
)


def _file_install_command(command: str) -> bool:
    """Recognize Unix's file-copying `install`, not npm/pip install."""
    try:
        argv = shlex.split(str(command or ""))
    except ValueError:
        return False
    return bool(argv) and argv[0].rsplit("/", 1)[-1].lower() == "install"


def _inline_validation(argv) -> bool:
    """Recognize an assertion in interpreter code without trusting prose.

    This is narrower than the command-wide regex: a real inline assertion is
    validation, while ``print('assert passed')`` is handled as output-only
    before this function is reached.
    """
    tokens = [str(token) for token in (argv or [])]
    if not tokens or tokens[0].rsplit("/", 1)[-1].lower() not in {
        "python", "python3", "node", "nodejs", "bun", "deno", "ruby", "perl", "php",
    }:
        return False
    try:
        code_index = next(i for i, token in enumerate(tokens[1:], 1) if token in {"-e", "-c"})
    except StopIteration:
        return False
    code = " ".join(tokens[code_index + 1:])
    return bool(re.search(r"(?:^|[;\n])\s*assert(?:ion)?\b|\b(?:pytest|unittest)\b", code, re.I))


def classify_run_shell(command: str) -> str:
    """run_shell is a wildcard tool — classify by what the command actually
    does, not the tool name. Defaults to OBSERVE for anything ambiguous:
    conservative on purpose, since silently treating an ambiguous action as
    "progress" when it's really just another read is the exact failure
    mode this module exists to catch."""
    if is_output_only_command(command):
        return "OBSERVE"
    wrapped = _shell_wrapper_body(command)
    if wrapped is not None:
        return classify_run_shell(wrapped)
    if _file_install_command(command):
        return "MUTATE"
    if _has_shell_file_redirect(command) or _SHELL_MUTATE_RE.search(command):
        return "MUTATE"
    if _SHELL_VALIDATE_RE.search(command):
        return "VALIDATE"
    return "OBSERVE"


def classify(tool_name: str, arguments: dict) -> str:
    if tool_name == "run_shell":
        return classify_run_shell((arguments or {}).get("command", ""))
    if tool_name == "run_command":
        argv = (arguments or {}).get("command", [])
        if isinstance(argv, list):
            argv = [str(part) for part in argv]
        elif isinstance(argv, str):
            try:
                argv = shlex.split(argv)
            except ValueError:
                argv = [argv]
        else:
            argv = []
        command_text = " ".join(argv)
        # Preserve argv boundaries here. Flattening ``python -c
        # "print('assert passed')"`` before classification makes quoted fake
        # evidence look like an assertion token.
        if is_output_only_command(argv):
            return "OBSERVE"
        if _file_install_command(command_text):
            return "MUTATE"
        head = argv[0].rsplit("/", 1)[-1].lower() if argv else ""
        if head in {"sh", "bash", "zsh", "fish"}:
            wrapped = _shell_wrapper_body(argv)
            if wrapped is not None:
                return classify_run_shell(wrapped)
        if (head in {"python", "python3", "node", "nodejs", "ruby", "perl"}
                and _INTERPRETER_WRITE_RE.search(command_text)):
            return "MUTATE"
        if _SHELL_MUTATE_RE.search(command_text):
            return "MUTATE"
        if _inline_validation(argv) or _SHELL_VALIDATE_RE.search(command_text):
            return "VALIDATE"
        return "OBSERVE"
    return CAPABILITY_CLASS.get(tool_name, "OBSERVE")


def fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def infer_success(capability: str, tool_name: str, result_content: str):
    """Best-effort success/failure inference for MUTATE and VALIDATE calls
    — used by adaptive_budget.py's failure-recovery credit (a failed
    mutation or test run legitimately justifies more re-observation before
    the next attempt, not avoidance). Returns True/False, or None when
    success/failure isn't a meaningful concept for the call (OBSERVE, PLAN,
    DELIVER).

    Reuses each tool's own REAL return-string convention rather than
    guessing: kernel/io_tools.py's patch_file/write_file return an
    "ERROR:"/"REJECTED" prefix on failure (same check structured_state.py's
    _edit_succeeded already uses for EditsSucceeded/EditsFailed);
    run_tests returns a (bool, str) tuple that dispatch.py stringifies via
    str(result), so real success looks like "(True, ...)"; run_shell's
    real format (kernel/exec_tools.py) is "Exit code: N\\nSTDOUT:...".
    """
    if capability == "MUTATE":
        return not (result_content.startswith("ERROR:") or result_content.startswith("REJECTED"))
    if capability == "VALIDATE":
        if tool_name == "run_tests":
            return result_content.startswith("(True,")
        if tool_name in {"run_shell", "run_command"}:
            return "Exit code: 0" in result_content
    return None


def is_substantive_validation(tool_name: str, arguments: dict, result_content: str) -> bool:
    """Return whether a successful call contains executable evidence.

    A clean process exit is not enough: starting a server, printing a file,
    or compiling a module can all return zero while the requested behavior is
    wrong. The rule is deliberately task/model-neutral. It accepts a test
    runner, or a command whose arguments/output show an explicit check, and
    still requires the underlying command to succeed.
    """
    if result_content.startswith(("ERROR:", "REJECTED:")):
        return False
    if tool_name == "run_tests":
        return result_content.startswith("(True,")
    if tool_name not in {"run_command", "run_shell"}:
        return False
    if "Exit code: 0" not in result_content:
        return False
    command = (arguments or {}).get("command", "")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if is_output_only_command(str(command)):
        return False
    return bool(_ASSERTIVE_CHECK_RE.search(str(command)) or
                _ASSERTIVE_CHECK_RE.search(result_content))


def result_is_failure(result_content: str) -> bool:
    """Whether a tool result is an execution/argument failure."""
    return result_content.startswith(("ERROR:", "REJECTED:"))


def is_blocked_response(result_content: str) -> bool:
    """True if `result_content` is dispatch.py's real enforcement-rejection
    message (allowed_names blocked this call), not a real tool result.
    Matters because the ORIGINAL call attempted a real tool — e.g. a
    blocked read_file still has tool_name="read_file" — so without this
    check, a blocked call would be classified and fingerprinted exactly
    like a real one, and (since each block message's text differs by
    tool/arguments) would never even register as a duplicate: a real bug
    caught live, where a blocked read_file was silently counted as
    genuine new OBSERVE evidence in the ledger."""
    return result_content.startswith("ERROR: '") and "is unavailable this turn" in result_content


class EvidenceLedger:
    """Tracks OBSERVE/VALIDATE evidence by content fingerprint. MUTATE and
    DELIVER calls are recorded for history but never flagged as duplicates
    of each other or of anything else — every real mutation changes state,
    so there's no meaningful "byte-identical to a prior mutation" case to
    detect the way there is for a read or a test run."""

    def __init__(self):
        self.seen_fingerprints = set()
        self.history = []  # list of dicts, one per tool call

    def record(self, turn: int, tool_name: str, arguments: dict, result_content: str) -> dict:
        if is_blocked_response(result_content):
            # The call never really happened — dispatch.py's allowed_names
            # enforcement rejected it before the real tool ran. Its own
            # distinct classification (not OBSERVE/MUTATE/VALIDATE/DELIVER)
            # means it can never satisfy a task_contract requirement, never
            # counts toward new_evidence_count, and — via recent_progress's
            # explicit capability check below — never resets the
            # stagnation clock either. See is_blocked_response's docstring
            # for the real bug this fixes.
            entry = {
                "turn": turn, "tool_name": tool_name, "capability": "BLOCKED",
                "fingerprint": None, "is_duplicate": False, "succeeded": None,
                "failed_observation": True, "scope": None,
            }
            self.history.append(entry)
            return entry

        capability = classify(tool_name, arguments)
        fp = fingerprint(result_content) if capability in ("OBSERVE", "VALIDATE") else None
        is_duplicate = fp is not None and fp in self.seen_fingerprints
        if fp is not None:
            self.seen_fingerprints.add(fp)
        failed_observation = result_is_failure(result_content)
        scope = (arguments or {}).get("path") or (arguments or {}).get("command") or (arguments or {}).get("pattern")
        entry = {
            "turn": turn,
            "tool_name": tool_name,
            "capability": capability,
            "fingerprint": fp,
            "is_duplicate": is_duplicate,
            "succeeded": infer_success(capability, tool_name, result_content),
            "failed_observation": failed_observation,
            "scope": scope,
        }
        self.history.append(entry)
        return entry

    def failed_action_count(self) -> int:
        """MUTATE/VALIDATE calls that explicitly failed — adaptive_budget's
        failure-recovery credit input."""
        return sum(1 for e in self.history if e["succeeded"] is False)

    def distinct_scopes_touched(self) -> int:
        """How many distinct files/paths have been the target of an OBSERVE
        or MUTATE call — adaptive_budget's repository-size-factor input.
        Deliberately crude (a real path string, not true file size or
        project scale), but grounded in what's actually available here
        without adding a new dependency."""
        return len({e["scope"] for e in self.history if e["scope"] and e["capability"] in ("OBSERVE", "MUTATE")})

    def recent_progress(self, window: int = 5) -> bool:
        """True if any of the most recent `window` recorded actions
        produced new (non-duplicate) evidence, or was a MUTATE/DELIVER.
        False means the last `window` actions were pure re-observation of
        things already known (or were blocked outright) — real stagnation,
        not just caution.

        Explicitly requires OBSERVE/VALIDATE for the "not duplicate" branch
        rather than checking is_duplicate alone — a BLOCKED entry always
        has is_duplicate=False by construction (no fingerprint is ever
        computed for it), so without this explicit capability check a
        blocked call would incorrectly count as progress and reset the
        stagnation clock, the same real bug is_blocked_response's docstring
        describes for new_evidence_count."""
        recent = self.history[-window:]
        if len(recent) < window:
            return True  # not enough history yet to call it stagnant
        return any(
            e["capability"] in ("MUTATE", "DELIVER")
            or (e["capability"] in ("OBSERVE", "VALIDATE") and
                not e["is_duplicate"] and not e["failed_observation"])
            for e in recent
        )

    def capabilities_seen(self) -> set:
        """Every capability class that has occurred at least once —
        task_contract.missing_required_capabilities' input."""
        return {e["capability"] for e in self.history}

    def new_evidence_count(self) -> int:
        """How many OBSERVE/VALIDATE calls produced genuinely new
        (non-duplicate) evidence — the "has enough been learned to act"
        signal, independent of raw call count (which counts re-reads too)."""
        return sum(
            1 for e in self.history
            if e["capability"] in ("OBSERVE", "VALIDATE") and
            not e["is_duplicate"] and not e["failed_observation"]
        )


def _self_test() -> bool:
    # --- is_blocked_response() / EvidenceLedger blocked-call handling ---
    # Real bug caught live tonight: a genuinely blocked read_file (level-4
    # tool restriction active) was recorded as if it were a real, new
    # OBSERVE evidence entry — dispatch.py's real block message, verbatim
    # format, replayed here.
    real_block_msg = (
        "ERROR: 'read_file' is unavailable this turn — only ['finish_task', 'patch_file', 'recall', "
        "'run_tests', 'write_file'] are allowed right now. Use one of those instead."
    )
    assert is_blocked_response(real_block_msg) is True
    assert is_blocked_response("ERROR: search text was not found verbatim in 'x.py'.") is False, (
        "a real patch_file failure must NOT be misidentified as a block"
    )
    assert is_blocked_response("some normal file content") is False

    ledger_blocked = EvidenceLedger()
    blocked_entry = ledger_blocked.record(1, "read_file", {"path": "crv_types.py"}, real_block_msg)
    assert blocked_entry["capability"] == "BLOCKED"
    assert blocked_entry["is_duplicate"] is False
    assert ledger_blocked.new_evidence_count() == 0, "a blocked call must never count as new evidence"
    assert "OBSERVE" not in ledger_blocked.capabilities_seen(), "a blocked read_file must not satisfy an OBSERVE requirement"

    # Multiple blocked calls (even with different args, so different text)
    # in a row must NOT reset the stagnation clock — this is the exact
    # scenario that would have hidden real stagnation behind "recent_
    # progress=True" before the fix.
    ledger_blocked2 = EvidenceLedger()
    for i in range(5):
        ledger_blocked2.record(i, "read_file", {"path": f"f{i}.py"}, real_block_msg)
    assert ledger_blocked2.recent_progress(window=5) is False, (
        "5 blocked calls in a row must be real stagnation, not falsely reset by is_duplicate=False"
    )

    # --- infer_success() ---
    assert infer_success("MUTATE", "patch_file", "Wrote 'x.py' (100 bytes).") is True
    assert infer_success("MUTATE", "patch_file", "ERROR: search text was not found verbatim.") is False
    assert infer_success("MUTATE", "write_file", "REJECTED (invalid syntax, nothing written): ...") is False
    assert infer_success("VALIDATE", "run_tests", "(True, 'Ran 3 tests OK')") is True
    assert infer_success("VALIDATE", "run_tests", "(False, '1 failure')") is False
    assert infer_success("VALIDATE", "run_shell", "Exit code: 0\nSTDOUT:\nok\nSTDERR:\n") is True
    assert infer_success("VALIDATE", "run_shell", "Exit code: 1\nSTDOUT:\n\nSTDERR:\nFAILED") is False
    assert infer_success("OBSERVE", "read_file", "some file content") is None, "success is meaningless for OBSERVE"

    # A syntactically different failed observation is still not progress: it
    # must not grant the model an unlimited discovery budget.
    ledger_failed_observe = EvidenceLedger()
    for i in range(5):
        ledger_failed_observe.record(i, "list_dir", {"path": f"missing-{i}"}, "ERROR: missing")
    assert ledger_failed_observe.new_evidence_count() == 0
    assert ledger_failed_observe.recent_progress(window=5) is False

    # --- EvidenceLedger.failed_action_count() ---
    ledger0 = EvidenceLedger()
    ledger0.record(1, "patch_file", {}, "Wrote 'x.py' (10 bytes).")
    ledger0.record(2, "patch_file", {}, "ERROR: search text was not found verbatim.")
    ledger0.record(3, "run_shell", {"command": "python3 -m pytest x.py"}, "Exit code: 1\nSTDOUT:\nSTDERR:\nFAILED")
    ledger0.record(4, "read_file", {}, "irrelevant content")  # succeeded=None, must not count as failed
    assert ledger0.failed_action_count() == 2, f"expected 2 real failures, got {ledger0.failed_action_count()}"

    # --- classify() ---
    assert classify("read_file", {}) == "OBSERVE"
    assert classify("patch_file", {}) == "MUTATE"
    assert classify("patch_product_file", {}) == "MUTATE"
    assert classify("write_product_file", {}) == "MUTATE"
    assert classify("finish_task", {}) == "DELIVER"
    assert classify("run_shell", {"command": "cd x && python3 -m pytest test_foo.py"}) == "VALIDATE"
    assert classify("run_shell", {"command": "cat -n crv_types.py | sed -n '1,50p'"}) == "OBSERVE"
    assert classify("run_shell", {"command": "grep -n 'def _cdf' crv_types.py"}) == "OBSERVE"
    assert classify("run_shell", {"command": "echo 'x' >> crv_types.py"}) == "MUTATE"
    # Real bug caught live tonight: 2>&1 (fd-to-fd stderr redirect, present
    # in nearly every real shell command that wants combined output) must
    # NOT be classified as MUTATE — only a redirect to an actual filename
    # target should be.
    assert classify("run_shell", {"command": "python3 -m pytest test_x.py -x 2>&1 | head -40"}) == "VALIDATE", (
        "2>&1 must not be misclassified as a file-write MUTATE"
    )
    assert classify("run_shell", {"command": "cmd 1>&2"}) == "OBSERVE", "1>&2 is also fd-to-fd, not a file write"
    assert classify("run_shell", {"command": "cmd 2> error.log"}) == "MUTATE", (
        "a REAL redirect to a filename (not &N) must still be classified as MUTATE"
    )
    # Real bug caught live (Run 16, working_state_enabled): a completely
    # read-only `find` piped through `head`, discarding stderr to /dev/null,
    # was misclassified as MUTATE — the sibling case to 2>&1 above. That
    # single false positive satisfied task_contract's MUTATE requirement
    # with zero real edits ever happening, causing level 4 to offer only
    # VALIDATE/DELIVER tools (excluding patch_file/write_file) on a
    # code_change task that hadn't written anything yet.
    assert classify("run_shell", {"command": 'find /tmp -name "test_continuous*" 2>/dev/null | head -10'}) == "OBSERVE", (
        "redirecting stderr to /dev/null must not be misclassified as a file-write MUTATE"
    )
    assert classify("run_shell", {"command": "cmd 2> /dev/null"}) == "OBSERVE", "same case with a space before /dev/null"
    assert classify("run_shell", {"command": "cmd > /dev/null 2>&1"}) == "OBSERVE", (
        "the common '> /dev/null 2>&1' idiom must not be misclassified either"
    )
    assert classify("unknown_tool_name", {}) == "OBSERVE"

    # --- EvidenceLedger: the real run-10 case, replayed synthetically ---
    # read_file(offset=1) three times against an UNCHANGED file — same
    # content each time, so entries 2 and 3 must be flagged duplicate.
    ledger = EvidenceLedger()
    same_content = "class ArcsinDistribution:\n    def pdf(self, x):\n        return 1\n"
    e1 = ledger.record(1, "read_file", {"path": "crv_types.py", "offset": 1}, same_content)
    e2 = ledger.record(5, "read_file", {"path": "crv_types.py", "offset": 1}, same_content)
    e3 = ledger.record(11, "read_file", {"path": "crv_types.py", "offset": 1}, same_content)
    assert e1["is_duplicate"] is False, "first observation of anything is never a duplicate"
    assert e2["is_duplicate"] is True, "byte-identical re-read must be flagged"
    assert e3["is_duplicate"] is True, "byte-identical re-read must be flagged"

    # A read AFTER the file genuinely changed (different content) must NOT
    # be flagged — real new evidence, not repetition.
    changed_content = same_content + "\n    def _cdf(self, x):\n        return 1\n"
    e4 = ledger.record(15, "read_file", {"path": "crv_types.py", "offset": 1}, changed_content)
    assert e4["is_duplicate"] is False, "content actually changed — must count as new evidence"

    # recent_progress(): 3 duplicate reads in a row (below window=5, so
    # "not enough history" still returns True — don't flag prematurely).
    ledger2 = EvidenceLedger()
    for i in range(3):
        ledger2.record(i, "read_file", {"path": "x.py"}, "same content")
    assert ledger2.recent_progress(window=5) is True, "fewer than `window` actions must not be flagged stagnant"

    # 5 duplicate reads in a row (== window) — genuinely stagnant. The
    # seeding first-observation is OUTSIDE the checked window (window only
    # looks at the last 5 entries), so all 5 in-window entries here are
    # real duplicates, not a mix.
    ledger3 = EvidenceLedger()
    ledger3.record(0, "read_file", {"path": "x.py"}, "same content")  # seed, outside the window
    for i in range(1, 6):
        ledger3.record(i, "read_file", {"path": "x.py"}, "same content")  # 5 duplicates, this is the window
    assert ledger3.recent_progress(window=5) is False, "5 non-new observations in a row is real stagnation"

    # A MUTATE anywhere in the window counts as progress, even surrounded
    # by duplicate reads — matches the design principle that mutation
    # always changes state regardless of what else happened.
    ledger4 = EvidenceLedger()
    ledger4.record(0, "read_file", {"path": "x.py"}, "same content")
    ledger4.record(1, "patch_file", {"path": "x.py"}, "Wrote 'x.py'.")
    for i in range(2, 5):
        ledger4.record(i, "read_file", {"path": "x.py"}, "same content")
    assert ledger4.recent_progress(window=5) is True, "a MUTATE in the window means it's not stagnant"

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   action_governor (classify + EvidenceLedger, deterministic)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
