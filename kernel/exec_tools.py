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
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid

from kernel.sandbox import confine, get_root

SHELL_TIMEOUT_SECONDS = 15
MAX_SHELL_TIMEOUT_SECONDS = 120  # a hallucinated huge value shouldn't be able to hang a run indefinitely
MAX_OUTPUT_CHARS = 4000
MAX_COMMAND_TIMEOUT_SECONDS = 120
_BACKGROUND = {}
_BACKGROUND_LOCK = threading.Lock()


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n...[truncated, {len(text) - MAX_OUTPUT_CHARS} more chars]"


def _start_background(command, cwd, shell):
    log = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=".agent-process-", suffix=".log",
        dir=get_root(), delete=False,
    )
    log_path = log.name
    proc = subprocess.Popen(
        command, shell=shell, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    handle = "proc-" + uuid.uuid4().hex[:12]
    with _BACKGROUND_LOCK:
        _BACKGROUND[handle] = {"proc": proc, "log_path": log_path, "command": str(command)}
    return (
        "Started background process.\n"
        f"Handle: {handle}\nPID: {proc.pid}\nLog: {log_path}\n"
        f"Use process_status(handle='{handle}') to inspect it and stop_process(handle='{handle}') to clean it up."
    )


def process_status(handle: str, tail_chars: int = 3000) -> str:
    """Inspect a process started by run_shell/run_command(background=True)."""
    with _BACKGROUND_LOCK:
        item = _BACKGROUND.get(handle)
    if item is None:
        return f"ERROR: unknown process handle: {handle}"
    proc = item["proc"]
    code = proc.poll()
    try:
        with open(item["log_path"], "r", encoding="utf-8", errors="replace") as stream:
            log = stream.read()[-max(1, min(int(tail_chars), MAX_OUTPUT_CHARS)):]
    except OSError as exc:
        log = f"(log unavailable: {exc})"
    state = "RUNNING" if code is None else f"EXITED code={code}"
    return f"{state}\nHandle: {handle}\nLog tail:\n{log}"


def stop_process(handle: str) -> str:
    """Stop a managed background process and its descendants."""
    with _BACKGROUND_LOCK:
        item = _BACKGROUND.get(handle)
    if item is None:
        return f"ERROR: unknown process handle: {handle}"
    proc = item["proc"]
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
        except ProcessLookupError:
            pass
    else:
        return f"Already stopped process {handle}; exit code={proc.returncode}; launch a fresh process before probing"
    return f"Stopped process {handle}; exit code={proc.returncode}; log={item['log_path']}"


def cleanup_background_processes() -> None:
    """Best-effort cleanup for all processes owned by this agent run."""
    with _BACKGROUND_LOCK:
        handles = list(_BACKGROUND)
    for handle in handles:
        stop_process(handle)


def active_background_handles() -> list[str]:
    """Return handles for background processes still running in this agent."""
    with _BACKGROUND_LOCK:
        items = list(_BACKGROUND.items())
    return [handle for handle, item in items if item["proc"].poll() is None]


def run_shell(command: str, timeout: int = SHELL_TIMEOUT_SECONDS, background: bool = False) -> str:
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
    if background:
        try:
            return _start_background(command, get_root(), shell=True)
        except OSError as exc:
            return f"ERROR: could not start background command: {exc}"
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


def run_command(command: list[str], timeout: int = SHELL_TIMEOUT_SECONDS, cwd: str = ".",
                background: bool = False) -> str:
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

    # Python installations commonly expose `python3` but not the `python`
    # alias. Normalize that portable spelling at the execution boundary so a
    # model's otherwise-valid test command is not mistaken for an application
    # failure. This is intentionally limited to the interpreter executable;
    # project commands and arguments remain untouched.
    command = list(command)
    if command[0] == "python" and shutil.which("python") is None:
        command[0] = "python3" if shutil.which("python3") else sys.executable

    if background:
        try:
            return _start_background(command, workdir, shell=False)
        except OSError as exc:
            return f"ERROR: could not start background command: {exc}"

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

    if (
        proc.returncode == 0
        and len(command) >= 2
        and command[0] in {"python", "python3", sys.executable}
        and os.path.basename(command[1]).startswith(("test_", "test-"))
        and command[1].endswith(".py")
        and not stdout.strip()
        and not stderr.strip()
    ):
        return (
            f"ERROR: test module '{command[1]}' produced no test evidence; "
            "invoke a test runner or explicitly call its test function."
        )
    return (
        f"Exit code: {proc.returncode}\n"
        f"STDOUT:\n{_truncate(stdout)}\n"
        f"STDERR:\n{_truncate(stderr)}"
    )
