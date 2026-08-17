"""State for a rejected product mutation's one-turn recovery requirement.

Consolidates four previously-independent `agent.py` loop variables
(`rejected_mutation_read_pending`, `rejected_mutation_needs_read`,
`rejected_mutation_needs_test_command`, `last_rejected_mutation_paths`) plus
two substring-matching predicate functions (`_rejection_needs_exact_read`,
`_rejection_needs_test_command`) into one `RecoveryState` object driven by
one classification function. Adding a new recovery kind now means one enum
case plus filling in its behavior in the small tables/branches below,
instead of touching the ~10 scattered call sites this used to require.

See ``~/.claude/plans/agile-forging-salamander.md`` (Step A) for the
incident that motivated this: adding the TEST_COMMAND kind to the
pre-refactor flat variables required touching agent.py at init, two
rejection-detection sites, two tool-surface-narrowing sites, two
checkpoint-message call sites, and two state-clearing sites.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum, auto


class RecoveryKind(Enum):
    """What a rejected mutation requires before another mutation is legal."""

    NONE = auto()
    EXACT_READ = auto()          # block-mismatch / replay-guard
    BOUNDED_INSPECTION = auto()  # generic stagnation
    TEST_COMMAND = auto()        # harness-evidence stagnation


# Tool(s) that satisfy (consume) each kind's recovery requirement.
# BOUNDED_INSPECTION's set intentionally mirrors
# lifecycle_policy.REPAIR_INSPECTION_TOOLS (read_file, list_symbols); kept as
# a literal here rather than importing lifecycle_policy to avoid a
# dependency edge from this small, low-level module onto a larger policy
# module — the two are covered by a cross-check test instead.
_SATISFYING_TOOLS: dict[RecoveryKind, frozenset[str]] = {
    RecoveryKind.EXACT_READ: frozenset({"read_file"}),
    RecoveryKind.BOUNDED_INSPECTION: frozenset({"read_file", "list_symbols"}),
    RecoveryKind.TEST_COMMAND: frozenset({"run_command"}),
}

# Tool-name restriction for the next turn. None means "no additional
# restriction from recovery" — used for BOUNDED_INSPECTION, which keeps the
# broader existing recovery surface rather than narrowing to one tool.
_TOOL_SURFACE: dict[RecoveryKind, frozenset[str] | None] = {
    RecoveryKind.EXACT_READ: frozenset({"read_file"}),
    RecoveryKind.BOUNDED_INSPECTION: None,
    RecoveryKind.TEST_COMMAND: frozenset({"run_command"}),
}


def classify_rejection(message: str) -> RecoveryKind:
    """Classify a rejected-mutation result string into a RecoveryKind.

    Block mismatches and replay-guard rejections share one cause: the
    actor's source snapshot disagrees with the file on disk, so only an
    exact read of the rejected path can supply current text. A
    stagnation-policy rejection whose active failure is harness evidence
    (test-framework initialization, never a product assertion) cannot be
    fixed by any product edit or inspection — only running the declared
    test entry point can. Anything else that rejected a mutation gets one
    bounded inspection.
    """
    if (
        "exact block was not found" in message
        or "already failed in the current run" in message
        or "already succeeded in the current run" in message
        # Stale line anchors mean the model's line map is out of date;
        # only a fresh read can repair the numbers.
        or "exceeds the current file length" in message
    ):
        return RecoveryKind.EXACT_READ
    if "stagnation policy" in message and "harness evidence" in message:
        return RecoveryKind.TEST_COMMAND
    return RecoveryKind.BOUNDED_INSPECTION


def rejected_mutation_symbol_map(target_path: str, max_symbols: int = 80) -> str:
    """Return a bounded symbol map for the rejected file, or empty text.

    A block-mismatch rejection inside a large file is usually a navigation
    failure: the model knows which symbol it wants to change but not where
    it lives. The map gives line anchors so the required read_file lands on
    the right region instead of the file header.
    """
    if not target_path:
        return ""
    try:
        import kernel.io_tools as io_tools
        from workspace.list_symbols import list_symbols
        resolved = io_tools._resolve(target_path)
        if not os.path.isfile(resolved):
            return ""
        symbols = list_symbols(str(resolved))
        if not symbols:
            return ""
        rows = [f"  {kind} {name} (line {lineno})" for kind, name, lineno in symbols[:max_symbols]]
        suffix = ""
        if len(symbols) > max_symbols:
            suffix = f"\n  ... ({len(symbols) - max_symbols} more symbols omitted)"
        return (
            "\n\nSymbol map of the rejected file — choose the read_file offset from these line "
            "numbers, then read that region to obtain the exact current text:\n" + "\n".join(rows) + suffix
        )
    except Exception:
        return ""


def rejected_mutation_inspection_messages(
    messages,
    *,
    last_repair_packet,
    target_path: str = "",
    state_text: str = "",
    kind: RecoveryKind = RecoveryKind.BOUNDED_INSPECTION,
) -> list[dict]:
    """Build the one-turn inspection/action checkpoint after a rejected mutation."""
    target = f" on `{target_path}`" if target_path else " on the implicated product file"
    if kind == RecoveryKind.TEST_COMMAND:
        # A harness-evidence failure (test-framework initialization, not a
        # product assertion) cannot be resolved by any product edit or
        # further inspection. Only actually running the project's own
        # declared test entry point can produce trustworthy evidence.
        checkpoint = list(messages[:2]) + [{
            "role": "system",
            "content": (
                "[rejected mutation recovery] The active failure is harness evidence (test-framework "
                f"initialization, not a product assertion){target}, so no product edit can resolve it. "
                "Use this one turn to call run_command with the project's declared test entry point "
                "(named in the task prompt) to obtain real product evidence. Do not patch, edit, "
                "validate with any other tool, or finish until that command has run."
            ),
        }]
        if state_text:
            checkpoint.append({"role": "system", "content": state_text})
        checkpoint.append({
            "role": "system",
            "content": "Latest rejected-mutation evidence:\n" + str(last_repair_packet or "(not available)")[:3000],
        })
        return checkpoint

    symbol_map = ""
    if kind == RecoveryKind.EXACT_READ:
        instruction = (
            "Use this one turn to call read_file on that exact path (use offset/limit to view the "
            "region around the rejected block, including the closest-match lines shown in the "
            "rejection evidence). Only the file's current exact text can make the next patch match. "
        )
        symbol_map = rejected_mutation_symbol_map(target_path)
    else:
        instruction = "Use this one turn to read the current source with a focused inspection tool. "
    checkpoint = list(messages[:2]) + [{
        "role": "system",
        "content": (
            "[rejected mutation recovery] The previous product patch was rejected because its exact "
            f"find_exact_block did not match the file on disk{target}. {instruction}"
            "Do not patch, validate, finish, browse broadly, or "
            "repeat the rejected block. The host will reject the exact failed mutation again. The next "
            "turn will require a fresh mutation or recovery."
            + symbol_map
        ),
    }]
    if state_text:
        checkpoint.append({"role": "system", "content": state_text})
    checkpoint.append({
        "role": "system",
        "content": "Latest rejected-mutation evidence:\n" + str(last_repair_packet or "(not available)")[:3000],
    })
    return checkpoint


@dataclass
class RecoveryState:
    """The single pending recovery requirement for the current run, if any."""

    kind: RecoveryKind = RecoveryKind.NONE
    target_paths: list[str] = field(default_factory=list)

    @property
    def pending(self) -> bool:
        return self.kind != RecoveryKind.NONE

    def start(self, message: str, target_paths) -> None:
        """Begin recovery from a rejected-mutation result string.

        Unconditionally overwrites any previously pending recovery — the
        most recent confirmed rejection is authoritative. (Pre-dispatch
        *announcements* of an upcoming rejection are a separate, narrower
        concern: callers there should check `.pending` themselves before
        printing, without calling `start()`, so an already-pending recovery
        doesn't get a duplicate announcement before this method's caller
        — the actual dispatch site — runs.)
        """
        self.kind = classify_rejection(message)
        self.target_paths = list(target_paths)

    def clear(self) -> None:
        self.kind = RecoveryKind.NONE
        self.target_paths = []

    def tools_allowed(self) -> frozenset[str] | None:
        """Tool-name restriction for the next turn, or None for no
        recovery-specific restriction."""
        if not self.pending:
            return None
        return _TOOL_SURFACE[self.kind]

    def consume(self, tool_name: str, args: dict | None) -> bool:
        """Whether this tool call satisfies (and clears) the pending
        recovery. EXACT_READ additionally requires the read to target one
        of the rejected paths; the others are satisfied by an attempted
        call regardless of its outcome."""
        if not self.pending or tool_name not in _SATISFYING_TOOLS[self.kind]:
            return False
        if self.kind == RecoveryKind.EXACT_READ:
            target = str((args or {}).get("path") or "")
            if target not in self.target_paths:
                return False
        self.clear()
        return True

    def checkpoint_message(self, messages, *, last_repair_packet, state_text: str = "") -> list[dict]:
        return rejected_mutation_inspection_messages(
            messages,
            last_repair_packet=last_repair_packet,
            target_path=", ".join(self.target_paths),
            state_text=state_text,
            kind=self.kind,
        )

    def recovery_phase_text(self) -> tuple[str, str, str]:
        """(must, may, success) text for the RECOVERY control block."""
        if self.kind == RecoveryKind.TEST_COMMAND:
            return (
                "call run_command with the project's declared test entry point (named in the "
                "task prompt) to obtain real product evidence",
                "run only that declared test command",
                "real product test evidence (not harness/framework evidence) is available",
            )
        if self.kind == RecoveryKind.EXACT_READ:
            return (
                "call read_file on the rejected path to obtain the current exact block "
                "(the symbol map shows line numbers; read the region containing the "
                "symbol you intend to change)",
                "read only the rejected file region",
                "the current exact block text is available for the next mutation turn",
            )
        return (
            "read the implicated product file once to obtain the current exact block",
            "use only a focused read or symbol inspection",
            "current source evidence is available for the next mutation turn",
        )
