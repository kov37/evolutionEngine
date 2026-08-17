"""Reusable, deterministic agent-capability benchmark.

Each task is created in a fresh temporary workspace and graded mechanically.
The same task can be run with the plain 35B loop or with noveltyEngine's 4B
context worker.  This is intentionally separate from the SymPy benchmark:
the goal is to measure general agent behavior, not one repository's history.

Examples:
    python3 agentic_benchmark.py --task all --condition both --iterations 20
    python3 agentic_benchmark.py --task recovery --condition novelty --action-critic
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from independent_grader import GradeResult, run_python_grader


ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "state" / "benchmark" / "agentic"


@dataclass(frozen=True)
class GraderMutation:
    """A benchmark-owned mutation used to prove a checker is not too weak.

    The mutation script runs in a private copy of the workspace, never in the
    actor's workspace, and its existence is not part of the task prompt.  The
    expected statuses describe what the visible grader and hidden shadow
    checker must observe for the mutation to be a valid strength test.
    """

    name: str
    script: str
    expect_visible: str | None = "FAIL"
    expect_shadow: str | None = None


@dataclass(frozen=True)
class Task:
    name: str
    prompt: str
    setup: dict[str, str]
    grade: str
    # Optional host-owned evidence checks.  ``baseline`` must fail before the
    # actor starts (the task is genuinely broken); ``pass_to_pass`` must pass
    # both before and after the actor (unrelated behavior is protected).
    baseline: str | None = None
    pass_to_pass: str | None = None
    shadow: str | None = None
    budget: int = 20
    max_success_iterations: int | None = None
    strength_mutations: tuple[GraderMutation, ...] = ()


def _write_setup(root: Path, setup: dict[str, str]) -> None:
    for relative, content in setup.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _protected_task_paths(task: Task) -> tuple[str, ...]:
    """Return supplied test artifacts that the actor must not rewrite."""
    protected = []
    for relative in task.setup:
        path = Path(relative)
        lower_parts = {part.lower() for part in path.parts}
        name = path.name.lower()
        if (
            "tests" in lower_parts
            or name.startswith("test_")
            or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
        ):
            protected.append(relative)
    return tuple(sorted(protected))


def _snapshot_protected_paths(root: Path, paths: tuple[str, ...]) -> dict[str, str | None]:
    """Hash supplied test files before the actor gets workspace access."""
    snapshot = {}
    for relative in paths:
        path = root / relative
        content = path.read_bytes() if path.exists() else None
        snapshot[relative] = hashlib.sha256(content).hexdigest() if content is not None else None
    return snapshot


def _check_protected_paths(
    root: Path, snapshot: dict[str, str | None]
) -> tuple[bool, str]:
    """Detect deleted or rewritten supplied tests after an agent run."""
    changed = []
    for relative, before in snapshot.items():
        path = root / relative
        after_content = path.read_bytes() if path.exists() else None
        after = hashlib.sha256(after_content).hexdigest() if after_content is not None else None
        if after != before:
            changed.append(relative + (" (deleted)" if after is None else " (changed)"))
    if changed:
        return False, "supplied test integrity violation: " + ", ".join(changed)
    return True, "supplied test artifacts unchanged"


def _grade(task: Task, root: Path) -> GradeResult:
    """Run the task grader outside the actor's workspace."""
    return run_python_grader(task.grade, root, timeout_seconds=45.0)


def _run_task_evidence(source: str | None, root: Path, *, phase: str) -> dict | None:
    """Run one host-owned pre/post contract and return auditable evidence."""
    if not source:
        return None
    result = run_python_grader(source, root, timeout_seconds=45.0, phase=phase)
    return result.as_dict()


def _baseline_contract_valid(task: Task, root: Path) -> tuple[bool, dict[str, dict]]:
    """Require the declared initial state before spending model calls."""
    evidence: dict[str, dict] = {}
    baseline_source = task.grade if task.baseline == "__acceptance__" else task.baseline
    baseline = _run_task_evidence(baseline_source, root, phase="baseline")
    if baseline is not None:
        evidence["fail_to_pass"] = {
            "expected": "FAIL",
            "observed": baseline,
            "valid": baseline["status"] == "FAIL",
        }
        if baseline["status"] != "FAIL":
            return False, evidence
    pass_to_pass = _run_task_evidence(task.pass_to_pass, root, phase="pass_to_pass_before")
    if pass_to_pass is not None:
        evidence["pass_to_pass_before"] = {
            "expected": "PASS",
            "observed": pass_to_pass,
            "valid": pass_to_pass["status"] == "PASS",
        }
        if pass_to_pass["status"] != "PASS":
            return False, evidence
    return True, evidence


def _post_task_evidence(task: Task, root: Path, evidence: dict[str, dict]) -> bool:
    """Run post-state regression evidence after the acceptance grader."""
    if not task.pass_to_pass:
        return True
    result = _run_task_evidence(task.pass_to_pass, root, phase="pass_to_pass_after")
    evidence["pass_to_pass_after"] = {
        "expected": "PASS",
        "observed": result,
        "valid": bool(result and result["status"] == "PASS"),
    }
    return bool(result and result["status"] == "PASS")


def _run_shadow_evidence(task: Task, root: Path) -> dict | None:
    """Run a host-owned hidden/shadow acceptance check.

    Shadow checks are intentionally not part of ``task.prompt``, are never
    written into the actor workspace, and are never included in verifier-repair
    feedback.  Their only role is to reject plausible-but-wrong artifacts that
    satisfy the visible acceptance check.  This function returns telemetry for
    the benchmark record, not a message for the actor.
    """
    if not task.shadow:
        return None
    result = run_python_grader(
        task.shadow,
        root,
        timeout_seconds=45.0,
        phase="shadow_acceptance",
    )
    return result.as_dict()


def _apply_shadow_result(artifact_passed: bool, shadow_result: dict | None) -> tuple[bool, str | None]:
    """Apply a hidden acceptance result without exposing its detail to the actor."""
    if shadow_result is None:
        return artifact_passed, None
    if shadow_result["status"] != "PASS":
        if not artifact_passed:
            return False, None
        return False, "hidden acceptance evidence failed"
    return artifact_passed, None


def _run_grader_mutation(
    task: Task,
    root: Path,
    mutation: GraderMutation,
) -> dict:
    """Apply one benchmark-owned mutation in an isolated copy and grade it."""
    with tempfile.TemporaryDirectory(prefix="novelty-grader-mutation-") as tmp:
        mutated = Path(tmp)
        shutil.copytree(root, mutated, dirs_exist_ok=True)
        applied = subprocess.run(
            [sys.executable, "-c", mutation.script],
            cwd=str(mutated),
            text=True,
            capture_output=True,
            timeout=30,
        )
        if applied.returncode != 0:
            return {
                "name": mutation.name,
                "valid": False,
                "reason": "mutation script failed: " + _bounded_grader_detail(
                    applied.stdout, applied.stderr
                ),
                "visible": None,
                "shadow": None,
            }

        visible = _grade(task, mutated).as_dict()
        shadow = _run_shadow_evidence(task, mutated)
        expected_visible = mutation.expect_visible
        expected_shadow = mutation.expect_shadow
        visible_ok = (
            expected_visible is None
            or (
                expected_visible == "PASS"
                and visible["status"] == "PASS"
            )
            or (
                expected_visible == "FAIL"
                and visible["status"] != "PASS"
            )
        )
        shadow_ok = (
            expected_shadow is None
            or (
                expected_shadow == "PASS"
                and shadow is not None
                and shadow["status"] == "PASS"
            )
            or (
                expected_shadow == "FAIL"
                and shadow is not None
                and shadow["status"] != "PASS"
            )
        )
        rejected = bool(
            (expected_visible == "FAIL" and visible["status"] != "PASS")
            or (expected_shadow == "FAIL" and shadow is not None and shadow["status"] != "PASS")
        )
        return {
            "name": mutation.name,
            "valid": visible_ok and shadow_ok and rejected,
            "visible": visible,
            "shadow": shadow,
        }


def _bounded_grader_detail(stdout: str, stderr: str) -> str:
    """Return a bounded diagnostic for mutation-script failures."""
    return ((stdout or "") + (stderr or "")).strip()[-2000:]


def _grader_strength_valid(task: Task, root: Path) -> tuple[bool, dict[str, dict]]:
    """Verify each benchmark-owned mutation is rejected by at least one layer."""
    evidence: dict[str, dict] = {}
    if not task.strength_mutations:
        return True, evidence
    for mutation in task.strength_mutations:
        result = _run_grader_mutation(task, root, mutation)
        evidence[mutation.name] = result
        if not result["valid"]:
            return False, evidence
    return True, evidence


def _metrics(output: str) -> dict:
    iterations = len(re.findall(r"🌀 \[Iteration \d+/\d+\]", output))
    tool_calls = len(re.findall(r"^🔧 .+$", output, re.MULTILINE))
    failures = len(re.findall(r"^.*(?:ERROR|REJECTED).*$", output, re.MULTILINE))
    novelty = None
    lines = re.findall(r"🧬 \[novelty metrics\] (\{.*\})", output)
    if lines:
        try:
            novelty = json.loads(lines[-1])
        except json.JSONDecodeError:
            novelty = {"parse_error": True}
    repair = None
    repair_lines = re.findall(r"🧰 \[repair metrics\] (\{.*\})", output)
    if repair_lines:
        try:
            repair = json.loads(repair_lines[-1])
        except json.JSONDecodeError:
            repair = {"parse_error": True}
    timing = None
    timing_lines = re.findall(r"⏱️ \[agent timing\] (\{.*\})", output)
    if timing_lines:
        try:
            timing = json.loads(timing_lines[-1])
        except json.JSONDecodeError:
            timing = {"parse_error": True}
    return {
        "iterations": iterations,
        "tool_calls": tool_calls,
        "failure_lines": failures,
        "done_signal": "✅ DONE" in output,
        "novelty": novelty,
        "repair": repair,
        "timing": timing,
    }


def _profile_limits(profile: str, iterations: int, chat_timeout: float,
                    run_timeout: float) -> tuple[int, float, float]:
    """Return bounded limits for a development smoke run.

    The full profile remains the default.  Smoke is intentionally a cap, not a
    different task or grader: it lets a developer test first mutation and one
    validation boundary without accidentally spending a full overnight
    budget on a known-stalled model.
    """
    if profile == "smoke":
        return min(iterations, 8), min(chat_timeout, 45.0), min(run_timeout, 300.0)
    return iterations, chat_timeout, run_timeout


def _run_completed(timed_out: bool, returncode: int | None) -> bool:
    """Whether the actor reached the benchmark handoff boundary normally."""
    return not timed_out and returncode == 0


def _scorecard_passed(artifact_passed: bool, finish_called: bool, run_completed: bool,
                      handoff_reconciled: bool = False) -> bool:
    """Require a correct artifact and clean exit, with explicit recovery semantics.

    A provider can die after the independent verifier has accepted the
    workspace but before the actor emits ``finish_task``. In that narrow case
    the verifier is the authoritative handoff boundary. Keep the actual
    ``finish_called`` bit visible while allowing the scorecard to distinguish
    this deterministic reconciliation from a model-completed run.
    """
    return bool(artifact_passed and run_completed and (finish_called or handoff_reconciled))


def _handoff_reconciled(artifact_passed: bool, run_completed: bool, output: str) -> bool:
    """Return true only for a clean artifact plus an explicit provider-loss stop."""
    return bool(artifact_passed and run_completed and _provider_interrupted(output or ""))


def _verifier_repair_prompt(task: Task, detail: str) -> str:
    """Turn independent-grader evidence into one generic bounded repair pass."""
    manifest_hint = _missing_dependency_manifest_hint(detail)
    manifest_block = ""
    if manifest_hint:
        manifest_block = (
            "\n\n## Missing dependency declaration\n"
            "The independent verifier reported a missing dependency declaration. "
            "Create or update the conventional manifest for this project "
            f"({manifest_hint}) before running the behavioral check. Do not skip "
            "the manifest or rely on an ad hoc runtime probe to satisfy the verifier."
        )
    return (
        task.prompt
        + "\n\n## Independent verifier feedback\n"
        + "The previous handoff was rejected by the independent verifier. Treat the exact feedback "
          "below as authoritative failure evidence. Inspect the current workspace, repair the "
          "implementation or missing artifact that caused it, run a focused check that covers the "
          "failure, and call finish_task promptly after the repair. The harness will rerun the "
          "independent verifier after handoff; do not invoke or rewrite its generated grader, and "
          "do not rewrite supplied task files.\n"
        + detail[-3000:]
        + manifest_block
    )


def _missing_dependency_manifest_hint(detail: str) -> str:
    """Return a generic manifest hint when a verifier reports a missing manifest."""
    lowered = str(detail or "").lower()
    manifest_names = {
        "package.json": "package.json",
        "requirements.txt": "requirements.txt",
        "pyproject.toml": "pyproject.toml",
        "cargo.toml": "Cargo.toml",
        "go.mod": "go.mod",
        "pom.xml": "pom.xml",
    }
    for marker, display in manifest_names.items():
        if marker in lowered:
            return display
    return ""


def _provider_interrupted(output: str) -> bool:
    """Identify a clean actor stop caused by the model transport."""
    lower = str(output or "").lower()
    return any(marker in lower for marker in (
        "provider unavailable; ending run cleanly",
        "acting-model turn failed",
        "remote end closed connection without response",
        "remotedisconnected",
    ))


def _run_preflight(timeout_seconds: float = 30.0) -> tuple[bool, str]:
    """Run cheap deterministic guard tests before any expensive model run."""
    command = [
        sys.executable, "-m", "unittest",
        "tests.test_agent_tools", "tests.test_adversarial_preflight",
        "tests.test_novelty_context", "tests.test_run_monitor",
    ]
    try:
        proc = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"preflight timed out after {timeout_seconds:.1f}s: {exc}"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output[-4000:]


def _event_kind(line: str) -> str:
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


def _live_summary(line: str) -> str:
    """Return a compact user-facing line; the monitor JSONL keeps raw text."""
    kind = _event_kind(line)
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


def _descendant_pids(pid: int) -> list[int]:
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
        result.extend(_descendant_pids(child))
    return result


def _terminate_process_tree(proc) -> None:
    """Terminate the actor and descendants, including detached service sessions."""
    descendants = _descendant_pids(proc.pid)
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
        for pid in reversed(_descendant_pids(proc.pid)):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        proc.wait()


def _stream_agent(proc, started: float, run_timeout: float, monitor_path: Path):
    """Stream every agent line while retaining the watchdog."""
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
        print(_live_summary(line), flush=True)
        event = {
            "elapsed_s": round(time.monotonic() - started, 3),
            "kind": _event_kind(line),
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
            _terminate_process_tree(proc)
        else:
            proc.wait()
        selector.close()
        monitor.close()
    return "".join(chunks), timed_out, proc.returncode


TASKS = {
    "wifi_simulator": Task(
        name="wifi_simulator",
        prompt=(
            "Create a polished, self-contained offline web GUI named wifi-simulator.html. "
            "It must be an educational simulation only: do not access real Wi-Fi, send packets, "
            "open sockets, invoke processes, or make network requests. Simulate an access point, "
            "clients, and a defensive monitor entirely in browser memory. Include a clear safety "
            "notice, scenario controls for Normal traffic, Deauthentication simulation, Rogue AP "
            "simulation, and Recovery, a visual topology showing AP/client connections, a live event "
            "timeline, and a defensive explanation that identifies observable indicators and safe "
            "mitigations. Add Start, Pause, Reset, and scenario controls; make state changes visible "
            "and accessible. Use inline CSS and JavaScript only, with no external dependencies. "
            "Open the file locally or serve it with Python's standard-library server and verify the "
            "primary controls work before calling finish_task."
        ),
        setup={
            "README.md": (
                "# Offline Wi-Fi simulator\n\n"
                "Build a browser-only educational simulator. It must never interact with real networks.\n"
            ),
        },
        grade=(
            "from pathlib import Path\n"
            "import re\n"
            "html = Path('wifi-simulator.html').read_text(encoding='utf-8')\n"
            "lower = html.lower()\n"
            "assert len(html) > 5000, 'simulator needs a considered UI and interaction model'\n"
            "required = ['start', 'pause', 'reset', 'normal', 'deauthentication', 'rogue', 'recovery',\n"
            "            'timeline', 'access point', 'client', 'mitigation']\n"
            "missing = [x for x in required if x not in lower]\n"
            "assert not missing, f'missing simulator concepts: {missing}'\n"
            "forbidden = [r'\\bimport\\s+socket', r'\\bfrom\\s+socket', r'\\bfetch\\s*\\(',\n"
            "             r'\\bXMLHttpRequest\\b', r'\\bWebSocket\\b', r'\\bscapy\\b',\n"
            "             r'child_process', r'\\bexec\\s*\\(', r'\\bspawn\\s*\\(']\n"
            "hits = [p for p in forbidden if re.search(p, lower)]\n"
            "assert not hits, f'unsafe runtime primitive found: {hits}'\n"
            "assert re.search(r'<(canvas|svg)\\b', lower), 'topology needs a visual surface'\n"
            "assert re.search(r'addEventListener|onclick|onchange', html), 'controls are not wired'\n"
            "assert re.search(r'(offline|simulation only|does not access|no real network)', lower)\n"
            "assert re.search(r'(indicator|detect|defen|mitigat)', lower)\n"
        ),
        baseline="__acceptance__",
        budget=20,
    ),
    "3d_scene": Task(
        name="3d_scene",
        prompt=(
            "Create a short but beautiful 3D scene as scene.html in this empty project. Use Three.js "
            "from its browser CDN, with a perspective camera, WebGLRenderer, responsive resize handling, "
            "lighting, shadows, animation loop, and a deliberate composition: a glowing crystalline orb "
            "hovering above a reflective floor with a few orbiting objects and a dark atmospheric background. "
            "Make it polished and self-contained in one HTML file, with a title and brief on-screen caption. "
            "Serve it locally with Python's standard-library HTTP server and verify that it loads before "
            "calling finish_task."
        ),
        setup={
            "README.md": (
                "# 3D scene\n\n"
                "Create one polished scene.html. The evaluator checks the artifact and serves it locally.\n"
            ),
        },
        grade=(
            "from pathlib import Path\n"
            "import re, subprocess, sys, time, urllib.request\n"
            "html = Path('scene.html').read_text(encoding='utf-8')\n"
            "required = ['three', 'PerspectiveCamera', 'WebGLRenderer', 'requestAnimationFrame',\n"
            "            'AmbientLight', 'DirectionalLight', 'PointLight', 'MeshStandardMaterial']\n"
            "missing = [x for x in required if x not in html]\n"
            "assert not missing, f'missing scene elements: {missing}'\n"
            "assert len(html) > 2500, 'scene is too small to be a considered composition'\n"
            "assert re.search(r'(orb|sphere|icosa|crystal)', html, re.I)\n"
            "assert re.search(r'(floor|plane)', html, re.I)\n"
            "port = 18766\n"
            "proc = subprocess.Popen([sys.executable, '-m', 'http.server', str(port)],\n"
            "                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
            "try:\n"
            "    for _ in range(20):\n"
            "        try:\n"
            "            with urllib.request.urlopen(f'http://127.0.0.1:{port}/scene.html', timeout=1) as r:\n"
            "                served = r.read().decode()\n"
            "            break\n"
            "        except Exception:\n"
            "            time.sleep(0.1)\n"
            "    else:\n"
            "        raise AssertionError('scene did not serve')\n"
            "    assert 'WebGLRenderer' in served\n"
            "finally:\n"
            "    proc.terminate()\n"
            "    try: proc.wait(timeout=3)\n"
            "    except subprocess.TimeoutExpired: proc.kill()\n"
        ),
        baseline="__acceptance__",
        budget=14,
    ),
    "real_app": Task(
        name="real_app",
        prompt=(
            "Create a real working local web app in this empty project. Build server.py using only "
            "the Python standard library. It must start with `python3 server.py --port PORT`, serve "
            "a useful Todo app at GET /, expose GET /health returning JSON with status=ok, support "
            "POST /api/tasks with JSON {title: string} returning the created task as JSON, and support "
            "GET /api/tasks returning all tasks as JSON. Keep data in memory, validate empty titles, "
            "include a simple usable HTML interface, and test the app by starting it and making HTTP "
            "requests before calling finish_task."
        ),
        setup={
            "README.md": (
                "# Todo app\n\n"
                "Create a standard-library-only web app. The evaluator will run server.py.\n"
            ),
        },
        grade=(
            "import json, subprocess, sys, time, urllib.request\n"
            "port = 18765\n"
            "proc = subprocess.Popen([sys.executable, 'server.py', '--port', str(port)],\n"
            "                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
            "base = f'http://127.0.0.1:{port}'\n"
            "try:\n"
            "    for _ in range(30):\n"
            "        try:\n"
            "            with urllib.request.urlopen(base + '/health', timeout=1) as r:\n"
            "                health = json.loads(r.read())\n"
            "            break\n"
            "        except Exception:\n"
            "            time.sleep(0.1)\n"
            "    else:\n"
            "        raise AssertionError('server did not start')\n"
            "    assert health.get('status') == 'ok', health\n"
            "    with urllib.request.urlopen(base + '/', timeout=2) as r:\n"
            "        html = r.read().decode()\n"
            "    assert '<html' in html.lower() and 'todo' in html.lower(), (\n"
            "        'GET / did not return expected Todo HTML: ' + repr(html[:500])\n"
            "    )\n"
            "    req = urllib.request.Request(base + '/api/tasks', data=json.dumps({'title':'Ship it'}).encode(),\n"
            "                                 headers={'Content-Type':'application/json'}, method='POST')\n"
            "    with urllib.request.urlopen(req, timeout=2) as r:\n"
            "        created = json.loads(r.read())\n"
            "    assert created.get('title') == 'Ship it' and created.get('id')\n"
            "    with urllib.request.urlopen(base + '/api/tasks', timeout=2) as r:\n"
            "        tasks = json.loads(r.read())\n"
            "    assert any(t.get('title') == 'Ship it' for t in tasks), tasks\n"
            "finally:\n"
            "    proc.terminate()\n"
            "    try: proc.wait(timeout=3)\n"
            "    except subprocess.TimeoutExpired: proc.kill()\n"
        ),
        baseline="__acceptance__",
        budget=24,
    ),
    "cascading_loop": Task(
        name="cascading_loop",
        prompt=(
            "Repair the isolated Python project. Run the provided test_metrics.py and use each "
            "new failure to guide the smallest correct repair. There are multiple sequential "
            "failures, so rerun the test after every change, preserve working code, and call "
            "finish_task only after the test exits successfully. Do not replace the test or "
            "weaken its assertions."
        ),
        setup={
            "target_code.py": (
                "# target_code.py\n"
                "def calculate_metrics(data_list):\n"
                "    # BUG 1: Syntax Error (Missing closing parenthesis)\n"
                "    total = sum(data_list\n"
                "\n"
                "    # BUG 2: Logical/Type Error (Dividing by a string instead of an int)\n"
                "    average = total / \"2\" \n"
                "    return average\n"
            ),
            "test_metrics.py": (
                "# test_metrics.py\n"
                "from target_code import calculate_metrics\n"
                "def test_calculation():\n"
                "    assert calculate_metrics([10, 20]) == 15\n"
            ),
        },
        grade=(
            "import subprocess, sys\n"
            "pytest_probe = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'test_metrics.py'],\n"
            "                              text=True, capture_output=True, timeout=30)\n"
            "if pytest_probe.returncode == 0:\n"
            "    raise SystemExit(0)\n"
            "if 'No module named pytest' not in (pytest_probe.stdout + pytest_probe.stderr):\n"
            "    raise AssertionError((pytest_probe.stdout + pytest_probe.stderr)[-4000:])\n"
            "direct = subprocess.run([sys.executable, '-c',\n"
            "    'from test_metrics import test_calculation; test_calculation()'],\n"
            "    text=True, capture_output=True, timeout=30)\n"
            "assert direct.returncode == 0, (direct.stdout + direct.stderr)[-4000:]\n"
        ),
        baseline="__acceptance__",
        budget=8,
        max_success_iterations=3,
    ),
    "multi_file_transaction": Task(
        name="multi_file_transaction",
        prompt=(
            "Refactor the isolated Python project so the supplied test passes. The requested change "
            "intentionally spans the two implementation modules: inspect their relationship and use "
            "the test failure as the contract. Preserve the supplied test, keep the intermediate edit "
            "recoverable, and do not stop after changing only one side of the dependency. Run the "
            "tests after the coherent refactor and call finish_task only after they pass."
        ),
        setup={
            "core_math.py": (
                "class Symbol:\n"
                "    def __init__(self):\n"
                "        self.is_real = True\n"
            ),
            "matrix_solver.py": (
                "from core_math import Symbol\n\n"
                "def solve(s):\n"
                "    return s.is_real\n"
            ),
            "test_solver.py": (
                "from core_math import Symbol\n"
                "from matrix_solver import solve\n\n"
                "def test_symbolic_contract():\n"
                "    symbol = Symbol()\n"
                "    assert not hasattr(symbol, 'is_real')\n"
                "    assert getattr(symbol, 'is_symbolic', False) is True\n"
                "    assert solve(symbol) is True\n"
            ),
        },
        grade=(
            "from core_math import Symbol\n"
            "from matrix_solver import solve\n"
            "symbol = Symbol()\n"
            "assert not hasattr(symbol, 'is_real')\n"
            "assert getattr(symbol, 'is_symbolic', False) is True\n"
            "assert solve(symbol) is True\n"
            "solver_source = open('matrix_solver.py', encoding='utf-8').read()\n"
            "assert 'is_symbolic' in solver_source and 'is_real' not in solver_source\n"
        ),
        baseline="__acceptance__",
        budget=10,
        max_success_iterations=6,
    ),
    "websocket_chat": Task(
        name="websocket_chat",
        prompt=(
            "Repair the isolated full-stack WebSocket chat application. Inspect the supplied "
            "server.js and index.html together, identify the connection, Buffer/string, payload "
            "schema, and disconnect-safe broadcast failures, then make the smallest production-"
            "quality repair. The browser and server must share one JSON contract: message payloads "
            "use {type: 'message', text: string}, and ping uses {type: 'ping'} with a {type: 'pong'} "
            "response. Handle Node ws message data as text safely, only send to OPEN clients, and "
            "keep the server stable when a peer disconnects during broadcast. Use ws:// (or wss://) "
            "in the browser, avoid unsafe HTML interpolation, and provide a package.json declaring "
            "the ws dependency if needed. Listen on process.env.PORT || 8080 so local validation can "
            "use an isolated port. You may install dependencies in the isolated workspace. "
            "Run a real local server/client smoke test, then call finish_task only after it passes."
        ),
        setup={
            "server.js": (
                "const WebSocket = require('ws');\n\n"
                "// Intentional configuration issue\n"
                "const wss = new WebSocket.Server({ port: \"8080\" });\n\n"
                "console.log(\"Chat server running on port 8080\");\n\n"
                "wss.on('connection', (ws) => {\n"
                "    console.log(\"New client connected\");\n\n"
                "    ws.on('message', (message) => {\n"
                "        // Warning: 'message' data type handling might be unstable depending on the environment\n"
                "        if (message.includes(\"ping\")) {\n"
                "            ws.send(JSON.stringify({ type: \"pong\" }));\n"
                "            return;\n"
                "        }\n\n"
                "        try {\n"
                "            // Unsafe broadcast loop\n"
                "            wss.clients.forEach((client) => {\n"
                "                client.send(message);\n"
                "            });\n"
                "        } catch (err) {\n"
                "            console.error(\"Broadcast failed:\", err);\n"
                "        }\n"
                "    });\n"
                "});\n"
            ),
            "index.html": (
                "<!DOCTYPE html>\n<html>\n<head>\n    <title>Broken Chat</title>\n"
                "    <script src=\"https://jsdelivr.net\"></script>\n</head>\n"
                "<body class=\"bg-gray-900 text-white p-8\">\n"
                "    <div class=\"max-w-md mx-auto bg-gray-800 p-6 rounded-lg shadow-xl\">\n"
                "        <h1 class=\"text-xl font-bold mb-4\">Secure Chat Portal</h1>\n"
                "        <div id=\"chatBox\" class=\"h-64 bg-gray-950 p-4 rounded mb-4 overflow-y-auto font-mono text-green-400\"></div>\n"
                "        <div class=\"flex gap-2\">\n"
                "            <input id=\"msgInput\" type=\"text\" class=\"flex-1 bg-gray-700 p-2 rounded text-white\" placeholder=\"Type a message...\">\n"
                "            <button id=\"sendBtn\" class=\"bg-blue-600 px-4 py-2 rounded font-bold hover:bg-blue-500\">Send</button>\n"
                "        </div>\n    </div>\n\n    <script>\n"
                "        const socket = new WebSocket(\"http://localhost:8080\");\n\n"
                "        socket.onmessage = (event) => {\n"
                "            const data = JSON.parse(event.data);\n"
                "            const chatBox = document.getElementById(\"chatBox\");\n"
                "            chatBox.innerHTML += `<div>> ${data.text}</div>`;\n"
                "        };\n\n"
                "        document.getElementById(\"sendBtn\").onclick = () => {\n"
                "            const input = document.getElementById(\"msgInput\");\n"
                "            socket.send(input.value);\n"
                "            input.value = \"\";\n"
                "        };\n    </script>\n</body>\n</html>\n"
            ),
        },
        grade=(
            "from pathlib import Path\n"
            "import json, os, signal, subprocess, sys, tempfile, time\n"
            "server = Path('server.js').read_text(encoding='utf-8')\n"
            "html = Path('index.html').read_text(encoding='utf-8')\n"
            "assert 'new WebSocket(\"http://' not in html and \"new WebSocket('http://\" not in html\n"
            "assert 'JSON.stringify' in html and 'JSON.parse' in html\n"
            "assert 'textContent' in html and 'innerHTML +=' not in html\n"
            "assert 'readyState' in server and 'OPEN' in server\n"
            "assert 'Buffer' in server or 'toString' in server\n"
            "package = Path('package.json')\n"
            "assert package.exists(), 'package.json must declare the runtime dependency'\n"
            "manifest = json.loads(package.read_text(encoding='utf-8'))\n"
            "deps = {**manifest.get('dependencies', {}), **manifest.get('devDependencies', {})}\n"
            "assert 'ws' in deps, 'package.json must declare ws'\n"
            "install = subprocess.run(['npm', 'install', '--no-audit', '--no-fund'], text=True, capture_output=True, timeout=60)\n"
            "assert install.returncode == 0, (install.stdout + install.stderr)[-3000:]\n"
            "probe = Path('.websocket_probe.cjs')\n"
            "probe.write_text('''const WebSocket = require('ws');\n"
            "const port = 18767;\n"
            "const a = new WebSocket(`ws://127.0.0.1:${port}`);\n"
            "const b = new WebSocket(`ws://127.0.0.1:${port}`);\n"
            "let messages = [];\n"
            "let pong = false;\n"
            "function fail(message) { console.error(message); process.exit(1); }\n"
            "function done() { if (messages.some(x => x.type === 'message' && x.text === 'hello') && pong) process.exit(0); }\n"
            "b.on('message', raw => { try { const value = JSON.parse(raw.toString()); messages.push(value); done(); } catch (e) { fail(e.message); } });\n"
            "a.on('open', () => { a.send(JSON.stringify({type:'ping'})); setTimeout(() => a.send(JSON.stringify({type:'message', text:'hello'})), 100); });\n"
            "a.on('message', raw => { try { if (JSON.parse(raw.toString()).type === 'pong') pong = true; done(); } catch (e) { fail(e.message); } });\n"
            "a.on('error', e => fail(e.message)); b.on('error', e => fail(e.message));\n"
            "setTimeout(() => { b.close(); setTimeout(() => a.send(JSON.stringify({type:'message', text:'after-close'})), 100); }, 500);\n"
            "setTimeout(() => fail('timed out waiting for chat exchange'), 2500);\n''', encoding='utf-8')\n"
            "port = 18767\n"
            "env = os.environ.copy(); env['PORT'] = str(port)\n"
            "proc = subprocess.Popen(['node', 'server.js'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
            "try:\n"
            "    for _ in range(30):\n"
            "        if proc.poll() is not None: break\n"
            "        time.sleep(0.1)\n"
            "    assert proc.poll() is None, (proc.stdout.read().decode() + proc.stderr.read().decode())[-3000:]\n"
            "    run = subprocess.run(['node', str(probe)], text=True, capture_output=True, timeout=8)\n"
            "    assert run.returncode == 0, (run.stdout + run.stderr)[-3000:]\n"
            "    assert proc.poll() is None, 'server crashed during disconnect-safe broadcast'\n"
            "finally:\n"
            "    proc.terminate()\n"
            "    try: proc.wait(timeout=3)\n"
            "    except subprocess.TimeoutExpired: proc.kill()\n"
            "    probe.unlink(missing_ok=True)\n"
        ),
        baseline="__acceptance__",
        budget=18,
    ),
    "bug_repair": Task(
        name="bug_repair",
        prompt=(
            "Fix the bug in app/text.py. The public function normalize_email must "
            "trim whitespace and lowercase the address while preserving a plus tag. "
            "Run the available tests or create a focused check, then call finish_task."
        ),
        setup={
            "app/__init__.py": "",
            "app/text.py": (
                "def normalize_email(value):\n"
                "    # Known bug: this destroys plus tags and keeps surrounding spaces.\n"
                "    return value.strip().split('+')[0].lower()\n"
            ),
            "test_text.py": (
                "from app.text import normalize_email\n\n"
                "def test_normalize_email():\n"
                "    assert normalize_email('  Alice+news@Example.COM  ') == 'alice+news@example.com'\n"
            ),
        },
        grade=(
            "from app.text import normalize_email\n"
            "assert normalize_email('  Alice+news@Example.COM  ') == 'alice+news@example.com'\n"
            "assert normalize_email('BOB@EXAMPLE.COM') == 'bob@example.com'\n"
        ),
        baseline="__acceptance__",
        budget=18,
    ),
    "feature": Task(
        name="feature",
        prompt=(
            "Add a function app.inventory.low_stock(items, threshold) that returns "
            "a new list of item dictionaries whose quantity is strictly below the "
            "threshold, sorted by quantity ascending. Do not mutate the input. Add "
            "focused tests and run them before finishing."
        ),
        setup={
            "app/__init__.py": "",
            "app/inventory.py": (
                "def total_value(items):\n"
                "    return sum(item['quantity'] * item['price'] for item in items)\n"
            ),
            "test_inventory.py": (
                "from app.inventory import total_value\n\n"
                "def test_total_value():\n"
                "    assert total_value([{'quantity': 2, 'price': 3}]) == 6\n"
            ),
        },
        grade=(
            "from app.inventory import low_stock\n"
            "items = [{'name':'a','quantity':5},{'name':'b','quantity':1},{'name':'c','quantity':3}]\n"
            "out = low_stock(items, 5)\n"
            "assert [x['name'] for x in out] == ['b','c']\n"
            "assert items == [{'name':'a','quantity':5},{'name':'b','quantity':1},{'name':'c','quantity':3}]\n"
        ),
        baseline="__acceptance__",
        pass_to_pass=(
            "from app.inventory import total_value\n"
            "assert total_value([{'quantity': 2, 'price': 3}]) == 6\n"
        ),
        shadow=(
            "from app.inventory import low_stock\n"
            "items = [{'name':'a','quantity':4},{'name':'b','quantity':2},{'name':'c','quantity':6}]\n"
            "out = low_stock(items, 6)\n"
            "assert [x['name'] for x in out] == ['b','a']\n"
            "assert items == [{'name':'a','quantity':4},{'name':'b','quantity':2},{'name':'c','quantity':6}]\n"
        ),
        strength_mutations=(
            GraderMutation(
                name="hardcoded_visible_example",
                script=(
                    "from pathlib import Path\n"
                    "Path('app/inventory.py').write_text(\n"
                    "    \"def low_stock(items, threshold):\\n\"\n"
                    "    \"    return [{'name':'b','quantity':1},{'name':'c','quantity':3}]\\n\"\n"
                    ")\n"
                ),
                expect_visible="PASS",
                expect_shadow="FAIL",
            ),
        ),
        budget=20,
    ),
    "data_report": Task(
        name="data_report",
        prompt=(
            "Build report.py. Read sales.csv and write report.json containing total_sales "
            "(sum of amount), sales_by_region (sum grouped by region), and top_product "
            "(product with the largest total amount). Use only the Python standard library. "
            "Run a check against the supplied data before finishing."
        ),
        setup={
            "sales.csv": (
                "region,product,amount\nNorth,Book,10\nSouth,Pen,4\nNorth,Pen,6\nSouth,Book,8\n"
            ),
            "README.md": "The output must be report.json in the project root.\n",
        },
        grade=(
            "import json\n"
            "data = json.load(open('report.json'))\n"
            "assert data['total_sales'] == 28\n"
            "assert data['sales_by_region'] == {'North': 16, 'South': 12}\n"
            "assert data['top_product'] == 'Book'\n"
        ),
        baseline="__acceptance__",
        pass_to_pass=(
            "from pathlib import Path\n"
            "assert Path('sales.csv').read_text(encoding='utf-8').startswith('region,product,amount')\n"
        ),
        budget=22,
    ),
    "lru_cache": Task(
        name="lru_cache",
        prompt=(
            "Repair this isolated Python project so the supplied test_suite.py passes. "
            "Inspect the implementation modules and run the supplied tests to discover the "
            "required behavior. The public APIs must remain usable by both modules. Do not "
            "modify, delete, or weaken any assertion in test_suite.py. Make the smallest "
            "coherent implementation changes across the project, rerun the tests after "
            "repairing them, and call finish_task only after the suite passes."
        ),
        setup={
            "cache.py": (
                "import time\n\n"
                "class LRUCache:\n"
                "    def __init__(self, capacity: int, ttl_seconds: float = None):\n"
                "        self.capacity = capacity\n"
                "        self.ttl = ttl_seconds\n"
                "        self.storage = {}\n"
                "        self.timestamps = {}\n"
                "        self.eviction_hook = None\n\n"
                "    def get(self, key):\n"
                "        if key not in self.storage:\n"
                "            return -1\n"
                "        return self.storage[key]\n\n"
                "    def put(self, key, value):\n"
                "        if len(self.storage) >= self.capacity:\n"
                "            evict_key = next(iter(self.storage))\n"
                "            del self.storage[evict_key]\n"
                "        self.storage[key] = value\n"
                "        self.timestamps[key] = time.time()\n\n"
                "    def register_eviction_hook(self, callback):\n"
                "        self.eviction_hook = callback\n"
            ),
            "storage_manager.py": (
                "from cache import LRUCache\n\n"
                "class PersistentStorageManager:\n"
                "    def __init__(self, cache_capacity: int, ttl: float):\n"
                "        self.cache = LRUCache(cache_capacity, ttl_seconds=ttl)\n"
                "        self.cold_storage = {}\n\n"
                "    def fetch(self, key):\n"
                "        val = self.cache.get(key)\n"
                "        if val != -1:\n"
                "            return val\n"
                "        return self.cold_storage.get(key, None)\n\n"
                "    def store(self, key, value):\n"
                "        self.cache.put(key, value)\n"
            ),
            "test_suite.py": (
                "import time\n"
                "import pytest\n"
                "from cache import LRUCache\n"
                "from storage_manager import PersistentStorageManager\n\n"
                "def test_basic_lru_order():\n"
                "    c = LRUCache(2)\n"
                "    c.put(1, \"one\")\n"
                "    c.put(2, \"two\")\n"
                "    c.get(1)\n"
                "    c.put(3, \"three\")\n"
                "    assert c.get(2) == -1\n"
                "    assert c.get(1) == \"one\"\n\n"
                "def test_ttl_expiration():\n"
                "    c = LRUCache(2, ttl_seconds=0.1)\n"
                "    c.put(1, \"val\")\n"
                "    time.sleep(0.15)\n"
                "    assert c.get(1) == -1\n\n"
                "def test_cascading_eviction_to_cold_storage():\n"
                "    mgr = PersistentStorageManager(cache_capacity=2, ttl=10.0)\n"
                "    mgr.store(\"A\", 1)\n"
                "    mgr.store(\"B\", 2)\n"
                "    mgr.store(\"C\", 3)\n"
                "    assert mgr.cache.get(\"A\") == -1\n"
                "    assert mgr.fetch(\"A\") == 1\n"
            ),
        },
        grade=(
            "import subprocess, sys\n"
            "result = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'test_suite.py'], "
            "text=True, capture_output=True, timeout=30)\n"
            "assert result.returncode == 0, (result.stdout + result.stderr)[-5000:]\n"
        ),
        baseline="__acceptance__",
        shadow=(
            "from cache import LRUCache\n"
            "events = []\n"
            "c = LRUCache(1)\n"
            "c.register_eviction_hook(lambda key, value: events.append((key, value)))\n"
            "c.put('a', 1)\n"
            "c.put('b', 2)\n"
            "assert events == [('a', 1)], events\n"
            "assert c.get('a') == -1\n"
            "c2 = LRUCache(2, ttl_seconds=0.0)\n"
            "c2.put('expired', 7)\n"
            "assert c2.get('expired') == -1\n"
            "assert 'expired' not in c2.storage\n"
            "assert 'expired' not in c2.timestamps\n"
        ),
        budget=12,
        max_success_iterations=8,
    ),
    "recovery": Task(
        name="recovery",
        prompt=(
            "Make the repository's test suite pass. First inspect the project and run the tests. "
            "One test intentionally reports a misleading missing-module path; recover by locating "
            "the real implementation. Fix the smallest appropriate file, run the tests again, and finish."
        ),
        setup={
            "pkg/__init__.py": "",
            "pkg/calculator.py": "def discount(price, percent):\n    return price * (1 - percent / 100)\n",
            "test_calculator.py": (
                "from pkg.calculator import discount\n\n"
                "def test_discount():\n"
                "    assert discount(200, 15) == 170\n"
                "    assert discount(80, 0) == 80\n"
            ),
        },
        grade=(
            "from pkg.calculator import discount\n"
            "assert discount(200, 15) == 170\n"
            "assert discount(80, 0) == 80\n"
        ),
        budget=18,
    ),
}


def run_one(task: Task, condition: str, iterations: int, action_critic: bool,
            action_gate: bool, chat_timeout: float, model: str,
            backend: str, base_url: str, action_first: bool,
            run_timeout: float, keep_workspace: bool = False,
            editor: str = "patch_file") -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"agentic-{task.name}-{condition}-"))
    _write_setup(work, task.setup)
    evidence_ok, validation_evidence = _baseline_contract_valid(task, work)
    if not evidence_ok:
        return {
            "task": task.name,
            "condition": condition,
            "passed": False,
            "model": model,
            "backend": backend,
            "detail": "task precondition evidence was invalid; model run was not started",
            "timed_out": False,
            "returncode": None,
            "elapsed_seconds": 0.0,
            "metrics": {},
            "scorecard": {"artifact_passed": False, "run_completed": False},
            "validation_evidence": validation_evidence,
        }
    protected_paths = _protected_task_paths(task)
    protected_snapshot = _snapshot_protected_paths(work, protected_paths)
    # Unbuffered child output is required for true event-level monitoring;
    # otherwise Python holds agent logs until the entire run exits.
    cmd = [sys.executable, "-u", str(ROOT / "agent.py"), "--project", str(work),
           "--iteration-budget", str(iterations), "--chat-timeout", str(chat_timeout),
           "--model", model, "--backend", backend, "--base-url", base_url,
           "--editor", editor]
    if action_first:
        cmd.append("--action-first")
    if condition == "novelty":
        cmd.extend(["--novelty-context"])
        if action_critic:
            cmd.append("--novelty-action-critic")
        if action_gate:
            cmd.append("--novelty-action-gate")
    cmd.append(task.prompt)
    started = time.monotonic()
    RUNS.mkdir(parents=True, exist_ok=True)
    monitor_path = RUNS / f"monitor-{task.name}-{condition}-{time.time_ns()}.jsonl"
    proc = subprocess.Popen(
        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    stdout, timed_out, returncode = _stream_agent(proc, started, run_timeout, monitor_path)
    if timed_out:
        stdout += f"\nBENCHMARK WATCHDOG: exceeded {run_timeout:.1f}s and was terminated.\n"
    elapsed = time.monotonic() - started
    integrity_ok, integrity_detail = _check_protected_paths(work, protected_snapshot)
    if integrity_ok:
        grade_result = _grade(task, work)
    else:
        grade_result = GradeResult(
            status="UNSAFE_WORKSPACE_CHANGE",
            passed=False,
            detail=integrity_detail,
            returncode=None,
            elapsed_seconds=0.0,
            checker_sha256="",
            phase="integrity",
        )
    artifact_passed, detail = grade_result.passed, grade_result.detail
    regression_ok = _post_task_evidence(task, work, validation_evidence)
    artifact_passed = artifact_passed and regression_ok
    if not regression_ok:
        detail = "pass-to-pass regression evidence failed"
    metrics = _metrics(stdout or "")
    initial_metrics = metrics
    verifier_repair = None
    # A clean actor handoff is not enough when the independent verifier rejects
    # the artifact. Give the same workspace one bounded repair pass with the
    # verifier's exact evidence. This is a generic external-validation loop:
    # the harness does not interpret the task or synthesize a WebSocket-specific
    # fix, and it never runs more than one feedback pass per benchmark attempt.
    if not artifact_passed and integrity_ok and _run_completed(timed_out, returncode):
        print("🔁 Independent verifier rejected the artifact; starting one bounded repair pass.")
        repair_monitor_path = RUNS / f"monitor-{task.name}-{condition}-{time.time_ns()}-repair.jsonl"
        repair_cmd = [
            sys.executable, "-u", str(ROOT / "agent.py"), "--project", str(work),
            "--iteration-budget", str(min(iterations, 10)), "--chat-timeout", str(chat_timeout),
            "--model", model, "--backend", backend, "--base-url", base_url,
            "--editor", editor,
        ]
        if action_first:
            repair_cmd.append("--action-first")
        if condition == "novelty":
            repair_cmd.extend(["--novelty-context"])
            if action_critic:
                repair_cmd.append("--novelty-action-critic")
            if action_gate:
                repair_cmd.append("--novelty-action-gate")
        repair_cmd.append(_verifier_repair_prompt(task, detail))
        repair_started = time.monotonic()
        repair_proc = subprocess.Popen(
            repair_cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        repair_stdout, repair_timed_out, repair_returncode = _stream_agent(
            repair_proc, repair_started, run_timeout, repair_monitor_path
        )
        stdout += "\n" + repair_stdout
        timed_out = timed_out or repair_timed_out
        returncode = repair_returncode
        integrity_ok, integrity_detail = _check_protected_paths(work, protected_snapshot)
        if integrity_ok:
            grade_result = _grade(task, work)
        else:
            grade_result = GradeResult(
                status="UNSAFE_WORKSPACE_CHANGE",
                passed=False,
                detail=integrity_detail,
                returncode=None,
                elapsed_seconds=0.0,
                checker_sha256="",
                phase="integrity",
            )
        artifact_passed, detail = grade_result.passed, grade_result.detail
        regression_ok = _post_task_evidence(task, work, validation_evidence)
        artifact_passed = artifact_passed and regression_ok
        if not regression_ok:
            detail = "pass-to-pass regression evidence failed"
        verifier_repair = {
            "monitor_log": str(repair_monitor_path),
            "timed_out": repair_timed_out,
            "returncode": repair_returncode,
            "elapsed_seconds": round(time.monotonic() - repair_started, 1),
            "artifact_passed": artifact_passed,
            "done_signal": _metrics(repair_stdout or "")["done_signal"],
        }
        metrics = _metrics(stdout or "")

    # Hidden/shadow acceptance is the final grader layer. It must not be fed
    # back into the actor or used to start a visible verifier-repair pass; it
    # only controls whether the already-inspected artifact can be reported as
    # correct. This keeps the shadow check outside the agent's information set.
    visible_artifact_passed = artifact_passed
    shadow_result = _run_shadow_evidence(task, work)
    artifact_passed, shadow_detail = _apply_shadow_result(
        visible_artifact_passed, shadow_result
    )
    if shadow_detail and visible_artifact_passed:
        detail = shadow_detail

    # A provider can terminate cleanly after the artifact is already correct,
    # either during the initial actor run or during the bounded verifier-repair
    # pass. Reconcile that narrow handoff only when the independent artifact
    # check passed, the actor process exited cleanly, and the trace has the
    # explicit provider-loss signature. This does not make a timeout, nonzero
    # exit, or ordinary model stall a pass.
    handoff_reconciled = _handoff_reconciled(
        artifact_passed,
        _run_completed(timed_out, returncode),
        (repair_stdout or "") if verifier_repair is not None else (stdout or ""),
    )
    scorecard = {
        "artifact_passed": artifact_passed,
        "finish_called": (
            verifier_repair["done_signal"] if verifier_repair is not None
            else initial_metrics["done_signal"]
        ),
        "run_completed": _run_completed(timed_out, returncode),
        "handoff_reconciled": handoff_reconciled,
        "shadow_passed": (
            shadow_result["status"] == "PASS" if shadow_result is not None else None
        ),
    }
    # A correct partial artifact is useful evidence, but it is not a complete
    # agent run. Require the actor's explicit finish signal so a model that
    # mutates successfully and then stalls in validation is scored honestly.
    passed = _scorecard_passed(
        artifact_passed, scorecard["finish_called"], scorecard["run_completed"],
        handoff_reconciled,
    )
    if not passed and artifact_passed:
        detail = (
            "Artifact passed, but the agent did not complete the required handoff: "
            + json.dumps(scorecard, sort_keys=True)
        )
    if task.max_success_iterations is not None:
        scorecard["iteration_target"] = task.max_success_iterations
        scorecard["iteration_target_met"] = (
            metrics["iterations"] <= task.max_success_iterations
        )
        passed = passed and metrics["done_signal"] and scorecard["iteration_target_met"]
        if not passed and artifact_passed:
            detail = (
                "Artifact passed, but the agent did not meet the workflow scorecard: "
                + json.dumps(scorecard, sort_keys=True)
            )
    record = {
        "task": task.name, "condition": condition, "editor": editor, "passed": passed,
        "model": model,
        "backend": backend,
        "detail": detail, "timed_out": timed_out, "returncode": returncode,
        "elapsed_seconds": round(elapsed, 1), "metrics": metrics,
        "scorecard": scorecard,
        "grader": grade_result.as_dict(),
        "validation_evidence": validation_evidence,
        "protected_test_paths": list(protected_paths),
        "test_integrity": integrity_ok,
        "monitor_log": str(monitor_path),
    }
    if shadow_result is not None:
        record["shadow_grader"] = shadow_result
    if verifier_repair is not None:
        record["verifier_repair"] = verifier_repair
    record["elapsed_seconds"] = round(time.monotonic() - started, 1)
    RUNS.mkdir(parents=True, exist_ok=True)
    with (RUNS / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    if keep_workspace:
        record["workspace"] = str(work)
        print(f"📁 Preserved workspace: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(record, indent=2))
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=[*TASKS, "all"], default="all")
    parser.add_argument("--condition", choices=["baseline", "novelty", "both"], default="both")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--chat-timeout", type=float, default=30)
    parser.add_argument("--run-timeout", type=float, default=600,
                        help="Maximum seconds for one agent run before its process group is terminated.")
    parser.add_argument("--profile", choices=["full", "smoke"], default="full",
                        help="smoke caps the run at 8 iterations, 45s/chat, and 300s total.")
    parser.add_argument("--model", default="qwen3.6:35b-mlx",
                        help="Ollama actor model used for the run.")
    parser.add_argument("--backend", choices=["ollama", "llama-cpp"], default="ollama",
                        help="Actor serving backend used for the run.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1",
                        help="Actor OpenAI-compatible base URL.")
    parser.add_argument("--action-critic", action="store_true")
    parser.add_argument("--action-gate", action="store_true")
    parser.add_argument("--action-first", action="store_true",
                        help="Use the model-neutral initial action contract.")
    parser.add_argument("--editor", choices=["patch_file"], default="patch_file",
                        help="Model-facing edit primitive; patch_file is the only supported editor.")
    parser.add_argument("--keep-workspace", action="store_true",
                        help="Preserve the generated task workspace for inspection.")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip the cheap deterministic guard suite (debugging only).")
    args = parser.parse_args()
    if not args.skip_preflight:
        print("🧪 Running deterministic preflight before real-model benchmark...")
        preflight_ok, preflight_detail = _run_preflight()
        if not preflight_ok:
            print("❌ PREFLIGHT FAILED — expensive model run not started.")
            print(preflight_detail)
            return 2
        print("✅ Preflight passed; starting real-model benchmark.")
    selected = list(TASKS.values()) if args.task == "all" else [TASKS[args.task]]
    conditions = ["baseline", "novelty"] if args.condition == "both" else [args.condition]
    iterations, chat_timeout, run_timeout = _profile_limits(
        args.profile, args.iterations, args.chat_timeout, args.run_timeout
    )
    records = [run_one(task, condition, iterations, args.action_critic,
                       args.action_gate, chat_timeout, args.model,
                       args.backend, args.base_url, args.action_first,
                       run_timeout, args.keep_workspace, args.editor)
               for task in selected for condition in conditions]
    passed = sum(r["passed"] for r in records)
    print(json.dumps({"summary": {"passed": passed, "total": len(records),
                                   "success_rate": passed / len(records) if records else 0}}, indent=2))
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
