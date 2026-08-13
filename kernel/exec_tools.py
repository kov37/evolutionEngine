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

import os
import signal
import subprocess

from kernel.sandbox import confine, get_root

SHELL_TIMEOUT_SECONDS = 15
MAX_SHELL_TIMEOUT_SECONDS = 120  # a hallucinated huge value shouldn't be able to hang a run indefinitely
MAX_OUTPUT_CHARS = 4000
MAX_COMMAND_TIMEOUT_SECONDS = 120


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
    proc = subprocess.Popen(
        command, shell=True, cwd=get_root(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,  # own process group, so a timeout can kill the whole tree
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # subprocess.run's own timeout only kills the shell process itself,
        # not anything it spawned — a real orphaned pytest process was found
        # live, still running 46 CPU-minutes after being reported TIMEOUT.
        # Killing the whole process group (not just proc.pid) actually ends
        # every descendant.
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()
        return f"TIMEOUT after {timeout}s — command likely hung."

    return (
        f"Exit code: {proc.returncode}\n"
        f"STDOUT:\n{_truncate(stdout)}\n"
        f"STDERR:\n{_truncate(stderr)}"
    )


def run_command(command: list[str], timeout: int = SHELL_TIMEOUT_SECONDS, cwd: str = ".") -> str:
    """Run an executable with argv semantics and a confined working directory.

    Prefer this for tests and project commands. Unlike ``run_shell`` it does
    not invoke a shell, so quoting, pipes, redirects, and shell metacharacters
    cannot change the command's meaning.
    """
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        return "ERROR: command must be a non-empty list of strings."
    try:
        timeout = max(1, min(int(timeout), MAX_COMMAND_TIMEOUT_SECONDS))
        workdir = confine(cwd)
    except (TypeError, ValueError) as exc:
        return f"ERROR: invalid command options: {exc}"
    if not os.path.isdir(workdir):
        return f"ERROR: cwd is not a directory: {cwd}"

    try:
        proc = subprocess.Popen(
            command, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            return f"TIMEOUT after {timeout}s — command likely hung."
    except OSError as exc:
        return f"ERROR: could not start command: {exc}"

    return (
        f"Exit code: {proc.returncode}\n"
        f"STDOUT:\n{_truncate(stdout)}\n"
        f"STDERR:\n{_truncate(stderr)}"
    )
