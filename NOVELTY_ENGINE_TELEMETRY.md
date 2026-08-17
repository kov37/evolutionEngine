# NoveltyEngine Telemetry Handoff

```yaml
schema: novelty-engine-telemetry/v1
generated_at: 2026-08-15
repository: /Users/digitialchameleon/noveltyEngine
branch: noveltyEngine
head_commit: 4143813
remote: https://github.com/kov37/evolutionEngine.git
active_agent_runs: 0
active_model_servers: 2
goal: >-
  Build a fast, model-agnostic coding agent that uses deterministic context,
  validation, recovery, and bounded memory to make reliable progress on
  progressively harder tasks.
primary_unresolved_target: SymPy issue 13878

latest_tool_fix:
  tool: find_files
  failure: "Python fnmatch treated **/ as requiring at least one directory"
  observed_effect: "root-level cache.py was reported as missing during lru_cache"
  design_decision: "repair the existing bounded tool; do not add a duplicate discovery tool"
  behavior: "recursive **/ segments now match zero or more directories"
  deterministic_verification: "158 passed, 1 warning in targeted agent-tools and monitor tests"
  real_model_status: "LRU run must be rerun; previous child loaded the pre-fix implementation"
  strict_contract_checkpoint:
    date: "2026-08-16"
    deterministic_suite: "209 passed, 38 subtests passed, 1 warning"
    maintained_command: "./.venv-swebench/bin/python -m pytest -q tests"
    model_facing_editor: "patch_file only"
    host_validation: "Pydantic v2, strict types, extra=forbid"
    hidden_model_tools: ["apply_patch", "list_workspace", "list_dir", "search_file", "grep_dir"]
    real_model_task: "multi_file_transaction"
    actor: "qwen3.8-27b-ctx32 via llama.cpp"
    result:
      artifact_grader: "PASS"
      workflow_scorecard: "FAIL"
      iterations: 10
      iteration_target: 6
      elapsed_seconds: 121.0
      first_tool_seconds: 7.475
      first_mutation_seconds: 41.904
      mutations: 3
      validations: 4
      validation_failures: 3
    comparison:
      prior_patch_file_iterations: 6
      prior_patch_file_elapsed_seconds: 86.2
      conclusion: "Correctness preserved; workflow efficiency regressed in this sample."
    monitor_log: "state/benchmark/agentic/monitor-multi_file_transaction-novelty-1786935713091859000.jsonl"
```

## 2026-08-16 verified runtime checkpoint

- Deterministic suite: `205 passed, 38 subtests passed, 1 warning`.
- Actor backend: llama.cpp, Qwen3.8 27B Q4_K_M GGUF; Ollama stopped for the
  comparison.
- Runtime flags: all GPU layers, Flash Attention, batch/micro-batch 2048,
  Q8 KV cache, mlock, and `draft-mtp`.
- Context-capacity sample: 8K `185.7/16.4`, 16K `185.2/16.4`, 32K
  `185.2/14.7` prefill/decode tokens/sec for the same 3,017-token request.
- Editor A/B on unchanged `multi_file_transaction`:
  - `patch_file`: direct PASS, 6 iterations, 86.2 seconds, first mutation
    41.8 seconds.
  - `apply_patch`: artifact PASS but workflow scorecard FAIL after 12
    iterations; verifier repair passed in 64.1 additional seconds. Total
    177.9 seconds.
- Interpretation: the atomic editor is safe and preserves multi-file state,
  but the first real comparison does not show a convergence advantage. The
  actor repeated a stale patch and then repeated `finish_task`; the host
  correctly refused false completion.
- Replication: an unchanged second `apply_patch` run reproduced the same
  stale-patch/repeated-completion pattern. Artifact passed after verifier
  repair; workflow scorecard failed. Total 147.3 seconds; repair 63.4 seconds.
- Worker status: disabled for this clean editor A/B because the worker
  adapter currently uses Ollama and no Qwen 4B GGUF is available. This is an
  actor/editor result, not a worker-on/off result.

## 2026-08-16 patch contract checkpoint

- `patch_file` schema fields: `path`, `find_exact_block`,
  `replace_with_block`.
- Compatibility: host-side normalization accepts legacy `search`/`replace`
  from old transcripts; those names are not exposed in the new schema.
- Deterministic verification: `207 passed, 38 subtests passed, 1 warning`.
- Contract inventory: `TOOL_CONTRACTS.md`.
- Follow-up candidates: make normal `write_file` new-file-only and correct
  generated optional-path schemas for `git_status` and `git_diff`.

## Latest generic control-plane checkpoint (the engine's traffic-controller rules)

```json
{
  "source": {
    "branch": "noveltyEngine",
    "base_commit": "f13d82a",
    "working_tree_source_changes": [
      "agent.py: require one provider tool when the host progress gate is active",
      "kernel/sandbox.py: accept only /workspace as the virtual active-root alias",
      "tests/test_agent_tools.py: add deterministic coverage for both boundaries"
    ]
  },
  "deterministic": {
    "command": ".venv-swebench/bin/python -m pytest -q tests",
    "passed": 159,
    "subtests": 35,
    "duration_seconds": 1.12
  },
  "real_model": {
    "actor": "Qwen3.8-27B-4bit via the local MLX OpenAI-compatible server",
    "worker": "qwen3.5:4b asynchronous advisory worker",
    "multi_file_attempt": {
      "status": "previous authoritative pass remains valid; a later wrapper-interrupted attempt has no grade",
      "fixture_changed": false,
      "authoritative_passed": true,
      "iterations": 5,
      "mutations": 2,
      "validations": 3,
      "elapsed_seconds": 97.3,
      "worker_calls": 3,
      "stale_judgments": 5,
      "worker_busy_drops": 0
    },
    "cascading_regression": {
      "status": "artifact passed; workflow missed strict 3-iteration target",
      "iterations": 4,
      "mutations": 2,
      "validations": 2,
      "novelty_events": 7,
      "worker_calls": 2,
      "stale_judgments": 4,
      "worker_busy_drops": 1,
      "elapsed_seconds": 66.7,
      "fixture_changed": false
    },
    "authoritative_previous_pass": {
      "passed": true,
      "iterations": 5,
      "mutations": 2,
      "validations": 3,
      "worker_calls": 3,
      "stale_judgments": 5,
      "elapsed_seconds": 95.6
    }
  },
  "design_review": {
    "transaction_buffer": "approve with bounded host-owned file tracking",
    "automatic_broad_git_rollback": "reject; preserve unrelated user changes",
    "4b_failure_compaction": "advisory only with deterministic local fallback",
    "model_specific_logic_added": false
  }
}
```

## Latest recovery-counter change (bounded read means one attempted read)

```json
{
  "source_change": [
    "count a rejected orientation inspection as the one allowed recovery read",
    "require a legal provider tool after orientation recovery closes inspection"
  ],
  "deterministic": "161 passed, 35 subtests passed",
  "real_model_cascading": {
    "artifact_passed": true,
    "workflow_passed": false,
    "iterations": 4,
    "target_iterations": 3,
    "mutations": 2,
    "validations": 2,
    "stale_judgments": 4,
    "worker_busy_drops": 1,
    "elapsed_seconds": 86.8,
    "worker_calls": 2,
    "stale_judgments": 4,
    "interpretation": "correct repair and clean exit, but one extra repair/validation iteration"
  },
  "model_runtime": "MLX actor uses Apple GPU when the local server is alive; this run's server remained alive through the benchmark",
  "tool_contract_retry": {
    "status": "implemented in working tree; one invalid tool call is retried inside the same logical turn",
    "plain_english": "when the model asks for a forbidden tool, the host immediately asks it to choose from the allowed list instead of wasting a full turn",
    "real_model_observation": "read_file was rejected and patch_file followed in the same iteration",
    "fixture_changed": false
  }
}
```

## Claude instruction

Produce a high-density telemetry report from this file. Preserve the distinction
between committed behavior, uncommitted changes, deterministic test evidence,
real-model benchmark evidence, and hypotheses. Do not treat an artifact-only
pass as a workflow pass. Recommend the smallest model-agnostic next change,
then name the exact deterministic and real-model tests that would verify it.

## Repository state

```json
{
  "branch": "noveltyEngine",
  "head": "65bc175",
  "head_message": "Validate completed multi-file transactions immediately",
  "remote_branch": "origin/noveltyEngine",
  "remote_in_sync_at_last_push": true,
  "last_pushed_head": "6543683",
  "worktree": {
  "tracked_modified": [
    "agent.py",
    "agentic_benchmark.py",
    "lifecycle_policy.py",
    "validation_contract.py",
    "NOVELTY_ENGINE_HANDOFF.md",
    "state/benchmark/agentic/results.jsonl",
    "tests/test_adversarial_preflight.py",
    "tests/test_agent_tools.py",
    "tests/test_transaction_buffer.py"
  ],
    "untracked_source": ["transaction_buffer.py"],
    "untracked_monitor_logs": true,
    "tracked_delta": {
      "files": 8,
      "insertions": 418,
      "deletions": 25
    },
    "warning": "The current transaction-window changes are not committed or pushed."
  }
}
```

## Runtime architecture

```json
{
  "actor": {
    "model": "Qwen3.8-27B-4bit",
    "format": "MLX local server",
    "adapter": "llama.cpp/OpenAI-compatible request path",
    "endpoint": "http://127.0.0.1:18080/v1",
    "temperature": 0.2,
    "max_tokens": 512,
    "thinking": false,
    "prompt_cache_size": 1
  },
  "worker": {
    "model": "qwen3.5:4b",
    "role": "bounded advisory triage/critic",
    "transport": "Ollama local service",
    "authority": ["diagnostic suggestion", "staleness signal"],
    "cannot": ["edit product code", "execute tools", "declare completion"],
    "scheduling": "asynchronous; deterministic local policy wins over stale advice"
  },
  "control_layers": [
    "LifecycleFSM",
    "lifecycle_policy validation/action contracts",
    "action_governor capability classification",
    "progress/escalation governors",
    "RiskLayer pre-mutation snapshots and narrow rollback",
    "NoveltyContext bounded event ledger and worker freshness",
    "independent benchmark graders"
  ],
  "context_policy": {
    "provider_context": 16384,
    "raw_tool_fraction": 0.18,
    "raw_tool_char_budget": 11796,
    "pruning": "bounded, recallable raw results plus compact repair checkpoints",
    "completion_authority": "independent validation evidence"
  },
  "transaction_buffer": {
    "authority": "host harness only",
    "tracked_paths": "accepted root-relative product mutations only",
    "followup_turns": 1,
    "expiry": "enter existing recovery without automatic destructive rollback",
    "prompt_pin": "bounded status block added to the next actor request",
    "cleanup": "clear on authoritative validation success"
  }
}
```

## Deterministic evidence

```json
{
  "command": ".venv-swebench/bin/python -m pytest -q tests",
  "result": "157 passed, 35 subtests passed",
  "elapsed_seconds": 1.13,
  "coverage_added_in_current_dirty_delta": [
    "one bounded orientation recovery read",
    "multi-file transaction resilience",
    "answer-free multi_file_transaction benchmark definition",
    "confined cwd error guidance",
    "transaction buffer lifecycle and expiry",
    "test-only source evidence and file-listing validation rejection",
    "focused repair inspection preservation"
  ]
}
```

## Benchmark scorecard

```json
{
  "cascading_loop": {
    "latest_verified_pass": true,
    "iterations": 3,
    "elapsed_seconds": 29.9,
    "mutations": 1,
    "validations": 2,
    "finish_called": true,
    "actor": "Qwen3.8-27B-4bit",
    "worker": "qwen3.5:4b"
  },
  "websocket_chat": {
    "latest_short_run": {
      "passed": false,
      "iterations": 8,
      "mutations": 0,
      "validations": 0,
      "failure": "orientation recovery still timed out before mutation"
    },
    "historical_checkpoint": "independent WebSocket artifact and smoke validation passed in an earlier run"
  },
    "multi_file_transaction": {
    "benchmark": "new, answer-free model-facing task",
    "offline_control_test": "passes",
    "first_real_run": {
      "artifact_passed": true,
      "workflow_passed": true,
      "iterations": 5,
      "mutations": 1,
      "important_caveat": "initial grader allowed File A to retain the old property; benchmark was tightened"
    },
    "strict_real_run": {
      "artifact_passed": true,
      "workflow_passed": false,
      "iterations": 7,
      "iteration_target": 6,
      "mutations": 3,
      "validations": 4,
      "worker_calls": 3,
      "stale_judgments": 7,
      "failure": "actor spent repair turns on protected-test/probe/wrong-path mutations before changing both product files"
    },
    "latest_attempt": {
      "artifact_passed": true,
      "workflow_passed": true,
      "iterations": 5,
      "iteration_target": 6,
      "mutations": 2,
      "validations": 3,
      "worker_calls": 3,
      "stale_judgments": 5,
      "elapsed_seconds": 95.6,
      "chat_timeout_seconds": 60,
      "transaction": "preserved core_math.py after the intermediate failure, then cleared after final pass",
      "monitor_log": "state/benchmark/agentic/monitor-multi_file_transaction-novelty-1786834348369614000.jsonl"
    }
  },
  "sympy_13878": {
    "status": "unresolved",
    "requested_cdf_methods_reached": 0,
    "latest_short_run": {
      "iterations_reached": 7,
      "mutations": 1,
      "validations": 3,
      "worker_calls": 1,
      "result": "stopped after exposing a generic cwd path-escape error"
    },
    "policy": "do not add SymPy- or NumPy-specific logic"
  }
}
```

## Validation and rollback semantics

```json
{
  "failed_validation": {
    "normal_behavior": "enter REPAIR and preserve product edits",
    "automatic_git_checkout": false,
    "automatic_product_rollback": false,
    "next_action": "targeted repair, then validation"
  },
  "RiskLayer_rollback_only": [
    "repair edit to an existing protected supplied test",
    "destructive write_file rewrite that removes most existing implementation lines"
  ],
  "transaction_window_current_change": {
    "intent": "preserve accepted product files through one bounded multi-file repair follow-up",
    "bound": "one follow-up failure; no automatic destructive rollback on expiry",
    "status": "implemented; deterministic suite 157/35; strict real-model workflow passed in 5 iterations"
  }
}
```

## 4B worker telemetry

```json
{
  "assessment": "under-utilized for orientation and often stale during repair",
  "cascading_latest": {
    "worker_calls": 1,
    "judgments": 2,
    "stale_judgments": 3,
    "worker_busy_drops": 0
  },
  "multi_file_latest": {
    "worker_calls": 2,
    "judgments": 4,
    "stale_judgments": 7,
    "coalesced_events": 0
  },
  "interpretation": "The 4B is useful as a secondary signal, but deterministic FSM/policy is currently doing the meaningful steering. It is not yet a reliable synchronous transaction planner."
}
```

## Repository delta requiring review

```json
{
  "agent.py": [
    "allows one bounded focused read during orientation recovery",
    "defers proactive validation while a finite multi-file repair batch is open",
    "keeps proactive validation enabled for ordinary single-file repair",
    "preserves focused repair inspection evidence",
    "integrates host-controlled transaction lifecycle and metrics"
  ],
  "lifecycle_policy.py": [
    "adds bounded focused recovery-read tool surface"
  ],
  "validation_contract.py": [
    "does not treat test-only traceback locations as product source",
    "rejects successful file-listing output as behavioral evidence"
  ],
  "tests/test_adversarial_preflight.py": [
    "adds explicit two-file transaction resilience test"
  ],
  "tests/test_agent_tools.py": [
    "updates orientation recovery expectations for one bounded read",
    "covers test-only source locations, repair evidence preservation, file-listing rejection, and transaction deferral"
  ],
  "transaction_buffer.py": [
    "host-only bounded product-file set, status block, expiry, and cleanup"
  ],
  "agentic_benchmark.py": [
    "adds answer-free multi_file_transaction task",
    "strict grader requires old property removal and new property use in File B"
  ],
  "state/benchmark/agentic/results.jsonl": [
    "contains real-model benchmark records; do not confuse monitor logs with source changes"
  ]
}
```

## Recommended next actions

1. Review the dirty transaction-buffer diff and commit it as a checkpoint;
   do not alter the six-iteration score merely because the artifact passed.
2. Run the deterministic suite and the clean multi-file benchmark again after
   the checkpoint commit.
3. Measure whether the seven-turn path is a real avoidable overhead or the
   natural cost of inspect -> mutate A -> validate -> mutate B -> validate.
4. If optimizing, change only a generic orchestration boundary and require
   both the cascading and multi-file tests to pass; never inject the expected
   file patches.
5. Return to SymPy only after this generic transaction benchmark is stable.

## Suggested Claude analysis prompt

```text
Analyze NOVELTY_ENGINE_TELEMETRY.md as an agent-architecture incident report.
Separate proven facts from hypotheses. Explain whether the current failed
multi-file run is caused primarily by transaction semantics, repair-tool
selection, stale 4B advice, or the actor model. Propose one minimal generic
change. Do not modify benchmark fixtures to make the score pass. Specify the
deterministic test and one real-model run required to validate the proposal.
```

## 2026-08-15 validation-gap review checkpoint

```yaml
deterministic_tests: "161 passed, 35 subtests passed"
latest_sympy_run: sympy-13878-novelty-1786844158
latest_sympy_result:
  grader_passed: false
  actor_returncode: 0
  mutations: 3
  validations: 5
  iterations: 8
  first_behavioral_traceback_reached_actor: true
  failure: "ArcsinDistribution is missing _cdf"
  unrelated_mutation_observed: "sympy/polys/agca/modules.py"
  interpretation: "validation evidence now arrives; failure attribution remains weak"
pipeline_changes:
  - "run one nearby conventional Python test target after a product mutation"
  - "preserve pytest failure head and tail instead of only the tail"
  - "classify executed pytest failures as behavior, not setup"
recommended_architecture:
  next: "evidence bundle plus failure provenance"
  later: "host-owned reproducer test fallback"
  benchmark_only: "sanitized shadow-grader feedback"
```

## Verified generic checkpoint after provenance implementation

```yaml
deterministic_tests: "165 passed, 35 subtests passed"
cascading_loop:
  passed: true
  iterations: 3
  iteration_target: 3
  mutations: 2
  validations: 3
  worker_busy_drops: 0
multi_file_transaction:
  passed: true
  iterations: 5
  iteration_target: 6
  mutations: 3
  validations: 4
  transaction_preserved_intermediate_edit: true
  transaction_cleared_after_success: true
validation_provenance:
  records: [tool, command, cwd, plane, failed_tests, source_paths, test_paths,
            changed_paths, changed_path_overlap, diagnostic]
  test_context: "bounded, explicitly read-only evidence"
  duplicate_auto_validation: "at most one automatic test target per mutation turn"
  setup_label_regression: "fixed; test-only context no longer contains setup marker wording"
next_hard_diagnostic: "SymPy issue 13878"
```

## Latest hard-task review

```yaml
sympy_run: sympy-13878-novelty-1786845580
actor_returncode: 0
grader_passed: false
grader_timed_out: true
grader_timeout_seconds: 120
mutations: 2
validations: 2
first_validation_reached_actor: true
rejected_patch_recovery: "missing; actor repeated patch searches without a fresh read"
transaction_expired: true
interpretation: >-
  Validation evidence is reaching the actor. The remaining generic gap is
  converting a rejected mutation into a bounded inspect-then-repair or
  recover action.
review_plans:
  recommended: evidence_provenance_plus_rejected_mutation_recovery
  second: host_owned_reproducer_gate
  benchmark_only: sanitized_shadow_grader
next_cycle:
  - deterministic rejected-patch recovery tests
  - unchanged cascading regression
  - unchanged multi-file transaction regression
  - bounded SymPy rerun
```

## Rejected mutation recovery checkpoint

```yaml
head_commit: ffb8ebb
deterministic_tests: "172 passed, 35 subtests passed"
rejected_mutation_contract:
  trigger: "product mutation returns ERROR or REJECTED"
  next_turn_tools: [read_file, search_file, list_symbols, grep_dir]
  read_budget: 1 attempted focused inspection
  following_turn: "fresh product mutation or recovery"
  broad_browse: false
  validation_during_inspection: false
frozen_regressions:
  cascading_loop:
    passed: true
    iterations: 3
    mutations: 2
    validations: 3
  multi_file_transaction:
    passed: true
    iterations: 5
    mutations: 2
    validations: 3
sympy_replay:
  run_status: incomplete_manual_stop
  reached_iteration: 6
  rejected_patch_observed: true
  recovery_state_entered: true
  capability_result: "not measured; provider timeout had not completed"
next_action: "full-duration unchanged SymPy replay"
```

## 2026-08-16 — rejected mutation replay guard

```yaml
source_change: >-
  Record exact rejected product mutation signatures during a run and reject
  an identical replay before dispatch; helper writes below .agentic/ remain
  exempt.
deterministic_tests: "175 passed, 35 subtests passed"
real_model_cascading:
  status: incomplete_stopped_before_final_score
  reached_iteration: 3
  mutations: 1
  validations: 1
  reason: "grader review took priority; no pass claimed"
sympy_replay:
  status: incomplete_stopped_after_repeated_stale_patch
  reached_iteration: 8
  mutations: 2
  validations: 2
  repeated_rejected_patch: true
  interpretation: >-
    The host detected the stale patch and supplied recovery evidence, but the
    actor replayed the same mutation. This motivated the generic replay guard.
model_specific_logic_added: false
next_verification:
  - rerun cascading to completion
  - rerun multi-file transaction to completion
  - rerun bounded SymPy unchanged
  - review validation_pipeline_spec.md when supplied
```

## 2026-08-16 — independent grader isolation

```yaml
source_change: >-
  Run acceptance checker source from a host-owned temporary directory rather
  than writing .agentic_grader.py into the actor workspace.
deterministic_tests: "178 passed, 35 subtests passed"
grader_contract:
  statuses: [PASS, FAIL, TIMEOUT, ENVIRONMENT_INVALID]
  checker_source_in_actor_workspace: false
  checker_hash_recorded: true
  bounded_detail_chars: 4000
  optional_preflight: supported
real_model_regressions:
  cascading_loop:
    passed: true
    grader_status: PASS
    iterations: 3
    iteration_target: 3
    mutations: 2
    validations: 3
  multi_file_transaction:
    passed: true
    grader_status: PASS
    iterations: 5
    iteration_target: 6
    mutations: 2
    validations: 3
remaining_grader_work:
  - wire immutable baseline checks into task contracts
  - add fail_to_pass and pass_to_pass evidence
  - add hidden/shadow acceptance checks outside actor visibility
  - add supplied-test integrity and diff checks
model_specific_logic_added: false
source_checkpoint: "78c757c"
```

## 2026-08-16 — supplied-test integrity guard

```yaml
source_change: >-
  Snapshot supplied test hashes before the actor run and reject any deleted or
  changed supplied test as UNSAFE_WORKSPACE_CHANGE before acceptance grading.
deterministic_tests: "179 passed, 35 subtests passed"
integrity_contract:
  protected_paths: "supplied tests only"
  new_actor_tests_allowed: true
  verifier_repair_after_integrity_failure: false
real_model_regressions:
  cascading_loop:
    passed: true
    grader_status: PASS
    iterations: 3
    iteration_target: 3
    mutations: 2
    validations: 3
    test_integrity: true
  multi_file_transaction:
    passed: true
    grader_status: PASS
    iterations: 5
    iteration_target: 6
    mutations: 3
    validations: 4
    test_integrity: true
remaining_grader_work:
  - baseline and fail_to_pass evidence
  - pass_to_pass regression evidence
  - hidden/shadow acceptance checks outside actor visibility
source_checkpoint: "820b4bf"
```

## 2026-08-15 — validation packet, preconditions, and advisory worker

```yaml
source_change:
  - host-owned bounded first-failure validation packet
  - fail_to_pass baseline evidence and optional pass_to_pass evidence
  - 4B Ollama JSON Schema with deterministic sampling settings
  - 4B suggestions no longer alter host tool legality or FSM state
deterministic_tests: "185 passed, 37 subtests passed"
worker_request:
  guided_json_schema: true
  think: false
  temperature: 0
  seed: 0
  repeat_penalty: 1.0
  live_schema_call: PASS
grader_evidence:
  baseline_statuses: "FAIL for cascading_loop and multi_file_transaction"
  baseline_failure_types_are_checked: true
  timeout_or_invalid_environment_counts_as_valid_baseline: false
  checker_outside_actor_workspace: true
  supplied_test_integrity: true
real_model_regressions_before_latest_worker_change:
  cascading_loop:
    passed: true
    iterations: 3
    grader_status: PASS
    fail_to_pass: true
  multi_file_transaction:
    passed: true
    iterations: 5
    grader_status: PASS
    fail_to_pass: true
  websocket_chat:
    independent_grader_status: PASS
    actor_finish_called: false
    handoff_reconciled: true
    interpretation: >-
      A bounded verifier repair completed the artifact, but the actor's setup
      recovery and 4B gating behavior remained inefficient. Rerun required
      after making the worker advisory-only.
next_grader_work:
  - hidden/shadow acceptance checks outside actor visibility
  - mutation-guided checker strength tests
  - advice_followed and stale_advice telemetry
model_specific_logic_added: false
source_checkpoint: pending
```

## 2026-08-15 — pause checkpoint and worker ablation

```yaml
implementation_loop: paused_by_user
benchmark_child_running: false
model_servers: left_running
worker_authority:
  tool_restrictions_from_4b_applied: false
  fsm_transitions_from_4b: false
  context_advice_visible_to_actor: true
  host_dispatch_authority: true
worker_generation:
  guided_json_schema: true
  live_ollama_schema_call: PASS
  think: false
  temperature: 0
  seed: 0
  repeat_penalty: 1.0
advice_telemetry:
  fields:
    - advice_issued
    - advice_followed
    - advice_successful
    - advice_failed
    - advice_regression_signals
  interpretation: >-
    Diagnostic only. A net-positive claim requires a paired run with the 4B
    disabled; advice counters cannot prove causality by themselves.
paired_cascading_ablation:
  novelty:
    passed: true
    elapsed_seconds: 64.7
    iterations: 3
    mutations: 2
    advice_issued: 2
    advice_followed: 1
    advice_successful: 1
    advice_failed: 0
    advice_regression_signals: 0
  baseline_without_4b:
    passed: true
    elapsed_seconds: 49.0
    iterations: 3
    mutations: 2
  result: >-
    Same outcome and iteration count; novelty was approximately 15.7 seconds
    slower in this single pair. This motivates deferred triage for simple
    first failures, but is not a statistically strong universal result.
latest_confirmed_deterministic_tests: "187 passed, 37 subtests, 1 warning"
latest_confirmed_real_runs:
  cascading_novelty:
    grader: PASS
    fail_to_pass: true
    iterations: 3
  websocket_after_advisory_only:
    grader: PASS
    actor_finish_called: false
    handoff_reconciled: true
    verifier_repair: true
verification_pending_after_pause:
  - rerun corrected triage-deferral test
  - rerun cascading and multi-file after final optimization
  - paired WebSocket ablation
  - hidden/shadow grader
  - mutation-guided grader-strength tests
reproducibility:
  implementation_loop: "stopped_for_documentation"
  documentation_only_after_pause: true
  benchmark_child_running: false
  model_servers: "left running; no benchmark process should be assumed active"
  repository_root: "/Users/digitialchameleon/noveltyEngine"
  branch: "noveltyEngine"
  benchmark_definition: "agentic_benchmark.py"
  independent_grader: "independent_grader.py"
  actor: "agent.py"
  worker: "novelty_context.py"
  lifecycle_policy: "lifecycle_policy.py"
  validation_packet: "validation_packet.py"
  deterministic_tests: "tests/"
  result_history: "state/benchmark/agentic/results.jsonl"
  monitor_history: "state/benchmark/agentic/monitor-<task>-<condition>-*.jsonl"
  handoff: "NOVELTY_ENGINE_HANDOFF.md"
  research: "VALIDATION_GRADER_RESEARCH.md"
  authority_order:
    - "host evidence and dispatch"
    - "deterministic FSM/lifecycle policy"
    - "independent grader"
    - "actor proposal"
    - "4B suggestion"
  benchmark_tasks:
    - cascading_loop
    - multi_file_transaction
    - websocket_chat
    - 3d_scene
    - real_app
    - wifi_simulator
  method:
    - "state one generic failure mode"
    - "change one model-agnostic host mechanism"
    - "add deterministic regression coverage"
    - "run preflight before model calls"
    - "run frozen task and paired component-disabled baseline"
    - "compare useful progress per second and recovery cost"
    - "repeat on another task shape"
    - "document verified and pending evidence"
    - "commit only coherent source/docs"
  phases:
    0: "resume safely and verify the pending deterministic test"
    1: "rerun frozen tasks and paired WebSocket ablation"
    2: "add hidden/shadow, pass-to-pass, and mutation-strength grader checks"
    3: "make multi-file transactions explicit and bounded"
    4: "measure percentage-based context pruning/compaction"
    5: "ablate worker-on/off/delayed while keeping 4B advisory-only"
    6: "run unchanged SymPy 13878 after short regressions are green"
  pending_verification:
    - "corrected triage-deferral unit test"
    - "cascading and multi-file after final triage deferral"
    - "paired WebSocket worker ablation"
    - "hidden/shadow grader"
    - "mutation-guided grader-strength tests"
```

## 2026-08-16 additional real-model checks

```yaml
additional_runs:
  runtime:
    actor: "Qwen3.8 27B Q4_K_M through llama.cpp"
    context: "32K, MTP, Flash Attention, Q8 KV, batch 2048"
    ollama: stopped
  cascading_loop:
    artifact_grader: PASS
    workflow: PASS
    iterations: 3
    elapsed_seconds: 49.8
    mutations: 2
    validations: 3
    monitor: "state/benchmark/agentic/monitor-cascading_loop-novelty-1786935962311775000.jsonl"
  websocket_chat:
    artifact_grader: PASS
    workflow: "PASS after bounded verifier repair"
    primary_iterations: 12
    repair_iterations: 5
    total_iterations: 17
    elapsed_seconds: 452.2
    primary_first_mutation_seconds: 113.499
    monitor_primary: "state/benchmark/agentic/monitor-websocket_chat-novelty-1786936025475669000.jsonl"
    monitor_repair: "state/benchmark/agentic/monitor-websocket_chat-novelty-1786936344702478000-repair.jsonl"
  conclusion: >-
    Strict contracts preserved correctness but added convergence cost when the
    actor selected write_file for an existing file. Improve deterministic
    rejected-mutation recovery; do not weaken the safety boundary.
```
