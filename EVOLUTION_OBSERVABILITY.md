# Evolution Observability and Step-by-Step Analysis

## Purpose

Examining the recursive system at a granular level is the best next step for understanding and improving its evolutionary behavior.

The current system exposes its inputs and final scores, but much of the causal chain between them remains opaque. A detailed trace can show whether evolution is failing because of:

- Weak mutation proposals.
- Poor or misleading feedback.
- Sparse fitness signals.
- Incorrect selection behavior.
- Context loss or truncation.
- Tool-use failures.
- Premature stopping.
- Verifier behavior.
- Accumulated complexity.
- Overfitting or evaluator exploitation.

The objective is not merely to produce more log output. It is to create **causal observability**: the ability to explain why a mutation was proposed, exactly what changed, how behavior changed, and why the mutation survived or was rejected.

## System hierarchy

The recursive program should be observed as a hierarchical state machine:

```text
Run
└── Generation
    ├── Parent selection
    ├── Mutation proposal
    │   ├── Model turn
    │   ├── Tool call
    │   ├── File mutation
    │   └── Result observed
    ├── Candidate evaluation
    │   └── Evaluation task
    │       ├── Solver turn
    │       ├── Tool call
    │       ├── Generated artifact
    │       ├── Checker case
    │       └── Failure classification
    ├── Fitness calculation
    ├── Selection decision
    └── Feedback passed forward
```

This hierarchy connects the outer evolutionary loop to the inner problem-solving loop.

## What should be recorded

### Run level

Each run should begin with an immutable configuration record containing:

- Run ID.
- Start timestamp.
- Initial Git commit.
- Model name and model digest.
- Model parameters.
- Context-window size.
- Random seed when supported.
- Editable files and directories.
- Task-set version.
- Verifier version.
- Search strategy.
- Iteration budget.
- Wall-clock budget.
- Token or model-call budget.
- Tool-execution limits.
- Relevant environment and dependency versions.

Without this record, two runs may differ for reasons that cannot be identified later.

### Generation level

For every generation, record:

- Generation ID.
- Parent generation ID.
- Parent Git SHA.
- Parent fitness.
- Why the parent was selected.
- Feedback supplied to the mutation model.
- Files before mutation.
- Files after mutation.
- Exact unified diff.
- Before-and-after file hashes.
- Mutation size.
- Complexity change.
- Candidate fitness.
- Acceptance or rejection.
- Exact selection rule and values used in the decision.
- Total model calls.
- Token usage when available.
- Wall-clock duration.
- Child Git SHA if accepted.

### Mutation-model level

For each model turn during self-modification, record:

- Turn ID.
- Full system prompt.
- Full user prompt.
- Prior messages included in context.
- Model response.
- Tool calls and arguments.
- Tool results.
- Turn duration.
- Input and output token counts when available.
- Context size and any truncation.
- Files changed by the turn.
- Diff produced by each write or patch.
- Whether the turn produced measurable progress.

This makes it possible to distinguish a poor strategy from a promising strategy that was executed incorrectly.

### Inner solver level

For every evaluation task, record:

- Task ID.
- Goal supplied to the candidate harness.
- Solver configuration.
- Every inner model response.
- Every inner tool call and result.
- Files created or modified.
- Per-turn file diffs.
- Syntax-validation results.
- Execution results.
- Verification feedback returned to the model.
- Number of repair cycles.
- Final artifact path and digest.
- Whether the harness reported success.
- Whether the external verifier agreed.
- Total time, tokens, and tool calls.

This level should reveal patterns such as:

- The model understood the task but exhausted its iteration budget.
- The harness stopped too early.
- Verification feedback was too vague to support repair.
- Context accumulated irrelevant information.
- A tool call failed and recovery never occurred.
- A correct file existed but the harness tracked the wrong target.
- The candidate passed its own checks but failed external verification.

### Checker level

A single Boolean fitness result is too coarse. Each checker should emit structured case results.

For example:

```json
{
  "checker": "binary_search",
  "cases": [
    {
      "case": "middle_element",
      "passed": true
    },
    {
      "case": "missing_element",
      "passed": false,
      "expected": -1,
      "actual": 0
    }
  ]
}
```

Structured cases provide a richer fitness signal and support a useful failure taxonomy.

Sensitive hidden inputs should not be fed back to the evolving system. They may still be stored in a sealed research trace for human analysis.

## Questions every trace should answer

For any accepted generation, the trace should make it possible to answer:

1. What hypothesis or feedback caused this mutation?
2. What exactly changed?
3. Which observable behavior changed?
4. Which tasks improved?
5. Which tasks regressed?
6. Was the gain repeatable across seeds or runs?
7. Did the mutation consume more compute?
8. Was the changed code causally responsible for the gain?
9. Did the improvement transfer to held-out tasks?
10. Was the candidate accepted because it improved, tied, or satisfied another criterion?
11. What information from this generation was passed forward?
12. Did complexity increase without measurable benefit?

If the trace cannot answer these questions, improvement cannot be confidently attributed.

## Analysis views

Once events are structured, several derived views become possible.

### Generation timeline

Show every proposal, model turn, file mutation, evaluation, and selection decision in chronological order.

### Lineage graph

Show parent-child relationships, fitness, accepted and rejected branches, and archived variants.

### Mutation-to-effect matrix

Relate changed files or subsystems to task improvements and regressions.

### Fitness curves

Plot development, selection, and held-out scores separately across generations.

### Compute curves

Plot performance against:

- Input tokens.
- Output tokens.
- Model calls.
- Tool calls.
- Wall-clock time.
- Accepted mutations.

### Failure distribution

Classify failures into categories such as:

- Planning.
- Task misunderstanding.
- Tool selection.
- Tool execution.
- Invalid syntax.
- Runtime exception.
- Incorrect behavior.
- Signature mismatch.
- Verification timeout.
- Context exhaustion.
- Premature completion.
- Selection or bookkeeping error.

### Complexity curve

Track source size, prompt size, tool count, branch count, and control-flow complexity across generations.

### Task-transfer matrix

Show whether a mutation motivated by one failure improves, harms, or has no effect on other tasks.

### Regression map

Show which capabilities were lost by each accepted mutation and when they were recovered.

## Causal testing

Observation reveals correlations, but understanding evolution also requires intervention.

For promising mutations:

1. Revert only that mutation and rerun evaluation.
2. Apply the mutation to a different parent.
3. Run the same candidate across multiple model seeds.
4. Evaluate it on unseen task variants.
5. Compare it with an equal-size neutral or random edit.
6. Test individual parts of a multi-part mutation separately.
7. Combine it with independently successful mutations and re-verify.
8. Measure whether its benefit persists under a newer verifier.

These experiments distinguish causally useful mutations from changes that merely accompanied a lucky model response.

## Existing observability

The repository already records useful information through:

- Accepted Git commits.
- Parent-child relationships.
- Per-generation JSONL summaries.
- Self-edit subprocess output.
- Per-task pass/fail results.
- Git history and diffs.
- Bounded iteration and wall-clock outcomes.

These pieces provide a useful foundation, but they do not yet form one connected trace across the outer and inner loops.

## Current observability gaps

Important missing elements include:

- Stable IDs connecting runs, generations, tasks, turns, and tool calls.
- One structured event format shared by every component.
- Structured model-turn records.
- Per-checker-case outcomes.
- Token and context measurements.
- File diffs after each individual mutation tool call.
- Consistent failure classifications.
- Repeated evaluation for variance estimation.
- Clear separation of development, selection, and held-out scores.
- Explicit machine-readable selection rationale.
- Causal ablation results.
- Artifact hashes connecting evaluated output to stored output.
- A single index linking JSONL events, Git commits, raw logs, and artifacts.

## Proposed event format

Use an append-only structured event stream. JSONL is suitable for the first implementation because it is easy to inspect, stream, version, and analyze.

Example:

```json
{
  "schema_version": 1,
  "run_id": "run-001",
  "generation_id": 3,
  "task_id": "binary-search-07",
  "span_id": "solver-turn-4",
  "parent_span_id": "task-binary-search-07",
  "event": "tool_result",
  "timestamp": "2026-08-07T12:00:00Z",
  "payload": {}
}
```

Recommended common fields:

- `schema_version`
- `run_id`
- `generation_id`
- `task_id`
- `span_id`
- `parent_span_id`
- `event`
- `timestamp`
- `sequence`
- `payload`
- `artifact_refs`

Large prompts, responses, source files, and diffs can be stored as content-addressed artifacts. Events should reference their digests rather than duplicating large payloads throughout the trace.

## Suggested event types

Initial event types could include:

- `run_started`
- `run_finished`
- `baseline_scored`
- `generation_started`
- `parent_selected`
- `feedback_constructed`
- `model_call_started`
- `model_call_finished`
- `tool_call_started`
- `tool_call_finished`
- `file_mutated`
- `candidate_ready`
- `evaluation_started`
- `task_started`
- `solver_turn_finished`
- `artifact_generated`
- `checker_case_finished`
- `task_finished`
- `fitness_computed`
- `candidate_accepted`
- `candidate_rejected`
- `generation_finished`
- `error`
- `timeout`

## Logging and feedback separation

The research trace and the evolutionary feedback channel must remain separate.

The trace may contain detailed hidden-case data for later human analysis. The feedback given to the evolving system should contain only the information allowed by the experiment design.

This prevents observability from accidentally weakening evaluation by leaking hidden tests or expected outputs into future generations.

## Storage strategy

Use two layers:

1. **Structured event index**

   A compact append-only JSONL stream containing IDs, event metadata, metrics, decisions, and artifact references.

2. **Content-addressed artifacts**

   Store prompts, responses, source snapshots, diffs, candidate files, verifier output, and raw logs under their SHA-256 digests.

This avoids unnecessary duplication and guarantees that analysis refers to the exact artifacts produced during a run.

## Controlling trace volume

Granular tracing can become expensive and difficult to inspect. Use two levels:

- Record compact structured events for every generation.
- Preserve full verbose artifacts for selected generations, failures, accepted improvements, and sampled control cases.

The trace should also record when content is truncated or omitted. Silent truncation would undermine later analysis.

## Replay

A useful trace should support partial deterministic replay.

Tool calls, checker calls, selection decisions, and file transitions can often be replayed exactly from recorded inputs. Model generation may remain nondeterministic, so record:

- Exact prompt and message context.
- Model identifier and digest.
- Generation parameters.
- Seed when available.
- Raw model response.

Replay should distinguish between:

- Replaying recorded model output through the rest of the system.
- Re-running model inference with the same inputs.

The first tests orchestration deterministically. The second measures model variance.

## Recommended first investigation

Before changing the evolutionary algorithm further, instrument one complete baseline run and manually dissect five cases:

1. The baseline evaluation.
2. One rejected mutation.
3. One neutral accepted mutation.
4. One genuinely improving mutation.
5. One regression.

For each case, reconstruct:

```text
feedback
  → proposed reasoning or strategy
  → model turns
  → tool calls
  → source diff
  → changed inner-loop behavior
  → checker results
  → fitness
  → selection decision
  → feedback to the next generation
```

This small sample will show which instrumentation is actually useful before investing in a larger dashboard or visualization layer.

## Core distinction

Two forms of evolution must be observed simultaneously:

### Artifact evolution

How source code, prompts, tools, configuration, and stored artifacts change across generations.

Git already captures much of this reasonably well.

### Behavioral evolution

How planning, tool use, recovery, verification response, resource consumption, and success patterns change across generations.

This is the missing layer. A hierarchical event trace should connect behavioral changes to exact source mutations and selection outcomes.

## Conclusion

A granular, end-to-end examination of the recursive program is likely to produce more insight than immediately adding new evolutionary mechanisms. It can expose where useful variation originates, where information is lost, why selection preserves particular changes, and whether apparent gains are real, repeatable, and causal.

Once artifact evolution and behavioral evolution are connected in one trace, changes to fitness, feedback, mutation scope, memory, or search strategy can be made from evidence rather than inferred from final scores alone.
