"""Piece 6: graduated escalation — the level system from the proposal:

    Level 0: Normal operation
    Level 1: Warn that recent actions produced no progress
    Level 2: Require justification tied to a specific unknown
    Level 3: Restrict zero-gain operations
    Level 4: Require the next missing completion-class action
    Level 5: Restart from a compact checkpoint or terminate trajectory

Replaces tonight's earlier FORCE_EDIT_AFTER_ITERATIONS mechanism, which
jumped straight from "fully unrestricted" to "read tools entirely removed"
at a single fixed iteration count — real data showed that produced rushed,
buggy first attempts (a missing import, a bad indentation guess) right
after the cutoff, on both runs it was tested against. This escalates
gradually instead: warn, then ask for justification, then restrict only
the SPECIFIC repeated zero-gain operation (not the whole capability
class), then finally require the missing capability outright — each level
a bigger nudge than the last, only reached if the smaller nudge didn't
work.

EscalationState tracks CONSECUTIVE turns needing intervention (both
stagnation and required-phase avoidance count the same way — both call for
"stop repeating, do something that changes state") and resets to Level 0
the moment real progress happens, so a model that self-corrects after a
single warning never sees Level 2+.
"""

from dataclasses import dataclass, field


# Level 5 fires after this many consecutive interventions regardless of
# level-4 attempts — a hard ceiling so a run that genuinely can't recover
# terminates cleanly instead of escalating forever. Deliberately not tied
# to iteration count (see progress_governor.py's own module docstring for
# why) — this counts consecutive NO-PROGRESS turns specifically.
LEVEL_5_THRESHOLD = 8


@dataclass
class EscalationState:
    consecutive_no_progress: int = 0
    last_repeated_scope: str = None  # the specific scope (file/command) behind the most recent duplicate, for Level 3's surgical restriction

    def update(self, signal, last_duplicate_scope: str = None) -> int:
        """Advance the state given this turn's ProgressSignal. Returns the
        resulting escalation level (0-5). Call once per iteration, after
        recording the turn's tool calls into the ledger."""
        if not signal.needs_intervention():
            self.consecutive_no_progress = 0
            self.last_repeated_scope = None
            return 0
        self.consecutive_no_progress += 1
        if last_duplicate_scope:
            self.last_repeated_scope = last_duplicate_scope
        n = self.consecutive_no_progress
        if n >= LEVEL_5_THRESHOLD:
            return 5
        if n >= 4:
            return 4
        return n  # 1, 2, or 3 map directly


def build_intervention(level: int, signal, contract, all_tool_names: set, escalation: EscalationState):
    """Returns (message: str | None, restricted_tool_names: set | None).
    restricted_tool_names=None means no restriction (full toolbelt);
    otherwise it's the exact set of tool names still allowed this call —
    same contract dispatch.py's allowed_names enforcement already expects
    (see agent.py's existing forced-edit-gating removal, which proved
    "offering fewer tools" alone isn't enough — this must be paired with
    dispatch.py's real enforcement to actually restrict anything)."""
    if level == 0:
        return None, None

    if level == 1:
        return (
            "The last few actions didn't produce any new information or change the task's state — "
            "you may be re-checking something you already know. Take a moment before your next action: "
            "is there something you genuinely still don't know, or is it time to act on what you have?"
        ), None

    if level == 2:
        return (
            "Still no new progress in the last few turns. Before your next tool call, state — in your own "
            "reasoning — exactly ONE specific fact you don't yet know that the next call will resolve. If "
            "you can't name one, you already have what you need; act on it instead of gathering more."
        ), None

    if level == 3:
        scope_note = f" (specifically: {escalation.last_repeated_scope!r})" if escalation.last_repeated_scope else ""
        return (
            f"You've repeated the same observation{scope_note} without it changing anything you know. "
            f"That specific call is being treated as zero-gain — try a different one, or act on what "
            f"you've already confirmed."
        ), None

    if level == 4:
        missing = signal.missing_capabilities
        missing_desc = ", ".join(missing) if missing else "the task's remaining requirement"
        allowed = {
            name for name in all_tool_names
            if _capability_of(name) in (missing[:1] or ["MUTATE"]) + ["VALIDATE", "DELIVER"]
        }
        # recall (an OBSERVE tool) stays available even when restricting to
        # missing capabilities — exact text of a pruned/prior result may
        # genuinely be needed to complete the required action correctly.
        allowed.add("recall") if "recall" in all_tool_names else None
        # run_shell is force-included too, for a different reason: it's not
        # in action_governor.CAPABILITY_CLASS at all (its real classification
        # is dynamic, by command content — see classify_run_shell), so
        # _capability_of's static lookup falls back to "OBSERVE" and would
        # incorrectly exclude it even when the model needs it to run tests
        # (a genuine VALIDATE use). VALIDATE is unconditionally part of the
        # level-4 target set, so this is always safe to include.
        allowed.add("run_shell") if "run_shell" in all_tool_names else None
        allowed.add("run_command") if "run_command" in all_tool_names else None
        return (
            f"You have enough information to proceed, but {missing_desc} hasn't happened yet. "
            f"Read-only exploration tools are unavailable this call on purpose — attempt the required "
            f"action now. An imperfect attempt you refine afterward is far better than continuing to wait."
        ), allowed

    if level == 5:
        # A hard finish-only wall is unsafe when the model has not yet made
        # the required mutation: one bad path or malformed observation can
        # strand a code-change task with no way to recover.  Give it a final,
        # deliberately narrow recovery window.  The caller still applies the
        # hard stop after HARD_STOP_AFTER_TURNS, so this cannot spin forever.
        if "MUTATE" in signal.missing_capabilities:
            recovery = {
                name for name in all_tool_names
                if _capability_of(name) in {"OBSERVE", "MUTATE", "VALIDATE", "DELIVER"}
                and name not in {"list_workspace", "list_dir", "grep_dir"}
            }
            recovery.update({"read_file", "find_files", "recall", "run_command", "run_shell"} & all_tool_names)
            return (
                "This is the final recovery window. The required code mutation still has not happened. "
                "Use one targeted read_file/find_files call only if needed to establish the exact path, "
                "then patch_file or write_file, validate it, and finish; do not resume broad exploration."
            ), recovery
        return (
            "This task has made no real progress for many turns in a row despite multiple prompts. "
            "Stop and call finish_task now with an honest summary of what was and wasn't accomplished, "
            "rather than continuing to spin."
        ), {"finish_task"}

    return None, None


def _capability_of(tool_name: str) -> str:
    import action_governor as gov
    return gov.CAPABILITY_CLASS.get(tool_name, "OBSERVE")


def _self_test() -> bool:
    import action_governor as gov
    import progress_governor as pg
    import task_contract as tc

    # --- EscalationState.update(): escalates one level per consecutive
    # no-progress turn, resets to 0 the instant progress resumes.
    state = EscalationState()
    stuck_signal = pg.ProgressSignal(
        stagnating=True, avoiding_required_phase=False,
        missing_capabilities=["MUTATE"], new_evidence_count=1, capabilities_seen={"OBSERVE"},
    )
    progressing_signal = pg.ProgressSignal(
        stagnating=False, avoiding_required_phase=False,
        missing_capabilities=[], new_evidence_count=5, capabilities_seen={"OBSERVE", "MUTATE"},
    )
    levels = [state.update(stuck_signal) for _ in range(LEVEL_5_THRESHOLD)]
    assert levels == [1, 2, 3, 4, 4, 4, 4, 5], levels
    assert state.update(progressing_signal) == 0, "real progress must reset to level 0 immediately"
    assert state.consecutive_no_progress == 0

    # --- build_intervention: level 0 is a true no-op.
    msg0, restricted0 = build_intervention(0, stuck_signal, tc.CODE_CHANGE_CONTRACT, {"read_file", "patch_file"}, state)
    assert msg0 is None and restricted0 is None

    # --- level 1-3: message present, but NO tool restriction (matches the
    # design principle: nudge before restricting, restricting is levels
    # 4-5 only).
    for lvl in (1, 2, 3):
        msg, restricted = build_intervention(lvl, stuck_signal, tc.CODE_CHANGE_CONTRACT, {"read_file", "patch_file"}, state)
        assert msg is not None and len(msg) > 0, f"level {lvl} must produce a message"
        assert restricted is None, f"level {lvl} must not restrict tools yet"

    # --- level 4: restricts to the missing capability's tools + VALIDATE/
    # DELIVER, and does NOT include pure-OBSERVE tools like read_file.
    all_tools = {"read_file", "grep_dir", "patch_file", "write_file", "run_tests", "run_shell", "finish_task", "recall"}
    msg4, restricted4 = build_intervention(4, stuck_signal, tc.CODE_CHANGE_CONTRACT, all_tools, state)
    assert "patch_file" in restricted4 and "write_file" in restricted4, restricted4
    assert "read_file" not in restricted4 and "grep_dir" not in restricted4, restricted4
    assert "recall" in restricted4, "recall should stay available even when restricting to required capability"
    # Real gap caught live tonight: run_shell isn't in the static
    # CAPABILITY_CLASS dict (its real classification is dynamic, by
    # command content), so without the explicit force-include it was
    # silently excluded from level 4 even though it's a legitimate way to
    # run tests (VALIDATE), which is unconditionally part of the target set.
    assert "run_shell" in restricted4, "run_shell should stay available at level 4 — it's a legitimate VALIDATE path"

    # --- level 5: a missing mutation gets one narrow recovery window rather
    # than an immediate finish-only wall; non-mutation tasks still terminate.
    msg5, restricted5 = build_intervention(5, stuck_signal, tc.CODE_CHANGE_CONTRACT, all_tools, state)
    assert "patch_file" in restricted5 and "read_file" in restricted5, restricted5
    assert "list_workspace" not in restricted5 and "grep_dir" not in restricted5, restricted5
    diagnosis_signal = pg.ProgressSignal(
        stagnating=True, avoiding_required_phase=False, missing_capabilities=["DELIVER"],
        new_evidence_count=1, capabilities_seen={"OBSERVE"},
    )
    _, diagnosis_restricted = build_intervention(5, diagnosis_signal, tc.DIAGNOSIS_CONTRACT, all_tools, state)
    assert diagnosis_restricted == {"finish_task"}, diagnosis_restricted

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   escalation_governor (EscalationState + build_intervention, levels 0-5)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
