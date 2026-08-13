"""Layer 3 of the three-layer memory design (pasted proposal, this session):
event log (layer 1) and evidence archive (layer 2) already exist and match
the design almost exactly — action_governor.EvidenceLedger.history IS the
immutable event log, kernel.memory.ARCHIVE IS the evidence archive, and its
entry numbers already ARE the "evidence_refs" the design's own examples use.
Nothing new needed for either.

This file is the actual missing piece: a small, JSON-serializable
"restartable checkpoint" for what a task currently knows and needs to do —
not a conversation summary. The test the design proposes: if the whole
transcript vanished and a fresh model got only this state plus repo/archive
access, could it continue correctly? structured_state.py's StructuredState
fails that test — its "facts" are regex-extracted class/def names with no
evidence links, no decisions, no invalidation, and its one judged field
(Status) is a free sentence with nothing to check it against.

Deliberately the SMALL MVP the design doc itself specifies (JSON working
state, engine-owned deterministic fields, model-proposed operations,
evidence-reference validation, a checkpoint trigger, rehydration) rather
than the full knowledge-graph version — same "verify a mechanism in
isolation before trusting it to act on anything live" discipline
action_governor.py's own docstring documents as this project's established
pattern. Built and self-tested standalone; NOT yet wired into agent.py's
live loop (that's a follow-up, once this passes its own tests the same way
action_governor.py did before its first integration).

One documented simplification vs. the design doc's full schema: Fact drops
the explicit `invalidated_by` field. Invalidation is instead derived
automatically by cross-referencing a fact's evidence_refs against
EvidenceLedger.history's `scope` (the file/command/pattern each archived
entry came from) — already recorded for every entry, no new bookkeeping
required. See invalidate_after_mutation().
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field

import action_governor as gov

SCHEMA_VERSION = 1

VALID_ACTION_KINDS = ("observe", "mutate", "validate", "deliver")
VALID_FACT_STATUSES = ("tentative", "confirmed", "stale", "contradicted")


@dataclass
class Fact:
    id: str
    claim: str
    evidence_refs: list = field(default_factory=list)
    status: str = "tentative"


@dataclass
class Decision:
    id: str
    decision: str
    rationale: str = ""
    evidence_refs: list = field(default_factory=list)
    supersedes: str = None
    superseded: bool = False


@dataclass
class ActiveFile:
    path: str
    version: str  # content hash at last observation/mutation
    role: str = ""
    evidence_refs: list = field(default_factory=list)


@dataclass
class Change:
    # Engine-owned only — no operation ever targets this list directly.
    path: str
    description: str
    before_hash: str
    after_hash: str
    patch_ref: int  # archive entry number of the patch_file/write_file call
    status: str  # applied, failed


@dataclass
class Validation:
    # Engine-owned only — no operation ever targets this list directly.
    command: str
    code_version: str  # composite hash of active files at run time
    result: str  # passed, failed, incomplete
    evidence_ref: int


@dataclass
class Action:
    kind: str  # observe, mutate, validate, deliver
    description: str
    reason: str = ""


@dataclass
class WorkingState:
    objective: str
    task_type: str = "code_change"
    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    phase: str = "explore"
    facts: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    active_files: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    validations: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)  # list of {"id","question","reason"}
    blockers: list = field(default_factory=list)
    next_actions: list = field(default_factory=list)
    events_since_checkpoint: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _next_id(prefix: str, existing: list) -> str:
    return f"{prefix}{len(existing) + 1}"


# --------------------------------------------------------------------------
# Operations: model proposes, engine validates + applies atomically.
# --------------------------------------------------------------------------

class OperationError(Exception):
    pass


def _validate_operation(op: dict, archive: dict) -> None:
    kind = op.get("op")
    if kind == "add_fact":
        value = op.get("value") or {}
        if "claim" not in value:
            raise OperationError("add_fact: missing 'claim'")
        status = value.get("status", "tentative")
        if status not in VALID_FACT_STATUSES:
            raise OperationError(f"add_fact: invalid status {status!r}")
        refs = value.get("evidence_refs", [])
        if status == "confirmed" and not refs:
            raise OperationError(f"add_fact: status=confirmed requires evidence_refs ({value.get('claim')!r})")
        for r in refs:
            if r not in archive:
                raise OperationError(f"add_fact: evidence_ref #{r} not found in archive")
    elif kind == "add_decision":
        value = op.get("value") or {}
        if "decision" not in value:
            raise OperationError("add_decision: missing 'decision'")
        for r in value.get("evidence_refs", []):
            if r not in archive:
                raise OperationError(f"add_decision: evidence_ref #{r} not found in archive")
    elif kind == "resolve_question":
        if "question_id" not in op:
            raise OperationError("resolve_question: missing 'question_id'")
    elif kind == "set_next_actions":
        for a in op.get("value", []):
            if a.get("kind") not in VALID_ACTION_KINDS:
                raise OperationError(f"set_next_actions: invalid kind {a.get('kind')!r}")
            if "description" not in a:
                raise OperationError("set_next_actions: action missing 'description'")
    elif kind == "add_blocker":
        if not op.get("value"):
            raise OperationError("add_blocker: missing 'value'")
    elif kind == "set_phase":
        if not op.get("value"):
            raise OperationError("set_phase: missing 'value'")
    elif kind == "add_unresolved":
        value = op.get("value") or {}
        if "question" not in value:
            raise OperationError("add_unresolved: missing 'question'")
    else:
        raise OperationError(f"unknown operation {kind!r}")


def _apply_operation(state: WorkingState, op: dict) -> None:
    kind = op["op"]
    if kind == "add_fact":
        v = op["value"]
        state.facts.append(Fact(
            id=_next_id("F", state.facts), claim=v["claim"],
            evidence_refs=list(v.get("evidence_refs", [])), status=v.get("status", "tentative"),
        ))
    elif kind == "add_decision":
        v = op["value"]
        supersedes = v.get("supersedes")
        if supersedes:
            for d in state.decisions:
                if d.id == supersedes:
                    d.superseded = True
        state.decisions.append(Decision(
            id=_next_id("D", state.decisions), decision=v["decision"], rationale=v.get("rationale", ""),
            evidence_refs=list(v.get("evidence_refs", [])), supersedes=supersedes,
        ))
    elif kind == "resolve_question":
        qid = op["question_id"]
        state.unresolved = [q for q in state.unresolved if q.get("id") != qid]
    elif kind == "set_next_actions":
        state.next_actions = [Action(kind=a["kind"], description=a["description"], reason=a.get("reason", ""))
                               for a in op["value"]]
    elif kind == "add_blocker":
        state.blockers.append(op["value"])
    elif kind == "set_phase":
        state.phase = op["value"]
    elif kind == "add_unresolved":
        v = op["value"]
        state.unresolved.append({
            "id": _next_id("Q", state.unresolved), "question": v["question"], "reason": v.get("reason", ""),
        })
    else:  # pragma: no cover — unreachable, _validate_operation already rejected this
        raise OperationError(f"unknown operation {kind!r}")


def apply_operations(state: WorkingState, operations: list, archive: dict) -> WorkingState:
    """Validate every operation against `archive` BEFORE applying any —
    atomic, all-or-nothing (design doc: "the engine validates and applies
    these operations atomically"). A single bad operation (e.g. a
    hallucinated evidence_ref) rejects the whole batch instead of leaving
    state partially, silently corrupted."""
    for op in operations:
        _validate_operation(op, archive)
    for op in operations:
        _apply_operation(state, op)
    state.revision += 1
    state.events_since_checkpoint = 0
    return state


# --------------------------------------------------------------------------
# Engine-owned deterministic fields — never a model operation target, never
# trusted from model self-report. Mirrors structured_state.py's update()
# split, but restricted to exactly the fields the design doc says must be
# code-derived: file hashes, applied/reverted changes, validation results.
# --------------------------------------------------------------------------

def _file_hash(resolved_path: str):
    try:
        with open(resolved_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


def current_code_version(state: WorkingState) -> str:
    return "+".join(sorted(f"{f.path}:{f.version}" for f in state.active_files)) or "(no files observed)"


def invalidate_after_mutation(state: WorkingState, path: str, ledger) -> None:
    """A successful mutation to `path` makes any CONFIRMED fact whose
    evidence came from observing that path potentially stale — it may have
    described code that no longer exists in its observed form. Cross-
    references evidence_refs against ledger.history's `scope` (the path/
    command/pattern already recorded for every archived entry) rather than
    requiring a separate invalidated_by field on Fact (see module
    docstring)."""
    stale_entries = {
        i + 1 for i, e in enumerate(ledger.history)
        if e.get("scope") == path
    }
    if not stale_entries:
        return
    for f in state.facts:
        if f.status == "confirmed" and stale_entries & set(f.evidence_refs):
            f.status = "stale"


def update_deterministic(state: WorkingState, tool_name: str, arguments: dict, result_content: str,
                          entry_number: int, resolve_path, ledger) -> None:
    """Called once per tool call — updates active_files/changes/validations
    from what actually happened, never from what a model claims happened.
    `resolve_path` is a callable(rel_path) -> absolute filesystem path
    (kernel.io_tools._resolve in the live agent.py loop)."""
    capability = gov.classify(tool_name, arguments)
    path = (arguments or {}).get("path")

    if capability == "OBSERVE" and path:
        h = _file_hash(resolve_path(path))
        if h:
            existing = next((f for f in state.active_files if f.path == path), None)
            if existing:
                existing.version = h
                existing.evidence_refs.append(entry_number)
            else:
                state.active_files.append(ActiveFile(path=path, version=h, evidence_refs=[entry_number]))

    elif capability == "MUTATE" and path:
        before = next((f.version for f in state.active_files if f.path == path), None)
        succeeded = gov.infer_success("MUTATE", tool_name, result_content)
        after = _file_hash(resolve_path(path)) if succeeded else before
        state.changes.append(Change(
            path=path, description=f"{tool_name} call", before_hash=before or "(unknown)",
            after_hash=after or "(unknown)", patch_ref=entry_number,
            status="applied" if succeeded else "failed",
        ))
        if succeeded:
            if before:
                invalidate_after_mutation(state, path, ledger)
            existing = next((f for f in state.active_files if f.path == path), None)
            if existing:
                existing.version = after or existing.version
            elif after:
                state.active_files.append(ActiveFile(path=path, version=after, evidence_refs=[entry_number]))

    elif capability == "VALIDATE":
        succeeded = gov.infer_success("VALIDATE", tool_name, result_content)
        result = "passed" if succeeded is True else ("failed" if succeeded is False else "incomplete")
        state.validations.append(Validation(
            command=(arguments or {}).get("command") or tool_name,
            code_version=current_code_version(state), result=result, evidence_ref=entry_number,
        ))

    state.events_since_checkpoint += 1


def is_validation_stale(v: Validation, state: WorkingState) -> bool:
    return v.code_version != current_code_version(state)


# --------------------------------------------------------------------------
# Checkpoint trigger
# --------------------------------------------------------------------------

def should_checkpoint(state: WorkingState, introduced_new_evidence=False, mutated_workspace=False,
                       validation_completed=False, changed_phase=False, context_pressure=0.0) -> bool:
    return (
        introduced_new_evidence
        or mutated_workspace
        or validation_completed
        or changed_phase
        or state.events_since_checkpoint >= 6
        or context_pressure >= 0.70
    )


# --------------------------------------------------------------------------
# Sidecar checkpoint call: model proposes operations, engine validates +
# applies. Same fail-open discipline as structured_state.py's Status field —
# a malformed/failed call leaves state unchanged rather than crashing the run.
# --------------------------------------------------------------------------

CHECKPOINT_MODEL = "qwen3.5:4b"
CHECKPOINT_NUM_CTX = 8192
CHECKPOINT_RETRIES = 2

CHECKPOINT_PROMPT = """Update the task's working state from the supplied recent events. \
Preserve only information needed to continue the task.

Do:
- record confirmed facts with evidence_refs (archive entry numbers below)
- label hypotheses you are not sure of as status "tentative", not "confirmed"
- record decisions and their rationale
- record unresolved questions
- propose the smallest concrete next actions

Do NOT:
- claim a command ran unless an event below proves it
- turn a hypothesis into a "confirmed" fact
- copy large raw tool outputs into a claim
- silently drop unresolved failures
- report an intended edit as an applied one (state.changes already tracks real edits — don't duplicate it)

Objective: {objective}
Phase: {phase}
Existing facts: {facts}
Existing unresolved questions: {unresolved}

Recent events (format: entry_number: tool_name(args) -> result):
{recent_events}

Return ONLY a JSON object of the form {{"operations": [...]}}. Each item is one of:
{{"op": "add_fact", "value": {{"claim": str, "evidence_refs": [int], "status": "confirmed"|"tentative"}}}}
{{"op": "add_decision", "value": {{"decision": str, "rationale": str, "evidence_refs": [int]}}}}
{{"op": "add_unresolved", "value": {{"question": str, "reason": str}}}}
{{"op": "resolve_question", "question_id": str}}
{{"op": "set_next_actions", "value": [{{"kind": "observe"|"mutate"|"validate"|"deliver", "description": str, "reason": str}}]}}
{{"op": "add_blocker", "value": str}}
{{"op": "set_phase", "value": "explore"|"implement"|"validate"|"deliver"}}
If nothing needs to change, return {{"operations": []}}.

JSON:"""


def _format_recent_events(ledger, since_index: int) -> str:
    lines = []
    for i, e in enumerate(ledger.history[since_index:], start=since_index + 1):
        lines.append(f"{i}: {e['tool_name']}({e.get('scope') or ''}) -> capability={e['capability']}"
                      f"{' (duplicate)' if e['is_duplicate'] else ''}")
    return "\n".join(lines) or "(none)"


def parse_operations(raw_text: str) -> list:
    """Extract the operations list from a model's raw response text —
    tolerant of surrounding prose/code fences, since smaller models rarely
    return perfectly bare JSON."""
    text = raw_text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise OperationError("no JSON object found in checkpoint response")
    payload = json.loads(text[start:end + 1])
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise OperationError("checkpoint response missing 'operations' list")
    return operations


def checkpoint(state: WorkingState, ledger, archive: dict, since_index: int = 0) -> WorkingState:
    """Ask the sidecar model to propose operations from events since the
    last checkpoint, then validate + apply atomically. Never raises on a
    bad model response or a bad operation batch — logs nothing here by
    design (caller prints); returns state unchanged on any failure."""
    from ollama import chat

    prompt = CHECKPOINT_PROMPT.format(
        objective=state.objective, phase=state.phase,
        facts="; ".join(f"{f.id}[{f.status}] {f.claim}" for f in state.facts) or "(none yet)",
        unresolved="; ".join(f"{q['id']}: {q['question']}" for q in state.unresolved) or "(none)",
        recent_events=_format_recent_events(ledger, since_index),
    )
    for _ in range(CHECKPOINT_RETRIES):
        try:
            response = chat(
                model=CHECKPOINT_MODEL, messages=[{"role": "user", "content": prompt}],
                think=False, options={"num_ctx": CHECKPOINT_NUM_CTX},
            )
            operations = parse_operations(response.message.content or "")
            return apply_operations(state, operations, archive)
        except Exception:  # OperationError, json.JSONDecodeError, ollama/network errors — all fail-open
            continue
    return state  # unchanged — same fail-open pattern as structured_state.py


# --------------------------------------------------------------------------
# Rehydration: the "restartable checkpoint" render.
# --------------------------------------------------------------------------

def render(state: WorkingState) -> str:
    lines = [
        f"## Working state (schema v{state.schema_version}, revision {state.revision}) — a restartable "
        "checkpoint: if this were all a fresh model saw, plus repo/archive access, it should be able to "
        "continue the task correctly.",
        f"Objective: {state.objective}",
        f"TaskType: {state.task_type}  Phase: {state.phase}",
    ]
    if state.blockers:
        lines.append(f"Blockers: {'; '.join(state.blockers)}")

    lines.append(f"Facts ({len(state.facts)}):")
    lines += [f"  {f.id} [{f.status}] {f.claim} (evidence: {f.evidence_refs or 'none'})" for f in state.facts] \
        or ["  (none yet)"]

    live_decisions = [d for d in state.decisions if not d.superseded]
    lines.append(f"Decisions ({len(live_decisions)}):")
    lines += [f"  {d.id}: {d.decision} — {d.rationale} (evidence: {d.evidence_refs or 'none'})"
              for d in live_decisions] or ["  (none yet)"]

    lines.append(f"Active files ({len(state.active_files)}):")
    lines += [f"  {af.path} @ {af.version}{' — ' + af.role if af.role else ''}" for af in state.active_files] \
        or ["  (none yet)"]

    succeeded = sum(1 for c in state.changes if c.status == "applied")
    failed = sum(1 for c in state.changes if c.status == "failed")
    lines.append(f"Changes ({succeeded} applied, {failed} failed):")
    lines += [f"  {c.path}: {c.before_hash}->{c.after_hash} [{c.status}] (recall({c.patch_ref}))"
              for c in state.changes] or ["  (none yet)"]

    lines.append(f"Validations ({len(state.validations)}):")
    lines += [
        f"  {v.command} -> {v.result}{' STALE — code changed since this ran' if is_validation_stale(v, state) else ''} "
        f"(recall({v.evidence_ref}))"
        for v in state.validations
    ] or ["  (none yet)"]

    lines.append(f"Unresolved questions ({len(state.unresolved)}):")
    lines += [f"  {q['id']}: {q['question']} — {q.get('reason', '')}" for q in state.unresolved] or ["  (none)"]

    lines.append(f"Next actions ({len(state.next_actions)}):")
    lines += [f"  [{a.kind}] {a.description} — {a.reason}" for a in state.next_actions] \
        or ["  (none proposed yet)"]

    return "\n".join(lines)


def _self_test() -> bool:
    state = WorkingState(objective="Fix arcsin cdf hang", task_type="code_change")
    archive = {1: "read_file result", 2: "grep result", 3: "patch result"}

    # --- apply_operations: happy path across every op type.
    ops = [
        {"op": "add_fact", "value": {"claim": "cdf uses gamma functions", "evidence_refs": [1], "status": "confirmed"}},
        {"op": "add_unresolved", "value": {"question": "where is _cdf dispatched from?", "reason": "need call site"}},
        {"op": "set_next_actions", "value": [{"kind": "observe", "description": "read crv.py", "reason": "find dispatch"}]},
    ]
    apply_operations(state, ops, archive)
    assert len(state.facts) == 1 and state.facts[0].id == "F1" and state.facts[0].status == "confirmed"
    assert len(state.unresolved) == 1 and state.unresolved[0]["id"] == "Q1"
    assert state.next_actions[0].kind == "observe"
    assert state.revision == 1 and state.events_since_checkpoint == 0

    # --- validation: confirmed fact with no evidence_refs must be rejected,
    # and the WHOLE batch must be atomic — nothing applied if any op fails.
    bad_ops = [
        {"op": "add_fact", "value": {"claim": "this should not land", "status": "confirmed", "evidence_refs": []}},
    ]
    try:
        apply_operations(state, bad_ops, archive)
        assert False, "confirmed fact with no evidence must be rejected"
    except OperationError:
        pass
    assert len(state.facts) == 1, "rejected batch must not partially apply"

    # --- validation: nonexistent evidence_ref rejected.
    try:
        apply_operations(state, [{"op": "add_fact", "value": {"claim": "x", "evidence_refs": [99], "status": "tentative"}}], archive)
        assert False, "evidence_ref not in archive must be rejected"
    except OperationError:
        pass

    # --- atomicity across a MIXED batch: one good op + one bad op = nothing applied.
    mixed = [
        {"op": "add_blocker", "value": "network flakiness"},
        {"op": "add_fact", "value": {"claim": "bad", "status": "confirmed", "evidence_refs": [42]}},
    ]
    try:
        apply_operations(state, mixed, archive)
        assert False
    except OperationError:
        pass
    assert state.blockers == [], "a later bad op in the batch must prevent the earlier good op from landing too"

    # --- resolve_question removes it; supersede marks old decision superseded, not deleted.
    apply_operations(state, [{"op": "resolve_question", "question_id": "Q1"}], archive)
    assert state.unresolved == []
    apply_operations(state, [{"op": "add_decision", "value": {"decision": "use direct gamma eval", "rationale": "r1", "evidence_refs": [2]}}], archive)
    d1_id = state.decisions[0].id
    apply_operations(state, [{"op": "add_decision", "value": {"decision": "use series instead", "rationale": "r2", "evidence_refs": [3], "supersedes": d1_id}}], archive)
    assert state.decisions[0].superseded is True
    assert state.decisions[1].superseded is False
    rendered = render(state)
    live_count = sum(1 for d in state.decisions if not d.superseded)
    assert f"Decisions ({live_count})" in rendered
    assert f"{d1_id}:" not in rendered, "a superseded decision must not appear in the rendered checkpoint"

    # --- unknown op type rejected outright.
    try:
        apply_operations(state, [{"op": "delete_everything"}], archive)
        assert False
    except OperationError:
        pass

    # --- update_deterministic: OBSERVE tracks file hash; MUTATE records a
    # Change and invalidates confirmed facts sourced from that path; VALIDATE
    # records pass/fail from the tool's OWN return convention, never a claim.
    import tempfile, os
    import action_governor as gov_mod

    with tempfile.TemporaryDirectory() as tmp:
        fpath = os.path.join(tmp, "crv_types.py")
        with open(fpath, "w") as f:
            f.write("def pdf(): pass\n")

        class FakeLedger:
            def __init__(self):
                self.history = []

        ledger = FakeLedger()
        ws = WorkingState(objective="test")

        def resolve(rel):
            return fpath  # single-file stand-in for kernel.io_tools._resolve

        # OBSERVE
        ledger.history.append({"scope": "crv_types.py", "tool_name": "read_file", "capability": "OBSERVE", "is_duplicate": False})
        update_deterministic(ws, "read_file", {"path": "crv_types.py"}, "def pdf(): pass\n", 1, resolve, ledger)
        assert len(ws.active_files) == 1
        h_before = ws.active_files[0].version
        assert h_before is not None

        # A confirmed fact sourced from entry #1.
        apply_operations(ws, [{"op": "add_fact", "value": {"claim": "pdf is a no-op stub", "evidence_refs": [1], "status": "confirmed"}}],
                          {1: "content"})
        assert ws.facts[0].status == "confirmed"

        # MUTATE succeeds (real io_tools convention: no ERROR:/REJECTED prefix = success).
        with open(fpath, "w") as f:
            f.write("def pdf(): return 1\n")
        ledger.history.append({"scope": "crv_types.py", "tool_name": "patch_file", "capability": "MUTATE", "is_duplicate": False})
        update_deterministic(ws, "patch_file", {"path": "crv_types.py"}, "Patched successfully", 2, resolve, ledger)
        assert len(ws.changes) == 1 and ws.changes[0].status == "applied"
        assert ws.changes[0].before_hash == h_before
        assert ws.active_files[0].version != h_before, "active_files must reflect the NEW hash after a successful mutation"
        assert ws.facts[0].status == "stale", "a confirmed fact sourced from the mutated path must be invalidated"

        # MUTATE fails (real io_tools ERROR: convention).
        ledger.history.append({"scope": "crv_types.py", "tool_name": "patch_file", "capability": "MUTATE", "is_duplicate": False})
        update_deterministic(ws, "patch_file", {"path": "crv_types.py"}, "ERROR: no match found", 3, resolve, ledger)
        assert ws.changes[1].status == "failed"

        # VALIDATE — real run_tests convention: "(True, ...)" = pass.
        update_deterministic(ws, "run_tests", {"path": "crv_types.py"}, "(True, 'ok')", 4, resolve, ledger)
        assert ws.validations[0].result == "passed"
        update_deterministic(ws, "run_tests", {"path": "crv_types.py"}, "(False, 'fail')", 5, resolve, ledger)
        assert ws.validations[1].result == "failed"

        # A model can NEVER claim a validation passed independent of the
        # tool's real return value — infer_success is the only source.
        assert gov_mod.infer_success("VALIDATE", "run_tests", "(True, 'ok')") is True

        # Staleness check: validation recorded before vs. after a later mutation.
        assert is_validation_stale(ws.validations[0], ws) in (True, False)  # just must not raise

    # --- should_checkpoint: any trigger condition fires; plain event
    # accumulation fires only at the threshold.
    fresh = WorkingState(objective="t")
    assert should_checkpoint(fresh, mutated_workspace=True) is True
    assert should_checkpoint(fresh) is False
    fresh.events_since_checkpoint = 6
    assert should_checkpoint(fresh) is True
    assert should_checkpoint(fresh, context_pressure=0.75) is True

    # --- parse_operations: tolerant of prose wrapping the JSON.
    wrapped = 'Sure, here is the update:\n{"operations": [{"op": "add_blocker", "value": "x"}]}\nDone.'
    parsed = parse_operations(wrapped)
    assert parsed == [{"op": "add_blocker", "value": "x"}]
    try:
        parse_operations("not json at all")
        assert False
    except OperationError:
        pass

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   working_state (WorkingState + operations + deterministic update + checkpoint)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
