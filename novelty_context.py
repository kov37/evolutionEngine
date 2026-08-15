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


DEFAULT_WORKER_MODEL = "qwen3.5:4b"  # default only; behavior never branches on model name
DEFAULT_WORKER_NUM_CTX = 4096
DEFAULT_WORKER_INTERVAL = 8
MAX_EVENT_CHARS = 6000
MAX_STATE_CHARS = 5000
MAX_WORKER_OUTPUT_CHARS = 1200
MAX_REPAIR_PACKET_CHARS = 2200
DEFAULT_TRIAGE_TIMEOUT = 5.0


@dataclass
class ContextEvent:
    event_id: int
    iteration: int
    tool: str
    arguments: dict[str, Any]
    result: str
    result_fingerprint: str
    mutation: bool = False
    validation: bool = False


@dataclass(frozen=True)
class WorkerConfig:
    """Model/backend-neutral worker settings.

    The context manager depends only on a chat callable returning text. Model
    names, providers, context limits, and deployment choices stay outside the
    scheduling and state logic so the same policy can be tested with any
    Ollama tag or an injected API adapter.
    """

    model: str = DEFAULT_WORKER_MODEL
    num_ctx: int = DEFAULT_WORKER_NUM_CTX
    interval: int = DEFAULT_WORKER_INTERVAL
    max_output_chars: int = MAX_WORKER_OUTPUT_CHARS
    action_after_events: int = 8


@dataclass
class WorkerJudgment:
    event_id: int = 0
    phase: str = "orient"
    new_facts: list[str] = field(default_factory=list)
    relevant_facts: list[str] = field(default_factory=list)
    duplicate_action: bool = False
    stagnating: bool = False
    recommended_action: str = "inspect"
    blocker: str = ""
    target: str = ""
    confidence: float = 0.0
    source: str = "fallback"
    latency_ms: float = 0.0
    diagnosis: str = ""
    failure_class: str = "unknown"
    next_action: str = ""
    preserve_files: list[str] = field(default_factory=list)

    def render(self) -> str:
        payload = {
            "event_id": self.event_id,
            "phase": self.phase,
            "new_facts": self.new_facts[-5:],
            "relevant_facts": self.relevant_facts[-5:],
            "duplicate_action": self.duplicate_action,
            "stagnating": self.stagnating,
            "recommended_action": self.recommended_action,
            "blocker": self.blocker[:240],
            "target": self.target[:240],
            "confidence": round(self.confidence, 2),
            "source": self.source,
            "diagnosis": self.diagnosis[:360],
            "failure_class": self.failure_class,
            "next_action": self.next_action[:240],
            "preserve_files": self.preserve_files[-5:],
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


def _call_key(tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """Stable identity for a tool call, independent of argument key order."""
    return tool, json.dumps(arguments or {}, sort_keys=True, default=str, separators=(",", ":"))


def _local_judgment(events: list[ContextEvent], event: ContextEvent, window: int = 8) -> WorkerJudgment:
    recent = events[-max(1, window):]
    duplicate = sum(e.result_fingerprint == event.result_fingerprint for e in recent[:-1]) > 0
    no_mutation = not any(e.mutation for e in recent)
    repeated = sum(e.result_fingerprint == event.result_fingerprint for e in recent) >= 2
    if event.mutation:
        phase = "verify"
        action = "validate"
    elif event.validation:
        phase = "repair" if event.result.startswith(("ERROR", "REJECTED")) else "verify"
        action = "patch_file" if phase == "repair" else "finish_or_repair"
    elif no_mutation and len(recent) >= window:
        phase, action = "mutate", "patch_file"
    else:
        phase, action = "localize", "inspect"
    return WorkerJudgment(
        event_id=event.event_id,
        phase=phase,
        new_facts=[f"{event.tool}: {event.result[:240]}"],
        duplicate_action=duplicate,
        stagnating=repeated or (no_mutation and len(recent) >= 8),
        recommended_action=action,
        target=str(event.arguments.get("path") or event.arguments.get("file") or "")[:240],
        confidence=0.55 if not duplicate else 0.8,
    )


def _prompt(state: str, event: ContextEvent) -> str:
    return f"""You are a compact coding-agent context worker. Return ONLY one JSON object.
Allowed phase values: orient, localize, hypothesize, mutate, verify, repair.
Allowed recommended_action values: inspect, patch_file, validate, finish_or_repair.
Allowed failure_class values: setup, behavior, progress, completion, unknown.
Allowed next_action values: inspect, patch_file, validate, run_command, finish_or_repair.
Use only facts present below. Keep every list to at most 3 short strings.
Schema: {{"phase":str,"new_facts":[str],"relevant_facts":[str],"duplicate_action":bool,"stagnating":bool,"recommended_action":str,"blocker":str,"target":str,"confidence":number,"diagnosis":str,"failure_class":str,"next_action":str,"preserve_files":[str]}}

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
        allowed_failure_class = {"setup", "behavior", "progress", "completion", "unknown"}
        allowed_next_action = {"inspect", "patch_file", "validate", "run_command", "finish_or_repair"}
        phase = data.get("phase") if data.get("phase") in allowed_phase else fallback.phase
        action = data.get("recommended_action")
        action = action if action in allowed_action else fallback.recommended_action
        failure_class = data.get("failure_class")
        failure_class = failure_class if failure_class in allowed_failure_class else fallback.failure_class
        next_action = data.get("next_action")
        next_action = next_action if next_action in allowed_next_action else fallback.next_action
        return WorkerJudgment(
            phase=phase,
            new_facts=[str(x)[:300] for x in data.get("new_facts", [])[:3]],
            relevant_facts=[str(x)[:300] for x in data.get("relevant_facts", [])[:3]],
            duplicate_action=bool(data.get("duplicate_action", fallback.duplicate_action)),
            stagnating=bool(data.get("stagnating", fallback.stagnating)),
            recommended_action=action,
            blocker=str(data.get("blocker", fallback.blocker))[:240],
            target=str(data.get("target", fallback.target))[:240],
            confidence=max(0.0, min(1.0, float(data.get("confidence", fallback.confidence)))),
            source="4b",
            diagnosis=str(data.get("diagnosis", fallback.diagnosis))[:360],
            failure_class=failure_class,
            next_action=next_action,
            preserve_files=[str(x)[:240] for x in data.get("preserve_files", fallback.preserve_files)[:5]],
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
        action_after_events: int = 8,
        config: WorkerConfig | None = None,
        chat_fn: Callable[..., Any] | None = None,
    ) -> None:
        if config is not None:
            worker_model, worker_num_ctx, worker_interval = config.model, config.num_ctx, config.interval
            action_after_events = config.action_after_events
        self.worker_model = worker_model
        self.worker_num_ctx = max(512, worker_num_ctx)
        self.worker_interval = max(1, worker_interval)
        self.action_after_events = max(1, action_after_events)
        self._chat_fn = chat_fn
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="novelty-4b")
        self._future: Future[WorkerJudgment] | None = None
        self._process = None
        self._process_conn = None
        self._process_mode = chat_fn is None
        self._active_event: ContextEvent | None = None
        self._pending_event: ContextEvent | None = None
        self._pending_fallback: WorkerJudgment | None = None
        self._next_event_id = 0
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
        self.coalesced_events = 0
        self.stale_judgments = 0
        self.started_at = monotonic()
        self.no_action_turns = 0
        self._gate_judgment: WorkerJudgment | None = None

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

    @staticmethod
    def _checkpoint_fallback(event: ContextEvent, packet: str) -> WorkerJudgment:
        """Classify a repair packet without a model when timing is critical."""
        lower = packet.lower()
        setup = any(marker in lower for marker in (
            "no tests", "no test evidence", "pytest", "module not found",
            "dependency", "could not start", "permission denied",
        ))
        failure_class = "setup" if setup else "behavior"
        action = "run_command" if setup else "patch_file"
        return WorkerJudgment(
            event_id=event.event_id,
            phase="repair",
            recommended_action="validate" if setup else "patch_file",
            next_action=action,
            failure_class=failure_class,
            diagnosis=("Validation setup is invalid; preserve the implementation."
                       if setup else "The latest executable evidence indicates a product behavior failure."),
            blocker=packet[:240],
            source="fallback",
            confidence=0.9,
            preserve_files=[str(path)[:240] for path in event.arguments.get("protected_paths", [])[:5]],
        )

    def request_repair_checkpoint(
        self, iteration: int, lifecycle_state: str, failure_packet: str,
        legal_actions: list[str] | tuple[str, ...] = (), protected_paths: list[str] | tuple[str, ...] = (),
    ) -> None:
        """Ask the worker to classify one compact failure at a transition point.

        This is deliberately separate from ordinary event sampling.  The
        checkpoint carries the lifecycle state and legal/protected actions so
        the 4B does not have to reconstruct them from a transcript.
        """
        packet = (
            f"lifecycle_state={lifecycle_state}\n"
            f"legal_actions={list(legal_actions)[:10]}\n"
            f"protected_paths={list(protected_paths)[:10]}\n"
            f"failure_packet={str(failure_packet)[:MAX_REPAIR_PACKET_CHARS]}"
        )
        with self._lock:
            self._next_event_id += 1
            event = ContextEvent(
                event_id=self._next_event_id, iteration=iteration,
                tool="repair_checkpoint", arguments={
                    "lifecycle_state": lifecycle_state,
                    "legal_actions": list(legal_actions)[:10],
                    "protected_paths": list(protected_paths)[:10],
                }, result=packet,
                result_fingerprint=_fingerprint("repair_checkpoint", {}, packet),
            )
            self.events.append(event)
            self.events = self.events[-100:]
            fallback = self._checkpoint_fallback(event, packet)
            self.last_judgment = fallback
            self.judgments.append(fallback)
            busy = ((self._future is not None and not self._future.done()) or
                    (self._process is not None and self._process.is_alive()))
            if busy:
                self._pending_event = event
                self._pending_fallback = fallback
                self.coalesced_events += 1
            else:
                self._start_worker_locked(event, fallback)

    def synchronous_triage(
        self, iteration: int, lifecycle_state: str, failure_packet: str,
        legal_actions: list[str] | tuple[str, ...] = (),
        protected_paths: list[str] | tuple[str, ...] = (),
        timeout: float = DEFAULT_TRIAGE_TIMEOUT,
    ) -> WorkerJudgment:
        """Run a bounded triage gate before the next actor prompt.

        If another worker call is already running, return the deterministic
        checkpoint immediately. The 4B can never delay the actor indefinitely.
        """
        with self._lock:
            before = self._next_event_id
        self.request_repair_checkpoint(
            iteration, lifecycle_state, failure_packet, legal_actions, protected_paths
        )
        with self._lock:
            started = self._active_event is not None and self._active_event.event_id > before
            fallback = self.last_judgment
            checkpoint_event = next(
                (event for event in reversed(self.events)
                 if event.tool == "repair_checkpoint" and event.event_id > before),
                None,
            )
            deterministic = (
                self._checkpoint_fallback(checkpoint_event, checkpoint_event.result)
                if checkpoint_event is not None else fallback
            )
        if started:
            judgment = self.collect(wait=True, timeout=max(0.1, timeout))
        else:
            judgment = fallback
        # The model may refine a known setup failure, but it may not turn
        # deterministic setup evidence into a product diagnosis. This avoids
        # hallucinated requirements such as inventing a pytest decorator when
        # the runner itself is unavailable.
        if deterministic.failure_class == "setup" and judgment.failure_class != "setup":
            judgment = deterministic
        with self._lock:
            self._gate_judgment = judgment
        return judgment

    def consume_gate_restrictions(self) -> set[str]:
        """Return one-shot tool restrictions from the synchronous gate.

        The gate may only remove tools. Core risk and protected-path policies
        remain enforced independently by the orchestrator.
        """
        with self._lock:
            judgment = self._gate_judgment
            self._gate_judgment = None
        if judgment is None or judgment.confidence < 0.75:
            return set()
        if judgment.failure_class == "setup":
            return {"write_file", "patch_file"}
        if judgment.failure_class == "behavior":
            return {"finish_task"}
        return set()

    def observe(self, iteration: int, tool: str, arguments: dict[str, Any], result: str,
                mutation: bool = False, validation: bool = False) -> None:
        with self._lock:
            self._next_event_id += 1
            event_id = self._next_event_id
        event = ContextEvent(event_id, iteration, tool, arguments, str(result),
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
            fallback = _local_judgment(self.events, event)
            process_busy = self._process is not None and self._process.is_alive()
            if ((self._future is not None and not self._future.done()) or process_busy):
                # Keep the newest actionable event instead of losing every
                # event that arrives during a slow 4B call. One slot is
                # intentional: replaying the whole backlog would make the
                # worker permanently chase history rather than current state.
                self._pending_event = event
                self._pending_fallback = fallback
                self.coalesced_events += 1
                return
            self._start_worker_locked(event, fallback)

    def observe_no_action(self, iteration: int, content: str = "") -> None:
        """Record an actor turn that produced prose but no executable action.

        A stalled first turn is itself useful context.  It must not wait for
        the normal eight-event sampling interval, otherwise the small worker
        arrives only after the useful recovery window has already passed.
        """
        with self._lock:
            self._next_event_id += 1
            event_id = self._next_event_id
        event = ContextEvent(
            event_id=event_id,
            iteration=iteration,
            tool="actor_turn_no_action",
            arguments={},
            result=str(content)[:MAX_EVENT_CHARS],
            result_fingerprint=_fingerprint("actor_turn_no_action", {}, str(content)),
        )
        with self._lock:
            self.events.append(event)
            self.events = self.events[-100:]
            self.no_action_turns += 1
            if self._process_mode:
                busy = self._process is not None and self._process.is_alive()
                if busy:
                    self._pending_event = event
                    self._pending_fallback = WorkerJudgment(
                        event_id=event.event_id, phase="mutate", recommended_action="patch_file",
                        blocker="The actor returned no executable tool call.", confidence=0.9,
                    )
                    self.coalesced_events += 1
                    return
                fallback = WorkerJudgment(
                    phase="mutate", recommended_action="patch_file",
                    blocker="The actor returned no executable tool call.",
                    confidence=0.9,
                )
                self._start_worker_locked(event, fallback)
                self.last_judgment = fallback
            elif self._future is None or self._future.done():
                fallback = WorkerJudgment(
                    event_id=event.event_id,
                    phase="mutate", recommended_action="patch_file",
                    blocker="The actor returned no executable tool call.",
                    confidence=0.9,
                )
                self._start_worker_locked(event, fallback)

    def _start_worker_locked(self, event: ContextEvent, fallback: WorkerJudgment) -> None:
        """Start one judgment; caller must hold ``_lock``."""
        self._active_event = event
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
            judgment.event_id = event.event_id
            judgment.latency_ms = (monotonic() - started) * 1000
            return judgment
        except Exception:
            self.worker_failures += 1
            fallback.event_id = event.event_id
            fallback.latency_ms = (monotonic() - started) * 1000
            return fallback

    def collect(self, wait: bool = False, timeout: float | None = None) -> WorkerJudgment:
        if self._process_mode and self._process is not None:
            if wait:
                self._process.join(timeout=timeout)
            if self._process.is_alive() or self._process_conn is None or not self._process_conn.poll():
                return self.last_judgment
            try:
                payload = self._process_conn.recv()
                event = self._active_event or self.events[-1]
                fallback = _local_judgment(self.events, event)
                if payload.get("ok"):
                    judgment = _parse_judgment(payload.get("raw", ""), fallback)
                    judgment.event_id = event.event_id
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
                with self._lock:
                    self._active_event = None
                    self._start_pending_locked()
            return judgment
        future = self._future
        if future is None:
            return self.last_judgment
        if not wait and not future.done():
            return self.last_judgment
        try:
            judgment = future.result(timeout=timeout) if wait and timeout is not None else future.result()
        except Exception:
            self.worker_failures += 1
            if self._future is future:
                self._future = None
            with self._lock:
                self._active_event = None
                self._start_pending_locked()
            return self.last_judgment
        with self._lock:
            self.last_judgment = judgment
            self.judgments.append(judgment)
            if self._future is future:
                self._future = None
            self._active_event = None
            self._start_pending_locked()
        return judgment

    def _start_pending_locked(self) -> None:
        """Launch the newest coalesced event after the active call finishes."""
        if self._pending_event is None:
            return
        event = self._pending_event
        fallback = self._pending_fallback or _local_judgment(self.events, event)
        self._pending_event = None
        self._pending_fallback = None
        self._start_worker_locked(event, fallback)

    def render_for_model(self, action_critic: bool = False) -> str:
        judgment = self.collect(wait=False)
        with self._lock:
            recent = self.events[-self.action_after_events:]
            latest_event_id = recent[-1].event_id if recent else 0
            judgment_is_stale = bool(judgment.event_id and judgment.event_id < latest_event_id)
            if judgment_is_stale:
                self.stale_judgments += 1
            adjacent_failure = len(self.events) >= 2 and all(
                e.result.startswith(("ERROR", "REJECTED")) for e in self.events[-2:]
            ) and _call_key(self.events[-2].tool, self.events[-2].arguments) == _call_key(
                self.events[-1].tool, self.events[-1].arguments
            )
            deterministic_trigger = adjacent_failure or (
                len(recent) >= self.action_after_events
                and not any(e.mutation or e.validation for e in recent)
            )
            critic_trigger = deterministic_trigger or (
                action_critic and (
                    judgment.stagnating or judgment.duplicate_action
                    or (judgment.recommended_action != "inspect" and judgment.confidence >= 0.75)
                )
            )
            critic_judgment = judgment
            if judgment_is_stale:
                # A structured triage checkpoint is more informative than a
                # generic local judgment for a newer event. Preserve its
                # failure-plane diagnosis while still using local fallback for
                # ordinary stale event advice.
                if judgment.diagnosis or judgment.failure_class != "unknown":
                    critic_judgment = judgment
                else:
                    critic_judgment = _local_judgment(self.events, self.events[-1], self.action_after_events)
            # Do not wait for an asynchronous 4B result to become actionable.
            # The local policy supplies a conservative recommendation; a later
            # 4B judgment can refine it on the next turn.
            if action_critic and deterministic_trigger and judgment.recommended_action == "inspect":
                critic_judgment = _local_judgment(self.events, self.events[-1], self.action_after_events)
            # Use the deterministic replacement when the asynchronous worker
            # is behind. The previous code computed critic_judgment but still
            # rendered the stale worker object, so the actor saw obsolete
            # advice while the engine claimed to fall back locally.
            rendered = "## Context manager state\n" + critic_judgment.render()
            if judgment_is_stale:
                rendered += (
                    f"\nWorker judgment is for event {judgment.event_id}, while the latest event is "
                    f"{latest_event_id}; use the deterministic local recommendation until the newer "
                    "judgment arrives."
                )
            if len(self.events) >= 2:
                previous, latest = self.events[-2:]
                same_failure = (
                    previous.result_fingerprint == latest.result_fingerprint
                    and latest.result.startswith(("ERROR", "REJECTED"))
                )
                if same_failure:
                    rendered += (
                        "\nDeterministic recovery: the last two tool calls produced the same failure. "
                        "Do not repeat that call or argument. Change strategy—use list_workspace, "
                        "find_files, search_file, or read_file to establish the correct relative path before retrying."
                    )
            if judgment.stagnating:
                rendered += (
                    "\nContext worker signal: repeated or non-progress actions detected. "
                    "Use the evidence already gathered and take the recommended concrete action "
                    "before performing another routine read."
                )
            if action_critic and critic_trigger:
                rendered += (
                    "\n## Action critic directive (advisory)\n"
                    f"The context critic recommends exactly one next action: {critic_judgment.recommended_action}. "
                    f"Target: {critic_judgment.target or 'not specified'}. "
                    f"Blocker: {critic_judgment.blocker or 'use the latest evidence to choose the exact target'}. "
                    "Use this recommendation if it matches the repository evidence; do not perform broad "
                    "exploration before addressing it."
                )
            if critic_judgment.diagnosis or critic_judgment.next_action:
                rendered += (
                    "\n## Structured repair checkpoint\n"
                    f"Failure class: {critic_judgment.failure_class}. "
                    f"Diagnosis: {critic_judgment.diagnosis or 'use the latest evidence'}. "
                    f"Next action: {critic_judgment.next_action or critic_judgment.recommended_action}. "
                    + ("Preserve: " + ", ".join(critic_judgment.preserve_files) + "."
                       if critic_judgment.preserve_files else "")
                )
            if action_critic and self.no_action_turns:
                rendered += (
                    "\n## Immediate recovery directive\n"
                    "The actor recently returned without an executable tool call. Stop explaining and "
                    "take one concrete action now: use write_file or patch_file to create or change the "
                    "smallest useful artifact, then validate it with run_tests or run_command."
                )
            return rendered + f"\nEvents recorded: {len(self.events)}; worker calls: {self.worker_calls}; " \
                   f"coalesced: {self.coalesced_events}; stale judgments: {self.stale_judgments}."

    def metrics(self) -> dict[str, Any]:
        self.collect(wait=False)
        with self._lock:
            mutations = sum(e.mutation for e in self.events)
            validations = sum(e.validation for e in self.events)
            duplicates = sum(j.duplicate_action for j in self.judgments)
            return {"events": len(self.events), "mutations": mutations, "validations": validations,
                    "worker_calls": self.worker_calls, "worker_failures": self.worker_failures,
                    "worker_busy_drops": self.worker_busy_drops, "coalesced_events": self.coalesced_events,
                    "stale_judgments": self.stale_judgments, "latest_event_id": self.events[-1].event_id if self.events else 0,
                    "judgment_event_id": self.last_judgment.event_id, "judgments": len(self.judgments),
                    "duplicate_judgments": duplicates, "elapsed_s": monotonic() - self.started_at}

    def blocked_calls(self) -> set[tuple[str, str]]:
        """Exact calls that produced the same error twice in succession."""
        with self._lock:
            if len(self.events) < 2:
                return set()
            previous, latest = self.events[-2:]
            if not (previous.result.startswith(("ERROR", "REJECTED")) and
                    latest.result.startswith(("ERROR", "REJECTED"))):
                return set()
            if _call_key(previous.tool, previous.arguments) != _call_key(latest.tool, latest.arguments):
                return set()
            return {_call_key(latest.tool, latest.arguments)}

    def requires_progress(self) -> bool:
        """Whether the next turn should make state-changing progress.

        This is deliberately based on the event ledger, not on a model name
        or an iteration number. It prevents an agent from reading forever
        after it has had a bounded opportunity to orient itself, while still
        allowing a task to validate or mutate before the gate is reached.
        """
        with self._lock:
            recent = self.events[-self.action_after_events:]
            return (len(recent) >= self.action_after_events and
                    not any(e.mutation or e.validation for e in recent))

    def recovery_reads_allowed(self) -> bool:
        """Allow targeted reads while the required mutation is still absent.

        Broad exploration remains gated by agent.py; exact reads/searches are
        necessary for a safe patch and the previous two-turn window expired
        before the 35B could apply one on the real SymPy task.
        """
        with self._lock:
            # One bounded orientation window is enough to identify a target.
            # Keeping reads open for another eight events allowed the actor
            # to reread the same file indefinitely without attempting the
            # already-supported mutation. Once the window closes, the gate
            # still permits patch/validation/command tools, so this is a
            # progress policy rather than a model-specific instruction.
            return len(self.events) < self.action_after_events

    def close(self) -> None:
        # A real 4B call is advisory. Never make task completion wait for it.
        self.collect(wait=False)
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1)
        if self._process_conn is not None:
            self._process_conn.close()
            self._process_conn = None
        # The worker is advisory and its result is no longer useful once the
        # actor has stopped. Never let a model/provider hang delay benchmark
        # shutdown or make the parent appear stuck.
        self._executor.shutdown(wait=False, cancel_futures=True)
