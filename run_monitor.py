#!/usr/bin/env python3
"""Human-friendly live monitor for NoveltyEngine benchmark runs.

Examples:
    python3 run_monitor.py --task lru_cache --condition novelty --follow
    python3 run_monitor.py --file state/benchmark/agentic/monitor-lru_cache-novelty-*.jsonl

The monitor is read-only. It consumes the benchmark's JSONL event stream and
the append-only results file; it never changes the agent or its workspace.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT / "state" / "benchmark" / "agentic" / "results.jsonl"
ITERATION_RE = re.compile(r"Iteration\s+(\d+)(?:/(\d+))?")


@dataclass
class MonitorState:
    event_count: int = 0
    tool_calls: int = 0
    iterations: int = 0
    iteration_budget: int | None = None
    validations: int = 0
    errors: int = 0
    last_elapsed: float = 0.0
    last_event: str = "waiting for events"
    timing: dict = field(default_factory=dict)
    repair: dict = field(default_factory=dict)
    novelty: dict = field(default_factory=dict)


def _json_after_marker(text: str, marker: str) -> dict:
    position = text.find(marker)
    if position < 0:
        return {}
    start = text.find("{", position + len(marker))
    if start < 0:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def consume_event(event: dict, state: MonitorState) -> str:
    """Update aggregate state and return a concise display line."""
    text = str(event.get("text", "")).strip()
    kind = str(event.get("kind", "event"))
    state.event_count += 1
    state.last_elapsed = float(event.get("elapsed_s", state.last_elapsed) or 0.0)
    state.last_event = text or kind

    if kind == "tool_call":
        state.tool_calls += 1
    if kind == "validation":
        state.validations += 1
    if kind == "error":
        state.errors += 1

    match = ITERATION_RE.search(text)
    if match:
        state.iterations = int(match.group(1))
        if match.group(2):
            state.iteration_budget = int(match.group(2))
    if "[agent timing]" in text:
        state.timing = _json_after_marker(text, "[agent timing]")
    if "[repair metrics]" in text:
        state.repair = _json_after_marker(text, "[repair metrics]")
    if "[novelty metrics]" in text:
        state.novelty = _json_after_marker(text, "[novelty metrics]")

    display = text
    if kind == "tool_call":
        tool = re.search(r"🔧\s+([A-Za-z0-9_]+)", text)
        path = re.search(r"""['"]path['"]:\s*['"]([^'"]+)""", text)
        display = "tool " + (tool.group(1) if tool else "unknown")
        if path:
            display += f"  path={path.group(1)}"
    elif kind == "iteration":
        display = "iteration " + (match.group(1) if match else "?")
    elif kind == "error":
        display = "ERROR " + text
    elif kind == "validation":
        display = "validation " + text
    return display[:500]


def _latest_monitor(task: str, condition: str, monitor_dir: Path) -> Path | None:
    pattern = f"monitor-{task}-{condition}-*.jsonl"
    candidates = list(monitor_dir.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _read_new_events(path: Path, seen: int) -> tuple[list[dict], int]:
    if not path.exists():
        return [], seen
    events = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[seen:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            break
        if isinstance(value, dict):
            events.append(value)
    return events, seen + len(events)


def _latest_result(results_path: Path, task: str | None, condition: str | None) -> dict:
    if not results_path.exists():
        return {}
    latest = {}
    for line in results_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if task and value.get("task") != task:
            continue
        if condition and value.get("condition") != condition:
            continue
        latest = value
    return latest


def _metric(metrics: dict, name: str, default: object = "-") -> object:
    value = metrics.get(name, default)
    return value if value is not None else default


def render_dashboard(
    *,
    path: Path | None,
    state: MonitorState,
    result: dict,
    task: str | None,
    condition: str | None,
) -> str:
    metrics = result.get("metrics") or {}
    novelty = state.novelty or {}
    repair = state.repair or {}
    timing = state.timing or {}
    title = f"{task or result.get('task') or '?'} / {condition or result.get('condition') or '?'}"
    grader_status = result.get("grader", {}).get("status", "RUNNING")
    passed = result.get("passed")
    if passed is True:
        status = "PASS"
    elif passed is False:
        status = "FAIL"
    else:
        status = grader_status
    lines = [
        "",
        "╔════════════════ NoveltyEngine live run monitor ════════════════╗",
        f"║ task: {title:<56} ║",
        f"║ status: {status:<55} ║",
        f"║ monitor: {str(path or 'waiting')[-55:]:<55} ║",
        "╠════════════════════ progress ══════════════════════════════════╣",
        f"║ elapsed: {state.last_elapsed:8.1f}s   iteration: {state.iterations}/{state.iteration_budget or '?':<8} "
        f"events: {state.event_count:<7} ║",
        f"║ tools: {state.tool_calls:<11} validations: {state.validations:<8} errors: {state.errors:<12} ║",
        f"║ mutations: {_metric(novelty, 'mutations', _metric(metrics, 'mutations')):<8} "
        f"first mutation: {_metric(timing, 'first_mutation_s'):<12} "
        f"first validation: {_metric(timing, 'first_validation_s'):<11} ║",
        f"║ worker calls: {_metric(novelty, 'worker_calls'):<7} failures: {_metric(novelty, 'worker_failures'):<8} "
        f"stale: {_metric(novelty, 'stale_judgments'):<8} busy drops: {_metric(novelty, 'worker_busy_drops'):<8} ║",
        f"║ advice issued: {_metric(novelty, 'advice_issued'):<5} followed: {_metric(novelty, 'advice_followed'):<7} "
        f"successful: {_metric(novelty, 'advice_successful'):<7} regressions: {_metric(novelty, 'advice_regression_signals'):<6} ║",
        f"║ lifecycle: {str(repair.get('lifecycle', {}).get('state', '-')):<16} "
        f"repair turns: {_metric(repair, 'repair_turns'):<6} transaction: "
        f"{str(repair.get('transaction', {}).get('active', '-')):<14} ║",
        "╠════════════════════ latest event ══════════════════════════════╣",
        f"║ {state.last_event[:62]:<62} ║",
        "╚═══════════════════════════════════════════════════════════════╝",
    ]
    if result:
        detail = str(result.get("detail", "")).replace("\n", " ")
        if detail:
            lines.append("result detail: " + detail[:300])
        if result.get("verifier_repair"):
            lines.append("verifier repair: used (reported separately from direct actor success)")
    return "\n".join(lines)


def monitor(args: argparse.Namespace) -> int:
    monitor_dir = Path(args.monitor_dir).resolve()
    results_path = Path(args.results).resolve()
    path = Path(args.file).resolve() if args.file else None
    state = MonitorState()
    announced: set[str] = set()
    last_dashboard_signature = None
    last_dashboard_at = 0.0
    while True:
        if path is None and args.task and args.condition:
            path = _latest_monitor(args.task, args.condition, monitor_dir)
        events, new_count = _read_new_events(path, state.event_count) if path else ([], 0)
        for event in events:
            display = consume_event(event, state)
            print(f"[{state.last_elapsed:8.1f}s] {display}", flush=True)
        if events:
            announced.add(str(path))
        result = _latest_result(results_path, args.task, args.condition)
        signature = (
            state.event_count,
            state.last_event,
            result.get("passed"),
            result.get("grader", {}).get("status"),
        )
        now = time.monotonic()
        if events or signature != last_dashboard_signature or now - last_dashboard_at >= args.heartbeat:
            print(render_dashboard(path=path, state=state, result=result,
                                   task=args.task, condition=args.condition),
                  flush=True)
            last_dashboard_signature = signature
            last_dashboard_at = now
        if result and not args.follow:
            return 0 if result.get("passed") else 1
        if not args.follow:
            return 0
        if path is not None and path.exists() and result:
            return 0 if result.get("passed") else 1
        time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Watch a NoveltyEngine JSONL run and explain its vital metadata."
    )
    parser.add_argument("--file", help="Exact monitor JSONL path.")
    parser.add_argument("--task", help="Task name; auto-selects the newest monitor file.")
    parser.add_argument("--condition", choices=["baseline", "novelty"],
                        help="Required with --task.")
    parser.add_argument("--monitor-dir", default=str(ROOT / "state/benchmark/agentic"),
                        help="Directory containing monitor JSONL files.")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS),
                        help="Append-only benchmark results JSONL.")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Seconds between dashboard refreshes.")
    parser.add_argument("--heartbeat", type=float, default=15.0,
                        help="Repeat the dashboard after this many quiet seconds.")
    parser.add_argument("--follow", action="store_true",
                        help="Keep watching until a result record appears.")
    args = parser.parse_args()
    if not args.file and not args.task:
        parser.error("one of --file or --task is required")
    if args.task and not args.condition:
        parser.error("--condition is required with --task")
    try:
        return monitor(args)
    except KeyboardInterrupt:
        print("\nMonitor stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
