"""Kernel tier: sandboxed shell execution.

NOTE: this is a resource sandbox, not a security sandbox. It confines the
working directory and caps runtime/output, but a command that reaches
outside the sandbox root (`cd .. && rm -rf x`, absolute paths, network
calls) is not blocked. That's an acceptable risk model for a local dev
tool you run yourself against your own model — it would not be for
anything processing untrusted input. When agent.py points the sandbox root
at a real project via --project, run_shell can run anything in that
project — that's the explicit tradeoff of pointing it there.
"""

import subprocess

from kernel.sandbox import get_root

SHELL_TIMEOUT_SECONDS = 15
MAX_SHELL_TIMEOUT_SECONDS = 120  # a hallucinated huge value shouldn't be able to hang a run indefinitely
MAX_OUTPUT_CHARS = 4000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n...[truncated, {len(text) - MAX_OUTPUT_CHARS} more chars]"


def run_shell(command: str, timeout: int = SHELL_TIMEOUT_SECONDS) -> str:
    """Run a shell command with its working directory confined to the
    workspace, and report exit code, stdout, and stderr. Use this to run
    tests, install a package, or invoke a tool you already wrote.

    Args:
      command: The shell command to execute, e.g. 'python3 search_text.py foo bar.txt'.
      timeout: Max seconds to let the command run before killing it and
        reporting TIMEOUT. Default 15. Raise this for a genuinely slow
        command (e.g. a full test suite); capped at 120s regardless of
        what's requested.
    """
    timeout = max(1, min(timeout, MAX_SHELL_TIMEOUT_SECONDS))
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=get_root(),
        )
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout}s — command likely hung."

    return (
        f"Exit code: {result.returncode}\n"
        f"STDOUT:\n{_truncate(result.stdout)}\n"
        f"STDERR:\n{_truncate(result.stderr)}"
    )
