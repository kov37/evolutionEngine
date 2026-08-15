"""Deterministic action policy derived from the agent lifecycle snapshot.

The actor and the 4B worker may suggest actions, but this module decides which
validation-phase capabilities are legal.  Keeping the decision in one pure
function prevents scattered prompt/tool branches from drifting apart.
"""

import shlex
from dataclasses import dataclass


READ_TOOLS = frozenset({
    "read_file", "find_files", "search_file", "list_workspace", "list_dir",
    "list_symbols", "grep_dir",
})
# Inventory is useful for orientation but is not evidence about the failing
# implementation. Only these focused readers should consume the one
# repair-inspection allowance; otherwise a harmless list_workspace call can
# block the read_file needed to understand the reported failure.
REPAIR_INSPECTION_TOOLS = frozenset({
    "read_file", "search_file", "list_symbols", "grep_dir",
})
# Once a behavioral failure is available, broad inventory is a poor next
# action: the failure packet already gives the actor a target. Keep focused
# readers/searches, but remove the two tools most likely to restart orientation
# instead of repairing. Setup recovery deliberately uses the full READ_TOOLS
# set because its missing target may be the environment itself.
BEHAVIOR_REPAIR_READ_TOOLS = READ_TOOLS - frozenset({"list_workspace", "list_dir"})
MUTATION_TOOLS = frozenset({"patch_file", "write_file"})
VALIDATION_TOOLS = frozenset({
    "run_tests", "run_command", "run_shell", "process_status", "stop_process",
    "diff_files", "git_diff",
})
ORIENTATION_TOOLS = frozenset({
    "read_file", "find_files", "search_file", "patch_file", "write_file",
    "finish_task", "recall",
})
ORIENTATION_PROGRESS_TOOLS = frozenset({
    "patch_file", "write_file", "run_tests", "run_command", "run_shell",
    "finish_task", "recall", "diff_files", "git_diff",
})


def counts_as_repair_inspection(tool_name: str) -> bool:
    """Return whether a tool produced focused evidence for a repair turn."""
    return tool_name in REPAIR_INSPECTION_TOOLS


def orientation_action_tools(*, evidence_available: bool = False) -> frozenset[str]:
    """Return the action surface after the orientation budget.

    Before the actor has obtained any useful evidence, one targeted read is
    still legal. Once evidence exists, keeping read/search tools available
    turns the recovery surface into another exploration loop, so only
    mutation, validation, completion, and exact recall remain.
    """
    return ORIENTATION_PROGRESS_TOOLS if evidence_available else ORIENTATION_TOOLS


def is_inspection_command(command) -> bool:
    """Identify a simple shell read/list command, without parsing a shell DSL.

    This intentionally blocks only an unambiguous single command. Pipelines,
    conditionals, interpreters, test runners, installers, and service probes
    remain available because they may carry behavioral evidence or change the
    environment.
    """
    if isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
    elif isinstance(command, (list, tuple)):
        argv = [str(part) for part in command]
    else:
        return False
    if not argv or any(token in {"&&", "||", ";", "|"} for token in argv):
        return False
    head = argv[0].rsplit("/", 1)[-1]
    # Shell wrappers are a common command-plane escape hatch. Inspect the
    # wrapped single command recursively, but keep compound shell programs
    # available because they may be real setup or behavioral checks.
    if head in {"bash", "sh", "zsh", "fish"} and "-c" in argv:
        command_index = argv.index("-c")
        wrapped = argv[command_index + 1:]
        if not wrapped:
            return False
        return is_inspection_command(" ".join(wrapped))
    if head in {
        "cat", "head", "tail", "less", "more", "sed", "awk", "grep",
        "rg", "find", "ls", "pwd", "tree", "file", "wc",
    }:
        return True

    # A command-plane guard must cover the shell's escape hatches too.  Small
    # models commonly replace a blocked `read_file` with `node -e` or
    # `python -c` that opens a file and prints it.  Keep this deliberately
    # narrow: only classify an inline interpreter snippet as inspection when it
    # both reads a file and prints the result, and do not block snippets that
    # contain an obvious test, process, network, or mutation operation.
    if head not in {
        "node", "nodejs", "bun", "deno", "python", "python3", "ruby", "perl", "php",
    }:
        return False
    if len(argv) == 2 and argv[1] in {"-v", "--version", "-h", "--help"}:
        return True
    try:
        code_index = next(i for i, token in enumerate(argv[1:], 1) if token in {"-e", "-c"})
    except StopIteration:
        return False
    code = " ".join(argv[code_index + 1:]).lower()
    if not code:
        return False
    reads_file = any(marker in code for marker in {
        "readfilesync", "readfile(", "read_text", "read_text(", "open(",
        "fs.readfile", "fs.readfilesync",
    })
    prints_result = any(marker in code for marker in {
        "console.log", "console.error", "print(", "puts ", "p ",
    })
    has_behavior = any(marker in code for marker in {
        "assert", "pytest", "unittest", "test(", "spawn(", "exec(",
        "request(", "fetch(", "listen(", "writefile", "write_text",
        "unlink", "mkdir", ".send(", "websocket",
    })
    return reads_file and prints_result and not has_behavior


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
    mutation_batch_remaining: int = 0,
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
        validation_tools = set(VALIDATION_TOOLS)
        batch_note = ""
        if mutation_batch_remaining > 0:
            validation_tools |= MUTATION_TOOLS
            batch_note = (
                f" Up to {mutation_batch_remaining} related product artifact(s) may still be added to "
                "the current change batch before validation; use mutations only for distinct unfinished "
                "artifacts, then validate."
            )
        return ValidationActionPolicy(
            tools=frozenset(validation_tools),
            setup_recovery=False,
            requires_mutation=False,
            prompt=(
                "Validation phase tool restriction: only validation tools are available this turn. "
                "Run a focused test or executable check and inspect its result. If the check needs "
                "a temporary probe, execute it inline through run_command/run_shell (for example, "
                "node -e or python -c); do not create a new helper file with a write tool." + batch_note
            ),
        )

    tools = set(READ_TOOLS | MUTATION_TOOLS | {"diff_files", "git_diff", "process_status", "stop_process"})
    if setup_failure:
        # A setup failure never justifies changing product code or supplied
        # evidence. The first recovery turn may inspect; it cannot guess-edit.
        tools -= MUTATION_TOOLS
        tools.update({"run_tests", "run_command"})
        if repair_inspection_used:
            tools -= READ_TOOLS
        prompt = (
            "A validation check failed because the execution/setup plane is incomplete. "
            "Use the available runner or explicit argv command to produce an assertion-bearing check. "
            "Do not mutate the product, rewrite the supplied test, or merely print a value."
        )
    else:
        tools -= READ_TOOLS - BEHAVIOR_REPAIR_READ_TOOLS
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
        tools = {"patch_file", "diff_files", "git_diff", "finish_task"}
        if not last_mutation_rejected:
            tools.add("write_file")
        prompt = "Repair recovery is active. Use the evidence already gathered and make exactly one targeted patch now."
        if last_mutation_rejected:
            prompt += " write_file was rejected earlier; use patch_file instead and do not retry it."

    return ValidationActionPolicy(
        tools=frozenset(tools),
        setup_recovery=setup_failure,
        requires_mutation=repair_required and not setup_failure,
        prompt=prompt,
    )
