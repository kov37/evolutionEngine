"""Minimal two-model context management for noveltyEngine.

The coding model remains on the critical path.  The 4B worker consumes tool
events asynchronously and returns a small, bounded JSON judgment.  If Ollama
is unavailable or returns malformed output, deterministic local bookkeeping
continues and the coding loop is never blocked by the worker.
"""

from __future__ import annotations

import json
import multiprocessing
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable


DEFAULT_WORKER_MODEL = "qwen3.5:4b"
DEFAULT_WORKER_NUM_CTX = 4096
DEFAULT_WORKER_INTERVAL = 8
MAX_EVENT_CHARS = 6000
MAX_STATE_CHARS = 5000
MAX_WORKER_OUTPUT_CHARS = 1200


@dataclass
class ContextEvent:
    iteration: int
    tool: str
    arguments: dict[str, Any]
    result: str
    result_fingerprint: str
    mutation: bool = False
    validation: bool = False


@dataclass
class WorkerJudgment:
    phase: str = "orient"
    new_facts: list[str] = field(default_factory=list)
    relevant_facts: list[str] = field(default_factory=list)
    duplicate_action: bool = False
    stagnating: bool = False
    recommended_action: str = "inspect"
    confidence: float = 0.0
    source: str = "fallback"
    latency_ms: float = 0.0

    def render(self) -> str:
        payload = {
            "phase": self.phase,
            "new_facts": self.new_facts[-5:],
            "relevant_facts": self.relevant_facts[-5:],
            "duplicate_action": self.duplicate_action,
            "stagnating": self.stagnating,
            "recommended_action": self.recommended_action,
            "confidence": round(self.confidence, 2),
            "source": self.source,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _fingerprint(tool: str, arguments: dict[str, Any], result: str) -> str:
    import hashlib

    normalized = json.dumps(
        {"tool": tool, "arguments": arguments, "result": result[:MAX_EVENT_CHARS]},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _local_judgment(events: list[ContextEvent], event: ContextEvent) -> WorkerJudgment:
    recent = events[-8:]
    duplicate = sum(e.result_fingerprint == event.result_fingerprint for e in recent[:-1]) > 0
    no_mutation = not any(e.mutation for e in recent)
    repeated = sum(e.result_fingerprint == event.result_fingerprint for e in recent) >= 2
    if event.mutation:
        phase = "verify"
        action = "validate"
    elif event.validation:
        phase = "repair" if event.result.startswith(("ERROR", "REJECTED")) else "verify"
        action = "patch_file" if phase == "repair" else "finish_or_repair"
    elif no_mutation and len(recent) >= 5:
        phase, action = "mutate", "patch_file"
    else:
        phase, action = "localize", "inspect"
    return WorkerJudgment(
        phase=phase,
        new_facts=[f"{event.tool}: {event.result[:240]}"],
        duplicate_action=duplicate,
        stagnating=repeated or (no_mutation and len(recent) >= 8),
        recommended_action=action,
        confidence=0.55 if not duplicate else 0.8,
    )


def _prompt(state: str, event: ContextEvent) -> str:
    return f"""You are a compact coding-agent context worker. Return ONLY one JSON object.
Allowed phase values: orient, localize, hypothesize, mutate, verify, repair.
Allowed recommended_action values: inspect, patch_file, validate, finish_or_repair.
Use only facts present below. Keep every list to at most 3 short strings.
Schema: {{"phase":str,"new_facts":[str],"relevant_facts":[str],"duplicate_action":bool,"stagnating":bool,"recommended_action":str,"confidence":number}}

Current state:
{state[:MAX_STATE_CHARS]}

Latest tool event:
tool={event.tool}
arguments={json.dumps(event.arguments, default=str)[:1200]}
result={event.result[:MAX_EVENT_CHARS]}
"""


def _ollama_process(prompt: str, model: str, num_ctx: int, conn) -> None:
    """Run one real worker call in a killable child process."""
    try:
        from ollama import chat
        response = chat(model=model, messages=[{"role": "user", "content": prompt}],
                        think=False, options={"num_ctx": num_ctx})
        raw = getattr(getattr(response, "message", None), "content", "") or ""
        conn.send({"ok": True, "raw": raw[:MAX_WORKER_OUTPUT_CHARS]})
    except Exception as exc:
        conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        conn.close()


def _parse_judgment(raw: str, fallback: WorkerJudgment) -> WorkerJudgment:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return fallback
    try:
        data = json.loads(match.group(0))
        allowed_phase = {"orient", "localize", "hypothesize", "mutate", "verify", "repair"}
        allowed_action = {"inspect", "patch_file", "validate", "finish_or_repair"}
        phase = data.get("phase") if data.get("phase") in allowed_phase else fallback.phase
        action = data.get("recommended_action")
        action = action if action in allowed_action else fallback.recommended_action
        return WorkerJudgment(
            phase=phase,
            new_facts=[str(x)[:300] for x in data.get("new_facts", [])[:3]],
            relevant_facts=[str(x)[:300] for x in data.get("relevant_facts", [])[:3]],
            duplicate_action=bool(data.get("duplicate_action", fallback.duplicate_action)),
            stagnating=bool(data.get("stagnating", fallback.stagnating)),
            recommended_action=action,
            confidence=max(0.0, min(1.0, float(data.get("confidence", fallback.confidence)))),
            source="4b",
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


class NoveltyContext:
    """Bounded event ledger plus one in-flight asynchronous 4B judgment."""

    def __init__(
        self,
        worker_model: str = DEFAULT_WORKER_MODEL,
        worker_num_ctx: int = DEFAULT_WORKER_NUM_CTX,
        worker_interval: int = DEFAULT_WORKER_INTERVAL,
        chat_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.worker_model = worker_model
        self.worker_num_ctx = worker_num_ctx
        self.worker_interval = max(1, worker_interval)
        self._chat_fn = chat_fn
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="novelty-4b")
        self._future: Future[WorkerJudgment] | None = None
        self._process = None
        self._process_conn = None
        self._process_mode = chat_fn is None
        # state_text() is called while observe() is recording an event.  A
        # re-entrant lock keeps that snapshot operation atomic without making
        # the event path self-deadlock.
        self._lock = threading.RLock()
        self.events: list[ContextEvent] = []
        self.judgments: list[WorkerJudgment] = []
        self.last_judgment = WorkerJudgment()
        self.worker_calls = 0
        self.worker_failures = 0
        self.worker_busy_drops = 0
        self.started_at = monotonic()

    def state_text(self) -> str:
        with self._lock:
            recent = self.events[-8:]
            return json.dumps({
                "events": [
                    {"iteration": e.iteration, "tool": e.tool, "mutation": e.mutation,
                     "validation": e.validation, "result": e.result[:300]}
                    for e in recent
                ],
                "last_judgment": json.loads(self.last_judgment.render()),
            }, separators=(",", ":"))

    def observe(self, iteration: int, tool: str, arguments: dict[str, Any], result: str,
                mutation: bool = False, validation: bool = False) -> None:
        event = ContextEvent(iteration, tool, arguments, str(result),
                             _fingerprint(tool, arguments, str(result)), mutation, validation)
        with self._lock:
            self.events.append(event)
            self.events = self.events[-100:]
            # Harvest a completed result before replacing the future.  Without
            # this, a fast sequence of events could overwrite a completed
            # future before collect() ever saw it.
            completed = ((self._future is not None and self._future.done()) or
                         (self._process is not None and not self._process.is_alive()))
        if completed:
            self.collect(wait=False)
        with self._lock:
            recent = self.events[-4:]
            repeated_error = (
                result.startswith(("ERROR", "REJECTED"))
                and sum(e.result.startswith(("ERROR", "REJECTED")) for e in recent) >= 2
            )
            repeated_action = sum(e.result_fingerprint == event.result_fingerprint for e in recent) >= 2
            validation_failure = validation and result.startswith(("ERROR", "REJECTED"))
            # The worker is reserved for evidence that local deterministic
            # signals cannot resolve cheaply. A first error or routine edit
            # should not start competing Ollama inference beside the 35B call.
            should_process = (
                repeated_error or repeated_action or validation_failure
                or len(self.events) % self.worker_interval == 0
            )
            if not should_process:
                return
            process_busy = self._process is not None and self._process.is_alive()
            if ((self._future is not None and not self._future.done()) or process_busy):
                self.worker_busy_drops += 1
                return
            fallback = _local_judgment(self.events, event)
            if self._process_mode:
                parent_conn, child_conn = multiprocessing.Pipe(False)
                self._process_conn = parent_conn
                process_factory = multiprocessing.get_context("fork").Process
                self._process = process_factory(
                    target=_ollama_process,
                    args=(_prompt(self.state_text(), event), self.worker_model, self.worker_num_ctx, child_conn),
                    daemon=True,
                )
                self.worker_calls += 1
                self._process.start()
                child_conn.close()
            else:
                self._future = self._executor.submit(self._judge, self.state_text(), event, fallback)

    def _judge(self, state: str, event: ContextEvent, fallback: WorkerJudgment) -> WorkerJudgment:
        started = monotonic()
        try:
            if self._chat_fn is None:
                from ollama import chat
                chat_fn = chat
            else:
                chat_fn = self._chat_fn
            self.worker_calls += 1
            response = chat_fn(model=self.worker_model, messages=[{"role": "user", "content": _prompt(state, event)}],
                               think=False, options={"num_ctx": self.worker_num_ctx})
            raw = getattr(getattr(response, "message", None), "content", "")
            judgment = _parse_judgment(raw[:MAX_WORKER_OUTPUT_CHARS], fallback)
            judgment.latency_ms = (monotonic() - started) * 1000
            return judgment
        except Exception:
            self.worker_failures += 1
            fallback.latency_ms = (monotonic() - started) * 1000
            return fallback

    def collect(self, wait: bool = False) -> WorkerJudgment:
        if self._process_mode and self._process is not None:
            if wait:
                self._process.join()
            if self._process.is_alive() or self._process_conn is None or not self._process_conn.poll():
                return self.last_judgment
            try:
                payload = self._process_conn.recv()
                event = self.events[-1]
                fallback = _local_judgment(self.events, event)
                if payload.get("ok"):
                    judgment = _parse_judgment(payload.get("raw", ""), fallback)
                else:
                    self.worker_failures += 1
                    judgment = fallback
                with self._lock:
                    self.last_judgment = judgment
                    self.judgments.append(judgment)
            except (EOFError, OSError, TypeError):
                self.worker_failures += 1
                judgment = self.last_judgment
            finally:
                self._process_conn.close()
                self._process_conn = None
                self._process = None
            return judgment
        future = self._future
        if future is None:
            return self.last_judgment
        if not wait and not future.done():
            return self.last_judgment
        try:
            judgment = future.result()
        except Exception:
            self.worker_failures += 1
            if self._future is future:
                self._future = None
            return self.last_judgment
        with self._lock:
            self.last_judgment = judgment
            self.judgments.append(judgment)
            if self._future is future:
                self._future = None
        return judgment

    def render_for_model(self) -> str:
        judgment = self.collect(wait=False)
        with self._lock:
            return "## Context manager state\n" + judgment.render() + \
                   f"\nEvents recorded: {len(self.events)}; worker calls: {self.worker_calls}; " \
                   f"busy drops: {self.worker_busy_drops}."

    def metrics(self) -> dict[str, Any]:
        self.collect(wait=False)
        with self._lock:
            mutations = sum(e.mutation for e in self.events)
            validations = sum(e.validation for e in self.events)
            duplicates = sum(j.duplicate_action for j in self.judgments)
            return {"events": len(self.events), "mutations": mutations, "validations": validations,
                    "worker_calls": self.worker_calls, "worker_failures": self.worker_failures,
                    "worker_busy_drops": self.worker_busy_drops, "judgments": len(self.judgments),
                    "duplicate_judgments": duplicates, "elapsed_s": monotonic() - self.started_at}

    def close(self) -> None:
        # A real 4B call is advisory. Never make task completion wait for it.
        self.collect(wait=False)
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1)
        if self._process_conn is not None:
            self._process_conn.close()
            self._process_conn = None
        self._executor.shutdown(wait=True)
