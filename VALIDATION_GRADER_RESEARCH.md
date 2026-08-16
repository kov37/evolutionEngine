# Validation grader research

This is design guidance, not an authoritative implementation specification.
The recurring lesson is simple: a grader must independently measure behavior,
protect the evaluation boundary, and report why a run failed. A model's claim
that it is finished is only a trace event.

## Findings

1. Test passing is necessary but not sufficient. An empirical SWE-bench study
   found plausible patches that passed the benchmark while failing developer
   tests, and found substantial behavioral differences from the reference
   patch. This supports independent acceptance checks, differential checks,
   and a distinction between `tests_passed` and `repair_supported`.

   Source: [Are "Solved Issues" in SWE-bench Really Solved Correctly?](https://arxiv.org/abs/2503.15223)

2. The task and test must agree. OpenAI's audit of SWE-bench Verified found
   narrow tests (rejecting valid implementations) and wide tests (requiring
   unspecified behavior). A grader should test the stated observable contract,
   not a particular implementation or hidden naming convention.

   Source: [Why SWE-bench Verified no longer measures frontier coding capabilities](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)

3. Regression suites need strength testing. Mutation-guided test augmentation
   creates altered implementations and keeps tests that reject incorrect
   variants while still accepting the intended behavior. This is a strong
   future direction for NoveltyEngine's hidden/shadow grader layer.

   Source: [STING: Mutation-Guided Diagnosis and Augmentation of Regression Suites](https://arxiv.org/abs/2604.01518)

4. Agent evaluation is multi-step. AgentBench evaluates success across
   different interactive environments, and its analysis identifies long-term
   reasoning, decision-making, and instruction-following as common failure
   modes. A single final answer score cannot explain a context manager's
   usefulness.

   Source: [AgentBench](https://arxiv.org/abs/2308.03688)

5. The trajectory is evidence for diagnosis, not correctness. SWE-agent keeps
   a separate trajectory record and performs benchmark evaluation separately.
   NoveltyEngine should preserve raw host telemetry while sending the actor a
   bounded packet.

   Source: [SWE-agent trajectory documentation](https://swe-agent.com/latest/usage/trajectories/)

## Recommended grader layers

| Layer | Plain meaning | Current state |
| --- | --- | --- |
| Environment precondition | Prove the starting task is runnable and genuinely broken | Implemented for frozen benchmark tasks |
| Fail-to-pass | The required failure exists before the run and disappears after repair | Implemented and recorded |
| Pass-to-pass | Unrelated behavior still works | Framework implemented; used by feature/data tasks |
| Independent acceptance | A host-owned checker runs outside the actor workspace | Implemented with status, timeout, hash, and bounded detail |
| Test integrity | The actor did not edit supplied tests | Implemented |
| Hidden/shadow acceptance | Additional checks the actor cannot inspect or weaken | Next major addition |
| Mutation strength | Deliberately weakens candidate behavior to test whether checks catch it | Planned after shadow checks |
| Trajectory telemetry | Explain time, tool use, retries, unsafe edits, and repair convergence | Partially implemented |

## Metrics that matter for a small coding model

- `task_success_rate`: independently graded behavioral completion.
- `valid_start_rate`: how often the benchmark setup passes its own precondition.
- `fail_to_pass_rate`: how often a genuine initial failure becomes a pass.
- `pass_to_pass_rate`: how often unrelated behavior survives.
- `unsafe_change_rate`: supplied-test edits, grader edits, or forbidden paths.
- `repair_success_rate`: failed validation followed by a correct repair.
- `first_mutation_seconds` and `first_validation_seconds`: convergence speed.
- `iterations_to_success` and `tool_calls_to_success`: efficiency.
- `stale_advice_rate` and `advice_followed_rate`: whether the 4B suggestion
  arrives late or is useful. These are diagnostic only; they never determine
  correctness.
- `false_positive_rate`: the grader says pass while a shadow or differential
  check says the behavior is wrong.

## NoveltyEngine decision

Do not replace deterministic host checks with an LLM judge. Keep the 35B
actor responsible for implementation, keep the host responsible for state,
tools, and acceptance, and use the 4B only for bounded, schema-valid advice.
The next highest-value grader feature is a hidden/shadow checker that is
created outside the actor workspace and runs after visible acceptance. After
that, add mutation-guided checker-strength tests.
