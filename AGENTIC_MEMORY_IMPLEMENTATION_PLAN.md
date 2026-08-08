# Agentic Hierarchical Memory for SWE Agents

## Implementation handoff

This document is an execution plan for extending `evolutionEngine` into a reliable long-horizon software-engineering agent. It combines:

- Hierarchical subgoal working memory from HiAgent.
- Lossless hierarchical organize/retrieve ideas from HORMA.
- Agent-controlled long-term and short-term memory operations from AgeMem.
- Trajectory-derived procedural memory and test-time learning from ReasoningBank-style systems.

The implementation is deliberately phased. The first phases must be deterministic and auditable. Learned memory policy and reinforcement learning come only after the runtime, trace schema, evaluator, and baselines are trustworthy.

Primary objective:

> Increase externally verified SWE task resolution and reduce wasted context/tool usage under a fixed model, token, and wall-clock budget.

Secondary objectives:

- Make every important decision recoverable from a trace.
- Keep active model context bounded without destroying exact evidence.
- Convert SWE trajectories into useful training, evaluation, and regression data.
- Make memory policies portable to non-SWE long-horizon tasks.

## Grounding and evidence

The current `evolutionEngine` loop appends all model and tool messages to one conversation. Tool previews are capped, but discarded output is not retrievable. The current watchdog infers stagnation from phrases and repeated symbols. The SWE findings show that the model can reach a correct diagnosis, fail to act, write a partial patch, or report completion without external success.

The two complementary approaches provide different capabilities:

| Approach | Useful contribution | What must be adapted |
|---|---|---|
| HiAgent | Subgoals create natural memory chunks; completed chunks can be summarized and removed from active context. | SWE subgoals need code/test evidence and repository-aware completion gates. |
| HORMA | Summaries link to raw trajectories; hierarchical navigation avoids flat, lossy retrieval. | Start with deterministic/structured retrieval before training a navigation policy. |
| AgeMem | Memory is exposed as explicit add/update/delete/retrieve/summary/filter actions; memory policy can be learned jointly with task outcomes. | Do not make the model responsible for critical bookkeeping before automatic reducers exist. |
| ReasoningBank | Successful and failed trajectories can become reusable procedural memory. | Store only externally validated, task-general lessons; quarantine task answers and hidden tests. |

AgeMem reports average gains from 28.05% to 41.96% for Qwen2.5-7B and from 43.97% to 54.31% for Qwen3-4B across five non-SWE benchmarks, with RL contributing roughly 8.5–8.7 percentage points over its non-RL version. These results justify testing memory learning, but do not establish equivalent gains for a local Qwen 35B SWE agent. The paper uses an 8K context and trains primarily on HotpotQA. See [AgeMem](https://arxiv.org/html/2601.01885v2).

## Design principles

1. **Externalize full history, inject only relevant state.** Raw trajectories belong in durable storage, not in every prompt.
2. **Preserve evidence links.** Every summary or claim must point to exact source events, files, lines, commands, or test results.
3. **Separate task memory from control state.** The agent needs both knowledge and a controller that knows which phase and subgoal it is executing.
4. **Use automatic bookkeeping first.** Tool calls, Git changes, tests, and verifier results update state without waiting for the model to remember to do so.
5. **Treat memory actions as decisions.** Add, update, delete, retrieve, summarize, and filter should be explicit operations with logs and costs.
6. **Use evidence, not prose, for progress.** A repeated diagnosis is not progress unless it produces a new observation, change, or verification result.
7. **Never trust self-reported completion.** The external verifier is authoritative.
8. **Train only after instrumentation is reliable.** Otherwise the system learns from missing traces, mislabeled success, and accidental evaluator artifacts.
9. **Keep context budgets explicit.** A 32K nominal window is not a requirement to fill 32K tokens.
10. **Make every policy ablatable.** The runtime must compare append-all, flat summary, hierarchy-only, and learned memory variants.

## Enforceable evidence versus semantic claims

This distinction is fundamental:

> The runtime can verify that observable state changed after a claim. It generally cannot verify that an arbitrary natural-language claim is semantically true.

For example, the runtime cannot deterministically prove that the model has identified the function causing a bug. An LLM judge asked to evaluate that claim would create a second self-report channel, not an independent oracle.

Therefore, the system must use two separate concepts:

### Reducer-visible facts

These are enforceable because they come from tools, parsers, Git, or an external verifier:

- A file or symbol was read.
- A command ran and returned an exit code.
- A test passed or failed.
- A file changed and has a specific diff.
- A hypothesis prediction was tested and an expected observation occurred.
- A subgoal produced a new artifact or state transition.
- The external SWE verifier reported resolved or unresolved.
- The final diff satisfies mechanical policy checks.

### Model assertions

These are useful for planning and retrieval, but remain untrusted annotations:

- “This is the root cause.”
- “The patch is complete.”
- “This file is the only file that matters.”
- “The test failure is unrelated.”

Store these assertions with provenance and confidence if useful, but never treat them as verified merely because they are well written or because another LLM agrees.

The controller should consequently use names such as `evidence_linked`, `prediction_observed`, and `mechanically_verified`, rather than implying that it has proved semantic truth. An assertion can be promoted to a working hypothesis when it has a falsifiable prediction; it is not promoted to fact until an independent executable check supports it.

The system is still valuable under this limitation: it can enforce that the agent is producing new observable evidence, making bounded changes, testing consequences, and satisfying external checks. It cannot guarantee that every intermediate explanation is correct.

### Mechanical completion gate

The runtime may enforce a gate such as:

```text
task contract present
AND repository is at the expected base/working-tree lineage
AND relevant diff is nonempty
AND forbidden-file policy passes
AND required command/test observations exist
AND targeted verification passes, when available
AND final diff review was performed
AND external SWE verifier reports resolved
```

If targeted or external verification is unavailable, the runtime must emit an explicit `unverified` result rather than infer success. A model assertion can explain why the agent believes the patch is correct, but it cannot satisfy a missing mechanical predicate.

### Generic (pre-Phase-6) implementation of this gate

Two of the eight predicates above ("repository is at the expected base/working-tree lineage" and "forbidden-file policy passes") implicitly assume SWE instance metadata — a known `base_commit`, a known forbidden-file list — that only exists once Phase 6's `swe/` adapter is built. A third ("external SWE verifier reports resolved") is Phase 6 by definition. Until then:

- **Buildable now, exactly as specified**: task contract present; relevant diff is nonempty (`changed_entities` from `memory/reducers.py`); required command/test observations exist (`test_runs`/`shell_runs`); final diff review was performed (a `git_diff`/`diff_files` call before `finish_task`, when the project is a git repo).
- **Buildable now, in a deliberately narrower form**: repository lineage — capture the git HEAD commit at run start and require it unchanged at completion. This confirms the checkout wasn't swapped out from under the agent mid-run; it does **not** confirm the checkout matches some externally-known intended base commit (that requires instance metadata) — narrower claim, but real and free, since `git_status`/`git_diff` tools already exist. Forbidden-file policy — an optional `forbidden_paths` glob list, checked against `changed_entities`' paths, passed into `run_agent()` rather than derived from instance metadata that doesn't exist generically.
- **Not buildable until Phase 6**: the external SWE verifier predicate. Falls through to the `unverified` outcome exactly as designed above — not a failure, an honest "we don't know."

Consequence worth stating plainly: until Phase 6, most runs will end `outcome: "unverified"`, not `"resolved"` — including ones that are actually correct, since there is no external oracle to confirm them. That's the intended behavior, not a gap to route around by loosening the gate.

## Target runtime

```text
Task contract + repository snapshot
              |
              v
       SWE task controller
  orient → reproduce → localize → patch → verify → review → finish
              |
    +---------+----------+------------------+
    |                    |                  |
    v                    v                  v
Structured state   Hierarchical memory   Progress monitor
    |                    |                  |
    +--------------------+------------------+
                         v
                  Context compiler
                         |
                         v
                    Qwen 35B
                         |
                         v
                 Tools / Docker verifier
                         |
                         v
              Lossless event + artifact store
```

## Repository layout to implement

Add the following modules without breaking the current baseline:

```text
evolutionEngine/
├── agent.py                         # compatibility entry point
├── dispatch.py                       # tool execution + event recording
├── controller/
│   ├── __init__.py
│   ├── phases.py                     # phase definitions and transitions
│   ├── state_machine.py              # controller runtime
│   ├── subgoals.py                   # subgoal DAG and episode boundaries
│   ├── progress.py                   # evidence-based progress/stagnation
│   ├── completion.py                 # local and external completion gates
│   └── checkpoints.py                # Git/worktree snapshots and rollback
├── memory/
│   ├── __init__.py
│   ├── schema.py                     # typed records and schema versioning
│   ├── store.py                      # SQLite/JSONL metadata store
│   ├── artifacts.py                  # content-addressed full outputs
│   ├── events.py                     # append-only event writer/reader
│   ├── state.py                      # materialized task state
│   ├── reducers.py                   # automatic state reducers
│   ├── episodes.py                   # subgoal memory chunks
│   ├── retrieval.py                  # structured, lexical, semantic retrieval
│   ├── summaries.py                  # summary creation and fidelity checks
│   └── procedural.py                 # cross-run validated lessons
├── context/
│   ├── __init__.py
│   ├── budget.py                     # token allocation and estimation
│   ├── compiler.py                   # fresh prompt assembly per turn
│   ├── render.py                     # stable model-facing state format
│   └── policies.py                   # append-all/window/hierarchical modes
├── trajectory/
│   ├── __init__.py
│   ├── ingest.py                     # normalize existing SWE traces
│   ├── dataset.py                    # train/dev/test trajectory splits
│   ├── labels.py                     # action/progress/failure labels
│   ├── replay.py                     # deterministic replay and perturbation
│   └── analysis.py                   # derived metrics and reports
├── swe/
│   ├── adapter.py                    # SWE-specific entity/evidence adapter
│   ├── tests.py                      # test result parser and classifier
│   ├── issue.py                       # issue/acceptance extraction
│   ├── repository.py                  # symbols, files, dependencies, Git
│   └── verifier.py                    # Docker/SWE-bench external gate
└── experiments/
    ├── configs/
    ├── run_ablation.py
    ├── run_swe_batch.py
    └── report.py
```

## Canonical data model

All records require a schema version, run ID, task ID, timestamp, and provenance. Use immutable event records and materialized state snapshots.

### Run record

```json
{
  "schema_version": 1,
  "run_id": "...",
  "task_id": "pytest-dev__pytest-7220",
  "model": "qwen3.6:35b-mlx",
  "model_digest": "...",
  "model_options": {
    "temperature": 0.3,
    "presence_penalty": 0.0,
    "num_ctx": 32768,
    "think": false
  },
  "repository_commit": "...",
  "verifier_version": "...",
  "memory_policy": "hierarchical-v1",
  "iteration_budget": 100,
  "token_budget": null,
  "seed": null
}
```

### Event record

```json
{
  "event_id": "evt-000042",
  "run_id": "...",
  "parent_event_id": "evt-000041",
  "phase": "localize",
  "subgoal_id": "sg-03",
  "event_type": "tool_result",
  "timestamp": "...",
  "payload": {},
  "artifact_id": "sha256:...",
  "repo_tree_hash": "...",
  "prompt_preview": "...",
  "input_tokens": 9321,
  "output_tokens": 418,
  "latency_ms": 4200
}
```

### Evidence record

```json
{
  "evidence_id": "ev-17",
  "claim": "The display path is computed relative to the process cwd.",
  "kind": "source_observation",
  "source_artifacts": ["sha256:..."],
  "locations": [{"path": "src/.../code.py", "start": 120, "end": 128}],
  "observed_at_tree_hash": "...",
  "status": "confirmed",
  "stale_after": "file-edit-or-tree-change"
}
```

### Hypothesis record

```json
{
  "hypothesis_id": "hyp-04",
  "claim": "...",
  "predicted_observation": "...",
  "disconfirming_observation": "...",
  "status": "untested",
  "evidence_ids": [],
  "next_action": "run targeted reproducer"
}
```

### Subgoal episode

```json
{
  "subgoal_id": "sg-03",
  "parent_id": "sg-01",
  "goal": "Trace how the wrong path is constructed",
  "success_condition": "Identify exact function and causal expression",
  "status": "complete",
  "summary": "...",
  "evidence_ids": ["ev-17", "ev-18"],
  "raw_event_ids": ["evt-22", "evt-23", "evt-24"],
  "changed_entities": [],
  "verification": null
}
```

## Memory operations

Expose AgeMem-inspired operations as tools, but make them safe and observable:

```text
memory_status()
memory_add(kind, content, evidence_ids, scope)
memory_update(memory_id, content, evidence_ids)
memory_delete(memory_id, reason)
memory_retrieve(query, scope, max_tokens)
memory_summary(source_event_ids, target_tokens)
memory_filter(criteria, active_context_id)
memory_expand(artifact_id, offset, length)
subgoal_create(goal, success_condition, parent_id)
subgoal_complete(subgoal_id, conclusion, evidence_ids)
hypothesis_record(claim, prediction, falsifier)
hypothesis_resolve(hypothesis_id, status, evidence_id)  # status in {prediction_observed, prediction_disconfirmed} — never "confirmed"/"rejected"; evidence_id must cite a real event that postdates the hypothesis
decision_record(action, rationale, evidence_ids)
```

Requirements:

- Every operation writes an event.
- `memory_delete` is a tombstone, not destructive erasure.
- `memory_summary` stores source links and a fidelity report.
- Automatic reducers update memory from file reads, writes, tests, Git, and verifier output.
- The model may request memory operations, but critical state does not depend on those requests.

## SWE-specific state machine

### `orient`

Inputs: issue text, repository snapshot, constraints.

Required state transitions:

- Extract acceptance criteria and explicit non-goals.
- Identify likely repository areas.
- Create initial subgoals.
- Record an observation/reproduction plan.

Exit gate: a concrete inspection or reproduction action exists.

### `reproduce`

Required transitions:

- Run the project’s relevant test or a minimal issue-derived reproducer.
- Record command, environment, exit code, and failure signature.
- Classify environmental failures separately from product failures.

Exit gate: confirmed failure, or documented reason reproduction is unavailable plus a testable behavioral hypothesis.

### `localize`

Required transitions:

- Inspect exact symbols and call paths.
- Record hypotheses with predicted/disconfirming observations.
- Reject alternatives using targeted evidence.
- Maintain a repository map and relevant code snippets.

Exit gate: one or more change contracts linked to observed source locations and a falsifiable prediction. This gate does **not** claim that the diagnosis is correct.

### `patch`

Required transitions:

- Create a checkpoint.
- Define expected files and behavior changes.
- Apply the smallest coherent patch.
- Record diff and affected entities.

Exit gate: nonempty relevant diff and a verification plan.

### `verify`

Required transitions:

- Run minimal reproducer.
- Run targeted tests.
- Run related regression tests.
- Parse failures into structured evidence.
- Re-enter localization or patching when results contradict the hypothesis.

Exit gate: targeted behavior passes, mechanical checks pass, and no critical blocker remains. This is an observed behavioral result, not proof that the model's explanation was correct.

### `review`

Required transitions:

- Inspect final diff.
- Check forbidden-file policy.
- Check acceptance criteria coverage.
- Check for accidental unrelated changes.
- Run external SWE verifier.

Exit gate: external verifier agrees, or the run is explicitly marked externally unverified. The verifier result is authoritative for task success; model confidence and natural-language explanation are not.

### `finish`

`finish_task` becomes a request, not authority. The runtime should reject completion unless mechanical completion gates pass. It must not attempt to prove arbitrary claims such as “the identified function caused the bug.”

## Context compiler policy

At each model call, compile a fresh request. Initial 32K allocation:

| Component | Initial budget |
|---|---:|
| System/tool policy | 3,000 tokens |
| Immutable task contract | 2,000 |
| Structured state | 3,000 |
| Retrieved evidence | 5,000 |
| Recent events | 3,000 |
| Action/verification instructions | 1,000 |
| Response reserve | 6,000 |
| Safety margin | 9,000 |

These are starting values, not fixed truths. Log actual usage and tune through ablations.

The compiler must:

- Keep the original issue byte-identical.
- Prefer exact current-file evidence over stale summaries.
- Include rejected hypotheses when they prevent repeated exploration.
- Include the current next-action contract.
- Include artifact IDs for expandable details.
- Exclude redundant raw tool output.
- Invalidate observations whose source files changed.

## Repository and test reducers

Implement automatic reducers for:

- File reads: path, line ranges, hash, symbols.
- Writes/patches: before/after hash, diff, changed symbols.
- Shell commands: command, exit code, stdout/stderr artifacts.
- Tests: framework, selected tests, pass/fail/error/skip, failure signatures.
- Git: branch, base commit, diff, status, checkpoints.
- SWE verifier: fail-to-pass, pass-to-pass, fail-to-fail, patch application, resolved status.

The reducer should classify test failures into:

1. Product failure.
2. Test/environment failure.
3. Patch application failure.
4. Missing dependency/setup failure.
5. Timeout/resource failure.
6. Unknown failure requiring inspection.

## Trajectory data pipeline

### Sources

Ingest:

- Existing `evolutionEngine` logs.
- Current SWE-bench run logs.
- Agent prompts and responses.
- Tool calls and full tool results.
- Per-turn diffs.
- Test and Docker verifier results.
- Reference patches only into a sealed evaluator/training partition.

Never mix reference patches or hidden tests into the agent’s runtime memory.

### Normalized trajectory format

Each trajectory should contain:

```json
{
  "task_id": "...",
  "run_id": "...",
  "turns": [
    {
      "turn": 1,
      "phase": "orient",
      "state_before": "...",
      "model_output": "...",
      "tool_calls": [],
      "tool_results": [],
      "state_after": "...",
      "diff_after": "...",
      "progress_events": [],
      "tokens": {}
    }
  ],
  "final_patch": "...",
  "external_result": {},
  "failure_taxonomy": []
}
```

### Labels

Derive labels from observable outcomes, not only model prose:

- `new_evidence`.
- `hypothesis_created`.
- `prediction_observed` (the predicted observation occurred; the broader claim remains provisional).
- `prediction_disconfirmed` (the predicted observation did not occur or a falsifier fired).
- `useful_read`.
- `redundant_read`.
- `patch_progress`.
- `test_progress`.
- `environment_failure`.
- `premature_patch`.
- `repeated_reasoning`.
- `successful_recovery`.
- `false_completion`.
- `externally_verified_success`.

For every turn calculate whether the repository, evidence ledger, hypothesis ledger, test ledger, or subgoal graph changed.

### Dataset splits

Create disjoint splits by task and preferably repository:

- `train-memory`: used for memory policy development or training.
- `dev-controller`: used for thresholds and prompt/compiler changes.
- `dev-swe`: used for end-to-end experiments.
- `heldout-repo`: unseen repositories for transfer.
- `sealed-eval`: never used for design tuning.

Do not split random turns from the same task across train and test; that leaks task-specific state.

### Replay and counterfactual evaluation

Build a replay runner that can execute the same trajectory under different context policies:

- Append-all.
- Last-N sliding window.
- Flat summary.
- HiAgent subgoal episodes.
- Structured hierarchy with retrieval.
- AgeMem-style model-selected memory operations.

For deterministic portions, replay the same tool observations. For policy portions, report both offline replay metrics and fresh environment rollouts. Do not claim a replay improvement equals a task-resolution improvement.

## Training strategy

### Stage 1: no learning

Implement automatic reducers, bounded context compilation, explicit subgoals, and deterministic retrieval. This establishes the causal baseline.

### Stage 2: supervised memory behavior

From successful and carefully reviewed trajectories, create examples of:

- What to store.
- What to update.
- What to retrieve.
- What to summarize.
- What to filter.
- When to transition phases.

Train a small policy adapter or memory controller first. Preserve the base Qwen model for comparison.

### Stage 3: memory-aware preference/RL training

Use external SWE outcomes as the primary reward. Add secondary rewards for:

- Useful evidence retention.
- Correct retrieval.
- Reduced redundant context.
- Meaningful patch/test cycles.
- Avoiding false completion.
- Staying within token and tool budgets.

Penalize:

- Context overflow.
- Repeated no-op reasoning.
- Redundant retrieval.
- Unnecessary memory writes.
- Unverified completion.
- Unrelated file changes.

Use step-wise credit assignment, but validate reward attribution carefully. A successful patch does not prove every preceding memory action was useful.

### Stage 4: test-time scaling

Only after a stable single-trajectory agent exists, add:

- Multiple candidate hypotheses.
- Independent patch attempts.
- Verifier-guided selection.
- Low-overthinking trajectory selection.
- Shared read-only evidence and isolated write branches.

Select candidates using external tests and diff review, not model confidence alone.

## Phase plan

### Phase 0 — Baseline freeze and instrumentation

Deliverables:

- Baseline configuration file.
- Complete trajectory recorder.
- Model options logged per run.
- Token, latency, tool, diff, and verifier metrics.
- Five SWE tasks × three seeds.

Acceptance tests:

- A run can be reconstructed turn by turn.
- Every tool result has a complete artifact.
- External resolution is linked to the exact run.
- Baseline metrics are reproducible within expected stochastic variance.

Do not change the agent policy in this phase.

### Phase 1 — Lossless event/artifact store

Deliverables:

- Versioned event schema.
- Append-only JSONL event log.
- Content-addressed artifact store.
- Run metadata and state snapshots.
- Migration/version validation.

Acceptance tests:

- Large output can be retrieved byte-for-byte.
- Process interruption does not corrupt prior events.
- Event IDs and parent relationships are valid.
- Existing `dispatch.py` behavior remains compatible.

### Phase 2 — Automatic materialized state

Deliverables:

- Repository, diff, test, hypothesis, evidence, and progress reducers.
- State snapshot renderer.
- Stale-evidence invalidation.
- Structured failure taxonomy.

Acceptance tests:

- A synthetic trajectory produces the expected state.
- Editing a file invalidates prior file facts.
- A failed test creates a machine-readable failure record.
- State can be rebuilt from events alone.

### Phase 3 — Context compiler and policy baselines

Deliverables:

- Fresh per-turn context compiler.
- Token budget allocator.
- Append-all, sliding-window, flat-summary, and hierarchy policies.
- Context-size metrics.

Acceptance tests:

- Context remains bounded over 200 synthetic turns.
- Task contract is never lost.
- Current test failure and next action survive compaction.
- Every summary can be traced to raw evidence.

### Phase 4 — SWE controller and subgoal episodes

Deliverables:

- Phase state machine.
- Subgoal DAG.
- Evidence-linked hypothesis ledger with explicit untrusted-assertion status.
- Checkpoints and rollback.
- Completion gates.
- Evidence-based stagnation detector.

Acceptance tests:

- A no-op trajectory triggers a useful recovery action.
- Repeated diagnosis without state change does not advance the phase.
- A patch cannot be marked complete without diff and verification checks.
- A failed test returns control to localization or patching.

**Subgoal completion must inherit the "bounded best-effort patch" escape hatch** from the failure-mode table below, not just the evidence gate. A gate a model can never satisfy is a new paralysis trap — the exact failure this project ruled out before ever reaching SWE-bench. `agent.py`'s existing watchdog already has this fallback ("write your best guess anyway" after N turns with no write); the subgoal-completion gate must not override it with a stricter one that has no forced-progress exit.

**Build order** (cheapest/highest-confidence first; each tested against a real run before the next):

1. Phase state machine — a pure reducer over the existing Phase 2 event log (no new tool calls, can't be gamed by prose).
2. `finish_task` completion gate — the generic predicates above. Highest value relative to effort: fixes false completion using only what Phase 2/3 already record.
3. Subgoal tools (`subgoal_create`, `subgoal_complete`) with the weak-but-real evidence gate — the mechanism actually targeting "plan instability."
4. Hypothesis ledger (`hypothesis_record`, resolution against a cited real event) — `prediction_observed`/`prediction_disconfirmed`, never `confirmed`/`rejected`.
5. Cross-subgoal stagnation detector — layers on top of, does not replace, the existing per-turn repetition/confidence watchdog.
6. Checkpoint/restore (stretch) — reuses the `post_content` artifacts Phase 2 already captures on every write, rather than touching Git.

### Phase 5 — Hierarchical retrieval and memory tools

Deliverables:

- Episode summaries with raw links.
- Structured/lexical retrieval.
- `memory_*` tools.
- Retrieval and summary fidelity metrics.
- Optional embeddings behind a feature flag.

Acceptance tests:

- Relevant prior evidence is retrieved under a bounded budget.
- Irrelevant distractors are filtered.
- Exact source expansion works.
- Retrieval does not resurrect stale pre-edit facts.

### Phase 6 — SWE trajectory dataset and replay

Deliverables:

- Ingest existing traces.
- Normalize trajectories.
- Add labels and failure taxonomy.
- Build repository-disjoint splits.
- Build replay and counterfactual context-policy evaluator.

Acceptance tests:

- Known successful and failed runs are classified correctly.
- Reference patches are excluded from runtime context.
- Replay reports differ by policy without modifying source repositories.
- Dataset manifests contain hashes and provenance.

### Phase 7 — End-to-end evaluation and ablations

Deliverables:

- Batch SWE runner.
- Fixed budgets and seeds.
- Statistical report generator.
- Ablation matrix.

Minimum comparison:

```text
A: current append-all agent
B: bounded sliding window
C: hierarchical deterministic memory
D: hierarchy + controller
E: hierarchy + controller + model-selected memory tools
```

Acceptance criteria:

- Report external resolution, tokens, latency, tool calls, and failure modes.
- Report confidence intervals or bootstrap intervals.
- Separate development and sealed evaluation results.
- Do not promote a policy based on one SWE instance.

### Phase 8 — Learned memory policy

Deliverables:

- Supervised memory-operation dataset.
- Small controller or adapter training pipeline.
- Memory-aware reward implementation.
- Step-wise credit assignment experiment.
- Checkpointed model artifacts.

Acceptance tests:

- Learned policy beats the deterministic policy on held-out tasks under equal budget.
- Memory quality does not improve while external resolution regresses.
- Training does not leak task answers or hidden test information.
- Behavior remains safe when memory tools fail.

### Phase 9 — Test-time scaling and multi-trajectory selection

Deliverables:

- Isolated candidate branches.
- Shared read-only evidence store.
- Verifier-guided patch selection.
- Candidate diversity and overthinking metrics.

Acceptance tests:

- A bad candidate cannot contaminate another candidate’s workspace.
- Selection uses executable evidence.
- Extra trajectories improve resolution enough to justify their cost.

## Experiment matrix

Run the following controlled matrix on the same task set:

| Dimension | Values |
|---|---|
| Memory | append-all, window, flat summary, hierarchy, hierarchy+tools |
| Controller | none, watchdog, explicit phases |
| Model mode | current settings, low-temperature, thinking enabled if compatible |
| Context | 8K, 16K, 32K effective budgets |
| Retrieval | lexical, structured, hybrid, hybrid+embedding |
| Training | no-RL, supervised controller, memory-aware RL |
| Seeds | at least 3 per development task |

Track both task-level and turn-level metrics.

## Metrics and promotion gates

### Primary

- Externally verified SWE resolution rate.
- Resolution rate on held-out repositories.
- Resolution rate under equal token/model-call budget.

### Efficiency

- Input and output tokens.
- Peak active context.
- Wall time.
- Tool calls.
- Turns to first useful patch.
- Turns between useful state transitions.

### Memory quality

- Evidence retention fidelity.
- Retrieval precision and recall.
- Stale-fact rate.
- Summary source coverage.
- Redundant memory-operation rate.

### Control quality

- Diagnosis-to-action delay.
- Repeated reasoning turns.
- Premature patch rate.
- Failed recovery rate.
- False completion rate.
- Agent/external-verifier disagreement.

Promotion rules:

1. No runtime policy is promoted from a single task.
2. Any success-rate gain must be reported with token and wall-time cost.
3. No learned policy is promoted if it increases false completion or unsafe edits.
4. Held-out repository performance is required for a general memory claim.

## Failure modes and mitigations

| Failure | Detection | Mitigation |
|---|---|---|
| Summary drops a critical fact | Fidelity checker and later verifier failure | Keep raw links; regenerate or restore episode |
| Retrieval returns stale code | Tree-hash mismatch | Invalidate and re-read |
| Model overuses memory tools | Operation-cost metrics | Penalize redundancy; cap calls |
| Model declares intent but does not act | Tool/state mismatch | Controller advances only on observed effects |
| Forced patch is premature | No evidence-linked change contract or no observable state transition | Require a falsifiable prediction, run an experiment, or make a bounded best-effort patch |
| Environment failure looks like product failure | Error taxonomy | Route to environment-recovery path |
| Cross-task memory leaks task answers | Provenance/scope checks | Quarantine task-specific artifacts |
| RL exploits evaluator | Held-out tests and behavior checks | External verifier diversity and adversarial tests |
| Context compiler hides necessary detail | Expansion requests and replay tests | Preserve artifact IDs and measure recall |
| Larger context worsens attention | Context ablation | Optimize active budget, not nominal maximum |

## Second-pass audit of this plan

Before implementation begins, verify the following dependency order:

1. Existing baseline runs and verifier are reproducible.
2. Full events and artifacts are recorded before any memory policy change.
3. State can be rebuilt from the event log.
4. Context policies can be compared without changing task execution.
5. Controller transitions are driven by state and verifier outputs.
6. Hierarchical summaries are reversible and fidelity-tested.
7. SWE trajectory splits prevent repository/task leakage.
8. Learned policies are compared against deterministic policies under equal budgets.
9. External verification, not `finish_task`, determines success.
10. Test-time scaling is added only after the single-trajectory system is stable.

If any prerequisite fails, stop the phase and fix the infrastructure before tuning prompts or training models.

## First implementation ticket for a coding agent

Implement Phase 0 and Phase 1 only.

The coding agent must:

1. Inspect the existing `agent.py`, `dispatch.py`, SWE runner, verifier, and logs.
2. Preserve the current append-all behavior behind a baseline policy flag.
3. Add versioned run metadata and append-only event recording.
4. Store complete tool results as content-addressed artifacts before truncating previews.
5. Record model options, context estimates, tool calls, diffs, test results, and external verifier results.
6. Add unit tests for event ordering, artifact expansion, interruption recovery, and schema validation.
7. Run the existing tests and one known SWE trajectory end to end.
8. Produce a report showing that the new instrumentation does not change baseline task behavior.

Do not implement RL, embeddings, automatic summarization, or a new controller in the first ticket.

## Completion definition

The project is complete only when:

- The agent can run long SWE tasks with bounded active context.
- Full evidence remains recoverable outside the prompt.
- Subgoals and hypotheses persist across compaction.
- Tool and verifier outcomes automatically update state.
- The controller detects and recovers from stagnation.
- Memory operations are observable and costed.
- Learned policies are evaluated against deterministic baselines.
- Improvements transfer to held-out repositories.
- External SWE verification agrees with the runtime’s completion decision.
