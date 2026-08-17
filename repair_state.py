"""State for the mutation -> validation -> repair FSM (agent.py's core loop).

Consolidates ten previously-independent `agent.py` loop variables
(`validation_required`, `repair_required`, `repair_recovery_mode`,
`repair_mode_entries`, `repair_turns_used`, `repair_mutation_pending`,
`revalidation_attempts`, `successful_repair_cycles`, `validation_failures`,
`repair_inspection_used`) into one `RepairState` object with named
transition methods, each applying — in one place — a field bundle that was
previously hand-duplicated at 3-5 separate call sites in `agent.py`.

This intentionally stays narrow: only the FSM's own fields move here. The
side effects that accompany a transition in `agent.py` (building a failure
packet, `transaction.note_validation_failed`, `lifecycle.transition(...)`,
novelty-context triage, appending prompt messages) stay exactly where they
are — folding those in too would make this object need to know about
transaction/lifecycle/novelty/validation_plan, which is a different, much
larger concern than "what state is the repair FSM in."

See ``~/.claude/plans/agile-forging-salamander.md`` (Step C).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RepairState:
    """The mutation -> validation -> repair/recovery FSM's own fields."""

    # A mutation opens a deterministic verification phase. The actor may
    # choose how to validate, but it cannot make another edit or deliver
    # until one validation action succeeds. This is an engine policy,
    # independent of model, provider, task wording, or tool names beyond
    # their capabilities.
    validation_required: bool = False
    validation_failures: int = 0
    repair_required: bool = False
    repair_recovery_mode: bool = False
    repair_mode_entries: int = 0
    repair_turns_used: int = 0
    repair_mutation_pending: bool = False
    repair_inspection_used: bool = False
    revalidation_attempts: int = 0
    successful_repair_cycles: int = 0

    def reopen_validation(self) -> None:
        """Reset to a clean validation-required state without an active
        repair, e.g. after probe-quality recovery or tool-plane recovery: a
        rejected/low-quality validation call is not product evidence, so
        the repair turn budget and any recovery-mode escalation are wiped
        along with it."""
        self.validation_required = True
        self.repair_required = False
        self.validation_failures = 0
        self.repair_turns_used = 0
        self.repair_recovery_mode = False
        self.repair_inspection_used = False

    def record_mutation_landed(self) -> None:
        """A mutation succeeded while repair was active: return to
        validation-only. Does not itself set `validation_required` — the
        caller does that unconditionally regardless of whether repair was
        active, since an ordinary (non-repair) mutation also opens a
        validation phase."""
        self.repair_mutation_pending = True
        self.repair_required = False
        self.repair_inspection_used = False
        self.validation_failures = 0
        self.repair_turns_used = 0
        self.repair_recovery_mode = False

    def enter_repair(self) -> None:
        """A validation check failed: require a targeted repair mutation
        before the next revalidation. `repair_mode_entries` counts distinct
        entries into repair, not every failure — it only increments the
        first time (repeated failures while already in repair keep
        incrementing `validation_failures` instead)."""
        if not self.repair_required:
            self.repair_mode_entries += 1
        self.validation_required = True
        self.validation_failures += 1
        self.repair_required = True
        self.repair_inspection_used = False

    def record_repair_turn(self) -> None:
        self.repair_turns_used += 1

    def force_recovery_checkpoint(self, minimum_turns_used: int) -> None:
        """Force a bounded targeted-mutation checkpoint on the next turn,
        preserving repair intent even if something upstream (e.g. a
        tool-plane recovery) reopened plain validation this turn.
        `minimum_turns_used` is the caller's REPAIR_TURN_BUDGET — passed in
        rather than imported so this module stays independent of agent.py's
        specific constants."""
        self.repair_recovery_mode = True
        self.validation_required = True
        self.repair_required = True
        self.repair_turns_used = max(self.repair_turns_used, minimum_turns_used)

    def enter_recovery_mode(self) -> None:
        self.repair_recovery_mode = True

    def mark_inspection_used(self) -> None:
        self.repair_inspection_used = True

    def record_revalidation_attempt(self) -> None:
        self.revalidation_attempts += 1

    def record_validation_passed(self) -> bool:
        """Record a passing validation. Returns True if this closes out a
        repair cycle that had a pending mutation (i.e. the mutation that
        was made in repair is what the passing check confirms) — the
        caller uses this to decide whether to count a successful repair
        cycle."""
        if self.repair_mutation_pending:
            self.successful_repair_cycles += 1
            self.repair_mutation_pending = False
            return True
        return False
