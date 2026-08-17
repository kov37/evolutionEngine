"""Live-streaming launcher for actor subprocesses.

Runs an already-constructed agent.py subprocess, prints a compact per-line
summary as it happens, and persists every raw line to a monitor JSONL file
so a run can be watched in real time (``run_monitor.py --file <path>
--follow``) instead of only after the process exits. Shared by every
harness that launches the actor (``agentic_benchmark.py``,
``swebench_runner.py``) so live visibility is not a benchmark-specific
feature.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from pathlib import Path


def event_kind(line: str) -> str:
    """Classify one live actor line for the durable monitor stream."""
    stripped = line.strip()
    if stripped.startswith("🌀 [Iteration"):
        return "iteration"
    if stripped.startswith("🔧"):
        return "tool_call"
    if stripped.startswith("⏱️"):
        return "timing"
    if stripped.startswith("🧰"):
        return "repair_metrics"
    if stripped.startswith("🧬"):
        return "novelty_metrics"
    if "validation" in stripped.lower():
        return "validation"
    if any(word in stripped.lower() for word in ("error", "rejected", "failed", "timeout")):
        return "error"
    if stripped.startswith(("🧠", "💭")):
        return "model_output"
    return "agent_event"


def live_summary(line: str) -> str:
    """Return a compact user-facing line; the monitor JSONL keeps raw text."""
    kind = event_kind(line)
    stripped = line.strip()
    if kind == "tool_call":
        match = re.match(r"🔧\s+([A-Za-z0-9_]+)\((.*)", stripped)
        if match:
            args = match.group(2)
            path = re.search(r"['\"]path['\"]:\s*['\"]([^'\"]+)", args)
            target = f" path={path.group(1)}" if path else ""
            return f"📡 tool {match.group(1)}{target}"
    if kind == "model_output":
        return "📡 model " + stripped[:220]
    if kind == "error":
        return "📡 ERROR " + stripped[:300]
    if kind in {"iteration", "timing", "repair_metrics", "novelty_metrics", "validation"}:
        return "📡 " + stripped[:400]
    return "📡 event " + stripped[:220]


def descendant_pids(pid: int) -> list[int]:
    """Find descendants even when an actor gives a service a new session."""
    try:
        raw = subprocess.check_output(["pgrep", "-P", str(pid)], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return []
    result = []
    for value in raw.split():
        try:
            child = int(value)
        except ValueError:
            continue
        result.append(child)
        result.extend(descendant_pids(child))
    return result


def terminate_process_tree(proc) -> None:
    """Terminate the actor and descendants, including detached service sessions."""
    descendants = descendant_pids(proc.pid)
    # Detached descendants have their own process groups. Kill those first;
    # then kill the actor's group, which also handles ordinary child processes.
    for pid in reversed(descendants):
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        for pid in reversed(descendant_pids(proc.pid)):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        proc.wait()


def stream_agent(proc, started: float, run_timeout: float, monitor_path: Path):
    """Stream every agent line while retaining the watchdog.

    ``started`` must be a ``time.monotonic()`` timestamp. Returns
    ``(full_stdout_text, timed_out, returncode)``.
    """
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    chunks = []
    partial = b""
    timed_out = False
    interrupted = False
    monitor_path.parent.mkdir(parents=True, exist_ok=True)
    monitor = monitor_path.open("w", encoding="utf-8")

    def emit(raw_line: bytes):
        line = raw_line.decode(errors="replace")
        chunks.append(line + "\n")
        print(live_summary(line), flush=True)
        event = {
            "elapsed_s": round(time.monotonic() - started, 3),
            "kind": event_kind(line),
            "text": line,
        }
        monitor.write(json.dumps(event, ensure_ascii=False) + "\n")
        monitor.flush()

    try:
        while True:
            remaining = run_timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                break
            ready = selector.select(min(0.5, remaining))
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            data = os.read(proc.stdout.fileno(), 4096)
            if not data:
                break
            partial += data
            lines = partial.split(b"\n")
            partial = lines.pop()
            for line in lines:
                emit(line)
        if partial:
            emit(partial)
    except KeyboardInterrupt:
        interrupted = True
        timed_out = False
        print("📡 ⚠️ benchmark interrupted; terminating actor process tree", flush=True)
    finally:
        if proc.poll() is None or timed_out or interrupted:
            terminate_process_tree(proc)
        else:
            proc.wait()
        selector.close()
        monitor.close()
    return "".join(chunks), timed_out, proc.returncode
