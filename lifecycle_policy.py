"""Deterministic action policy derived from the agent lifecycle snapshot.

The actor and the 4B worker may suggest actions, but this module decides which
validation-phase capabilities are legal.  Keeping the decision in one pure
function prevents scattered prompt/tool branches from drifting apart.
"""

import shlex
import re
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
DEPENDENCY_MANIFEST_NAMES = frozenset({
    "package.json", "package-lock.json", "npm-shrinkwrap.json",
    "pyproject.toml", "poetry.lock", "pipfile", "pipfile.lock",
    "cargo.toml", "cargo.lock", "go.mod", "go.sum",
    "gemfile", "gemfile.lock", "composer.json", "composer.lock",
    "pom.xml", "build.gradle", "settings.gradle",
})
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


_FILE_MUTATION_HEADS = frozenset({"tee", "cp", "mv", "rm", "touch", "install"})
_INTERPRETER_HEADS = frozenset({
    "node", "nodejs", "bun", "deno", "python", "python3", "ruby", "perl", "php",
})


def _contains_file_mutation(argv) -> bool:
    """Recognize common file-write forms before classifying a read command."""
    tokens = [str(token) for token in (argv or [])]
    if not tokens:
        return False
    head = tokens[0].rsplit("/", 1)[-1].lower()
    if head in _FILE_MUTATION_HEADS:
        return True
    if any(
        (token in {">", ">>"}
         or (token.startswith((">", "1>", "2>", "3>"))
             and not token.startswith((">&", "1>&", "2>&", "3>&"))))
        for token in tokens[1:]
    ):
        return True
    if head == "sed" and any(token in {"-i", "--in-place"} or token.startswith("-i") for token in tokens[1:]):
        return True
    if head == "perl" and any(token == "-i" or token.startswith("-i") or token == "--in-place" for token in tokens[1:]):
        return True
    if head not in _INTERPRETER_HEADS:
        return False
    try:
        code_index = next(i for i, token in enumerate(tokens[1:], 1) if token in {"-e", "-c"})
    except StopIteration:
        return False
    code = " ".join(tokens[code_index + 1:]).lower()
    if any(marker in code for marker in {
        "writefilesync", "appendfilesync", "unlinksync", "mkdirsync",
        "fs.writefile", "fs.appendfile", "write_text", ".write(",
        ".write (", ".unlink(",
    }):
        return True
    return bool(re.search(r"\bopen\s*\([^)]*,\s*['\"][wax+]", code))


def is_output_only_command(command) -> bool:
    """Return true when a command can only print text, not test behavior."""
    if isinstance(command, (list, tuple)):
        argv = [str(part) for part in command]
    else:
        try:
            argv = shlex.split(str(command or ""))
        except ValueError:
            return False
    if not argv or any(token in {"&&", "||", ";", "|"} for token in argv):
        return False
    if _contains_file_mutation(argv):
        return False
    return argv[0].rsplit("/", 1)[-1].lower() in {"echo", "printf"}


def counts_as_repair_inspection(tool_name: str) -> bool:
    """Return whether a tool produced focused evidence for a repair turn."""
    return tool_name in REPAIR_INSPECTION_TOOLS


def is_dependency_manifest_path(path: str) -> bool:
    """Return true only for a conventional dependency/config manifest path."""
    normalized = str(path or "").replace("\\", "/").rstrip("/")
    name = normalized.rsplit("/", 1)[-1].lower()
    return name in DEPENDENCY_MANIFEST_NAMES or (
        name.startswith("requirements") and name.endswith(".txt")
    )


def is_validation_helper_path(path: str) -> bool:
    """Return true for ephemeral probes kept below the agent-owned directory."""
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = [part for part in normalized.split("/") if part]
    return len(parts) >= 2 and parts[0] == ".agentic"


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
    if _contains_file_mutation(argv):
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
    accepted_validation_evidence: bool = False,
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
        # A temporary behavioral probe is a validation artifact, not a
        # product mutation. Dispatch restricts this write surface to .agentic/.
        validation_tools.add("write_file")
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
                "a single-line node -e or python -c command). Keep every tool argument single-line. "
                "If a helper file is clearer, write it only below .agentic/ and run that helper; "
                "do not edit product or supplied test files." + batch_note
            ),
        )

    tools = set(READ_TOOLS | MUTATION_TOOLS | {"diff_files", "git_diff", "process_status", "stop_process"})
    if setup_failure:
        # A setup failure never justifies changing product code or supplied
        # evidence. A dependency manifest is the one bounded exception: the
        # runner cannot install a declared dependency until that manifest
        # exists, and dispatch applies the same path allowlist below.
        tools |= MUTATION_TOOLS
        tools.update({"run_tests", "run_command"})
        if repair_inspection_used:
            tools -= READ_TOOLS
        prompt = (
            "A validation check failed because the execution/setup plane is incomplete. "
            "Use the available runner or explicit argv command to produce an assertion-bearing check. "
            "You may create or update only a dependency manifest such as package.json; do not mutate "
            "product code, rewrite the supplied test, or merely print a value."
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
    if accepted_validation_evidence and not setup_failure:
        # A later orchestration/tool-plane failure must not erase an already
        # accepted behavioral result or force a needless product rewrite.
        # Keep finish_task legal so the actor can hand off the verified state.
        tools.add("finish_task")
        prompt += " An earlier executable check already passed; if this failure is only a tool-plane restriction, call finish_task rather than changing product code."

    return ValidationActionPolicy(
        tools=frozenset(tools),
        setup_recovery=setup_failure,
        requires_mutation=repair_required and not setup_failure,
        prompt=prompt,
    )
