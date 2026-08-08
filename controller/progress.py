"""Cross-subgoal stagnation detector — layers on top of, does not replace,
agent.py's existing per-turn repetition/confidence-checkpoint/watchdog
mechanisms. Those fire on "no successful WRITE" and were built for, and
tested against, single-symbol stalling. This fires on "no successful
ANYTHING" — not even a new read, a declared subgoal, or a resolved
hypothesis — a more severe, cross-subgoal stall, and fires earlier,
pushing toward re-planning rather than "just write something."
"""

from memory.events import read_events

STAGNATION_TURNS_THRESHOLD = 15


def last_progress_iteration(run_dir: str) -> int:
    """Highest iteration at which any successful tool call happened —
    including subgoal_create/hypothesis_record, unlike
    controller/subgoals.py's has_real_progress_since (which deliberately
    excludes those when gating one SPECIFIC subgoal's own completion — a
    subgoal can't be evidence for itself). Here, declaring a new subgoal
    or hypothesis IS real forward progress at the planning level."""
    last = 0
    for record in read_events(run_dir):
        if record.get("event_type") != "tool_call":
            continue
        if record["payload"].get("result_preview", "").startswith(("ERROR", "REJECTED")):
            continue
        last = max(last, record.get("iteration") or 0)
    return last


def stagnation_nudge(run_dir: str, current_iteration: int, threshold: int = STAGNATION_TURNS_THRESHOLD):
    """None if there's been real progress recently enough. Otherwise a
    message to inject into the conversation."""
    turns_stalled = current_iteration - last_progress_iteration(run_dir)
    if turns_stalled < threshold:
        return None
    return (
        f"STOP. {turns_stalled} turns have passed with no new recorded observation — not a read, a write, "
        f"a test run, a declared subgoal, or a resolved hypothesis. Whatever you're doing isn't producing "
        f"anything checkable. Either call subgoal_create to state a concrete next step and immediately act "
        f"on it, or read something you haven't read yet, or run a test. Do not just explain your reasoning again."
    )


def detect_patch_reversion(run_dir: str, path: str, search: str, replace: str):
    """Event_id of an earlier SUCCESSFUL patch_file call on the same path
    that this one exactly undoes (this call's search == that one's
    replace, and this call's replace == that one's search) — or None.

    A live run surfaced exactly this: the model correctly patched a bug
    fix, then one patch later reverted its own fix back to the buggy
    version, then reapplied it a third time — "action instability," a
    new pattern distinct from the already-documented "plan instability"
    (re-deriving a diagnosis without ever acting). This only detects the
    LITERAL string-level inverse from recorded tool arguments — purely
    mechanical, not a judgment about whether reverting was justified. A
    genuine, deliberate revert (new evidence shows the first patch was
    wrong) looks identical at this level; the nudge this feeds asks the
    model to say why, in one sentence, rather than blocking the action
    outright — patch_file itself stays a dumb, trusted primitive with no
    opinion on intent, same as everywhere else in kernel/."""
    for record in read_events(run_dir):
        if record.get("event_type") != "tool_call":
            continue
        if record["payload"]["tool_name"] != "patch_file":
            continue
        if record["payload"].get("result_preview", "").startswith(("ERROR", "REJECTED")):
            continue
        args = record["payload"].get("arguments", {})
        if args.get("path") == path and args.get("search") == replace and args.get("replace") == search:
            return record["event_id"]
    return None
