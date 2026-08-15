"""Deterministic action policy derived from the agent lifecycle snapshot.

The actor and the 4B worker may suggest actions, but this module decides which
validation-phase capabilities are legal.  Keeping the decision in one pure
function prevents scattered prompt/tool branches from drifting apart.
"""

from dataclasses import dataclass


READ_TOOLS = frozenset({
    "read_file", "find_files", "search_file", "list_workspace", "list_dir",
    "list_symbols", "grep_dir",
})
MUTATION_TOOLS = frozenset({"patch_file", "write_file"})
VALIDATION_TOOLS = frozenset({
    "run_tests", "run_command", "run_shell", "process_status", "stop_process",
    "diff_files", "git_diff",
})


@dataclass(frozen=True)
class ValidationActionPolicy:
    """The complete validation-phase policy for one actor turn."""

    tools: frozenset[str]
    setup_recovery: bool
    requires_mutation: bool
    prompt: str


def build_validation_policy(
    *,
    validation_required: bool,
    repair_required: bool,
    setup_failure: bool,
    repair_inspection_used: bool,
    last_mutation_rejected: bool,
    validation_failures: int,
    protected_edit_recovery_pending: bool,
    repair_recovery_mode: bool,
) -> ValidationActionPolicy | None:
    """Return one immutable policy, or ``None`` outside validation.

    Setup recovery is intentionally a two-step protocol: one targeted
    inspection, followed by an executable runner/command.  Behavioral repair
    is the converse: inspect once, then mutate.  Repeated failures narrow the
    surface further instead of reopening the whole tool registry.
    """
    if not validation_required:
        return None

    if not repair_required:
        return ValidationActionPolicy(
            tools=VALIDATION_TOOLS,
            setup_recovery=False,
            requires_mutation=False,
            prompt=(
                "Validation phase tool restriction: only validation tools are available this turn. "
                "Run a focused test or executable check and inspect its result."
            ),
        )

    tools = set(READ_TOOLS | MUTATION_TOOLS | {"diff_files", "git_diff", "process_status", "stop_process"})
    if setup_failure:
        tools.update({"run_tests", "run_command"})
        if repair_inspection_used:
            tools -= READ_TOOLS
            tools -= MUTATION_TOOLS
        prompt = (
            "A validation check failed because the execution/setup plane is incomplete. "
            "Use the available runner or explicit argv command to produce an assertion-bearing check. "
            "Do not mutate the product, rewrite the supplied test, or merely print a value."
        )
    else:
        if repair_inspection_used:
            tools -= READ_TOOLS
        prompt = (
            "A validation check failed. Inspect this failure and make one targeted repair now; "
            "do not run another check or finish until the defective artifact has changed."
        )

    if last_mutation_rejected:
        tools.discard("write_file")
    if validation_failures >= 2 and not setup_failure:
        tools.discard("write_file")
        prompt += " After repeated failures, use patch_file only and preserve the existing structure."
    if protected_edit_recovery_pending:
        tools -= MUTATION_TOOLS
        tools.update(VALIDATION_TOOLS)
        prompt = "The previous edit targeted a protected path. Run a fresh executable check before proposing another edit."
    if repair_recovery_mode and repair_required and not setup_failure:
        tools = set({"patch_file", "write_file", "diff_files", "git_diff", "finish_task"})
        prompt = "Repair recovery is active. Use the evidence already gathered and make exactly one targeted patch now."

    return ValidationActionPolicy(
        tools=frozenset(tools),
        setup_recovery=setup_failure,
        requires_mutation=repair_required and not setup_failure,
        prompt=prompt,
    )
