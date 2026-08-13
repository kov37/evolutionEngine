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
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "state" / "benchmark" / "agentic"


@dataclass(frozen=True)
class Task:
    name: str
    prompt: str
    setup: dict[str, str]
    grade: str
    budget: int = 20


def _write_setup(root: Path, setup: dict[str, str]) -> None:
    for relative, content in setup.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _grade(task: Task, root: Path) -> tuple[bool, str]:
    """Run a task-specific independent grader in a subprocess."""
    grader = root / ".agentic_grader.py"
    grader.write_text(task.grade, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(grader)], cwd=root, text=True,
        capture_output=True, timeout=45,
    )
    detail = (proc.stdout + proc.stderr).strip()[-3000:]
    return proc.returncode == 0, detail


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
    return {
        "iterations": iterations,
        "tool_calls": tool_calls,
        "failure_lines": failures,
        "done_signal": "✅ DONE" in output,
        "novelty": novelty,
    }


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
            "    assert '<html' in html.lower() and 'todo' in html.lower()\n"
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
        budget=24,
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
        budget=22,
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
            backend: str, base_url: str, action_first: bool) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"agentic-{task.name}-{condition}-"))
    _write_setup(work, task.setup)
    cmd = [sys.executable, str(ROOT / "agent.py"), "--project", str(work),
           "--iteration-budget", str(iterations), "--chat-timeout", str(chat_timeout),
           "--model", model, "--backend", backend, "--base-url", base_url]
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
    try:
        # Deliberately leave the aggregate benchmark uncapped while we study
        # llama.cpp mechanics. The agent's iteration budget and per-turn
        # timeout remain visible experimental controls; this layer must not
        # silently terminate a slow but informative run.
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True,
                              timeout=None)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(cmd, 124, exc.stdout or "", exc.stderr or "")
        timed_out = True
    elapsed = time.monotonic() - started
    passed, detail = _grade(task, work)
    record = {
        "task": task.name, "condition": condition, "passed": passed,
        "model": model,
        "backend": backend,
        "detail": detail, "timed_out": timed_out, "returncode": proc.returncode,
        "elapsed_seconds": round(elapsed, 1), "metrics": _metrics(proc.stdout or ""),
    }
    RUNS.mkdir(parents=True, exist_ok=True)
    with (RUNS / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(record, indent=2))
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=[*TASKS, "all"], default="all")
    parser.add_argument("--condition", choices=["baseline", "novelty", "both"], default="both")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--chat-timeout", type=float, default=30)
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
    args = parser.parse_args()
    selected = list(TASKS.values()) if args.task == "all" else [TASKS[args.task]]
    conditions = ["baseline", "novelty"] if args.condition == "both" else [args.condition]
    records = [run_one(task, condition, args.iterations, args.action_critic,
                       args.action_gate, args.chat_timeout, args.model,
                       args.backend, args.base_url, args.action_first)
               for task in selected for condition in conditions]
    passed = sum(r["passed"] for r in records)
    print(json.dumps({"summary": {"passed": passed, "total": len(records),
                                   "success_rate": passed / len(records) if records else 0}}, indent=2))
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
