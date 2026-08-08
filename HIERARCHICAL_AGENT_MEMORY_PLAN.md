# Hierarchical Agent Memory and Control Plan

## Executive summary

The agent should not replay its entire conversation indefinitely or rely on prose-based watchdogs. It should maintain a bounded context assembled from durable, structured state and selectively retrieved evidence.

The architecture has four core parts:

1. A lossless event and artifact store.
2. Hierarchical working memory organized around subgoals.
3. A context compiler that builds each model request from relevant state.
4. An evidence-driven controller that advances work through explicit phases.

This is abstractly applicable to coding, research, data analysis, planning, debugging, operations, and other long-horizon tasks. The memory and controller are general; the evidence extractors, tools, and verifier must be adapted to each domain.

## What is wrong with the current harness

The current agent appends every assistant and tool message to one conversation. This causes context growth, information dilution, and eventual loss of important details. Tool output is truncated, but the discarded portion is not retrievable.

The harness also lacks durable representations of:

- Current subgoal and phase.
- Confirmed facts and their sources.
- Hypotheses and rejected alternatives.
- Files, symbols, documents, or entities inspected.
- Changes made and their rationale.
- Tests, experiments, and observations.
- Open questions and blockers.

The current watchdog infers progress from phrases such as “the bug is.” That is not a reliable measure of state change. A model can comply with a required format without performing the intended action.

The current model settings also require controlled evaluation. The local model uses a 32K context, temperature 1, presence penalty 1.5, and the harness disables thinking. Test these settings independently before attributing all failures to memory.

## General architecture

```text
Task contract
     |
     v
Evidence-driven controller
     |
     +--> bounded context compiler --> model
     |
     +--> structured task state
     |
     +--> hierarchical memory and retrieval
     |
     +--> lossless event/artifact store
     |
     v
Tools, environment, verifier
```

The model should see a compact, decision-specific context. The full history remains available outside the prompt and can be expanded when exact details are needed.

## Memory hierarchy

### Layer 0: immutable task contract

Always preserve:

- Original task or issue.
- Environment and repository identity.
- Constraints and permitted operations.
- Acceptance criteria.
- External verification rules.

This prevents summaries from gradually changing the task definition.

### Layer 1: active working set

Include only the information needed for the current decision:

- Current phase and subgoal.
- Last few action/observation pairs.
- Current hypothesis or question.
- Relevant retrieved evidence.
- Immediate next-action requirement.

For a 32K context, initially target 6K–10K tokens for this layer and reserve space for the model response and tool schemas.

### Layer 2: structured task state

Maintain a typed state object such as:

```json
{
  "phase": "localize",
  "objective": "",
  "current_subgoal": "",
  "acceptance_criteria": [],
  "confirmed_facts": [],
  "hypotheses": [],
  "rejected_hypotheses": [],
  "inspected_entities": [],
  "changed_entities": [],
  "experiments": [],
  "open_questions": [],
  "next_action": {},
  "blockers": []
}
```

Every important fact should have provenance: source artifact, location, timestamp, and the version or hash under which it was observed.

### Layer 3: subgoal episodes

Each completed or abandoned subgoal becomes an episode containing:

- Goal and success condition.
- Actions taken.
- Evidence discovered.
- Conclusion.
- Rejected alternatives.
- Relevant entities and artifacts.
- Verification results.
- Links to the raw trajectory.

Summaries should be compact but reversible through their artifact links.

### Layer 4: domain knowledge

Use deterministic indexes where possible instead of asking the model to remember everything. Examples include:

- Code symbols and dependencies.
- Document sections and citations.
- Dataset schemas and transformations.
- Experiment configurations and results.
- Workflow states and resource identifiers.

### Layer 5: cross-task procedural memory

After independent evaluation, store reusable procedures, successful strategies, failure patterns, and environment conventions. Keep task-specific answers and hidden evaluation information quarantined.

## Lossless event and artifact storage

Store every event before presenting a shortened preview to the model:

```text
.evolution/runs/<run-id>/
├── events.jsonl
├── state.json
├── episodes/
├── artifacts/
├── checkpoints/
└── metrics.json
```

Each tool event should include:

- Tool name and arguments.
- Full result stored as an artifact.
- Short prompt preview.
- Exit status or error type.
- Entity/file versions before and after the call.
- Timestamp and parent event.

The model should be able to request an exact artifact range when the preview is insufficient.

## Context compiler

Build every model request afresh instead of mutating one unbounded `messages` list.

The compiler should combine:

1. System policy and tool instructions.
2. Immutable task contract.
3. Current structured state.
4. Retrieved evidence for the current decision.
5. A short recent-event tail.
6. A clear next-action contract.

Retrieval should prefer:

- Exact entity, path, symbol, or identifier matches.
- Current-phase relevance.
- Causal dependencies.
- Unresolved-question relevance.
- Recent evidence.

Penalize stale evidence and redundant evidence. Embeddings may be a fallback, but structured and lexical retrieval should come first.

## Evidence-driven controller

Use a domain-neutral state machine with domain-specific phase adapters:

```text
orient → reproduce/observe → localize/analyze → act → verify → review → finish
                 ↑                    ↓
                 └──── revise/replan ─┘
```

The names can change by domain. For example:

- Coding: inspect, reproduce, localize, patch, test.
- Research: scope, gather evidence, analyze, draft, fact-check.
- Data work: inspect schema, profile, transform, validate, report.
- Operations: observe, diagnose, change, health-check, roll back.

Transitions should be caused by evidence, not by phrases in the model response.

### Hypothesis ledger

Represent each hypothesis with:

```json
{
  "claim": "",
  "predicted_observation": "",
  "disconfirming_observation": "",
  "status": "untested",
  "evidence_ids": []
}
```

The next action must test, confirm, reject, or replace a hypothesis. Repeating an explanation without new evidence is not progress.

### Progress signals

Count measurable state changes:

- New source or entity inspected.
- New evidence recorded.
- Hypothesis confirmed or rejected.
- Experiment or test completed.
- Artifact changed.
- Subgoal completed.
- Blocker resolved.

After several turns with no state change, retrieve forgotten evidence, run a disconfirming experiment, switch subgoals, restore a checkpoint, or request a structured review.

## Model-facing memory operations

Provide a small memory API:

```text
memory_status()
memory_recall(query, scope, max_tokens)
memory_expand(artifact_id, offset, length)
subgoal_create(goal, success_condition)
subgoal_complete(conclusion, evidence_ids)
hypothesis_record(claim, prediction, falsifier)
decision_record(choice, evidence_ids)
checkpoint_create(label)
checkpoint_restore(id)
```

Important state must also be updated automatically from tool events. Do not depend entirely on the model choosing to record its own memory.

Examples:

- A read operation records the entity, range, and version.
- A write invalidates stale observations.
- An experiment records inputs, outputs, and exit status.
- A verifier updates acceptance-criterion coverage.
- A rollback restores the corresponding state snapshot.

## Implementation sequence for `evolutionEngine`

### Phase 0: baseline and instrumentation

- Capture complete trajectories.
- Record prompt/output token counts, latency, tool calls, diffs, and verification.
- Run multiple tasks and seeds.
- Test thinking mode, temperature, presence penalty, and context budget separately.

### Phase 1: lossless event store

Add `memory/events.py`, `memory/artifacts.py`, `memory/schema.py`, and `memory/run_store.py`. Integrate recording into tool dispatch before previews are truncated.

### Phase 2: deterministic state reducers

Add reducers for repository/entity state, changes, experiments/tests, and progress. Derive as much state as possible from tools, Git, parsers, and verifiers.

### Phase 3: bounded context compiler

Add `context/budget.py`, `context/compiler.py`, `context/render.py`, and `context/retrieval.py`. Keep the current append-all mode as an experimental baseline.

### Phase 4: phase and subgoal controller

Add explicit phases, transition rules, completion checks, checkpoints, rollback, and evidence-based stagnation handling.

### Phase 5: hierarchical episodes

Summarize at subgoal boundaries, phase transitions, checkpoint creation, or context-pressure thresholds. Verify summaries against their source artifacts.

### Phase 6: domain adapters and verifiers

Implement adapters for SWE-bench first, then abstract the same interfaces for other domains. The adapter should provide observation extraction, action validation, progress signals, and external completion checks.

### Phase 7: cross-run procedural memory

Distill strategies only from externally evaluated successes and failures. Validate a lesson across multiple runs before promoting it to reusable memory.

### Phase 8: learned memory policy

Only after the deterministic system is stable, train or fine-tune a memory controller using trajectories labeled by downstream value and token cost. A SWE-MeM-style learned policy is a later optimization, not the foundation.

## Evaluation plan

Compare four systems:

1. Current append-all baseline.
2. Sliding window plus flat summary.
3. Subgoal-based hierarchical compression.
4. Lossless hierarchy plus context compiler and controller.

Measure:

- External task success or resolve rate.
- Success by task family and difficulty.
- Tokens and wall time per task.
- Peak context occupancy.
- Time to first useful action.
- Diagnosis-to-action delay.
- Number of meaningful action/verification cycles.
- Repeated reads and repeated failed actions.
- Memory retrieval precision.
- Summary fidelity.
- False completion rate.
- Agent-reported versus externally verified completion.

Use at least five development tasks with multiple seeds, then a broader held-out set. The primary criterion should be improved externally verified success under an equal token or model-call budget.

## Applicability beyond software engineering

The architecture generalizes when a problem has:

- A long sequence of dependent decisions.
- External observations or tool results.
- Intermediate state that must persist.
- A verifier, evaluator, or measurable completion condition.

It applies naturally to research, data analysis, planning, operations, document workflows, scientific investigation, and interactive environments.

The domain-specific adapter must define:

1. What counts as an entity or artifact.
2. How observations are captured.
3. What facts become stale after changes.
4. Which actions are reversible.
5. What constitutes measurable progress.
6. How completion is externally verified.

For open-ended creative work with no reliable verifier, use human review, rubric-based evaluation, or checkpoint comparison instead of pretending that a Boolean completion signal exists.

## Recommended priority

The highest-value order is:

1. Instrument complete trajectories.
2. Store full tool results losslessly.
3. Add structured state and provenance.
4. Compile bounded contexts instead of replaying all messages.
5. Add evidence-driven phases and subgoals.
6. Add reversible hierarchical summaries and retrieval.
7. Add cross-run procedural memory.
8. Explore learned memory policies.

The objective is not to let the model continue indefinitely. It is to make each turn see the right state, produce a measurable state transition, and recover exact evidence when necessary.
