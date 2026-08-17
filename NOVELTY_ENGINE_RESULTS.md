# Novelty Engine Iteration Results

## First real comparison

Task: local five-function docstring task, fresh temporary workspace per run.
Primary model: `qwen3.6:35b-mlx`. Context worker: `qwen3.5:4b`.

| Mode | Result | Iterations | Tool calls | Duplicate calls | Time | Worker calls |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | PASS | 9 | 15 | 3 | 112s | 0 |
| Async thread, every event | PASS | 13 | 15 | 2 | 161s | 13 |
| Async thread, batched/triggered | PASS | 9 | 15 | 2 | 207s | 5 |
| Killable process, batched/triggered | PASS | 8 | 14 | 2 | 175s | 3 |

## Findings

1. The 4B worker produces grounded structured judgments and had zero failures
   in the real runs.
2. Calling the worker for every event is too expensive on this hardware.
3. Batching reduces worker calls but does not eliminate model contention.
4. A killable process removes the interpreter-shutdown tail and prevents stale
   advisory work from blocking completion, but the integrated run is still
   slower than baseline because concurrent Ollama inference competes for
   resources.
5. The worker currently provides context advice but does not yet improve task
   outcome on this easy task; this is expected and is not evidence of benefit.

## Current operating policy

The default novelty path is opportunistic: the 4B worker is invoked after
context pressure, failed validation, repeated actions, or a meaningful event,
not on every routine read. The baseline loop remains available for comparison.

## Action-critic experiment

The next controlled experiment is enabled with `--novelty-action-critic`. The
4B worker still cannot execute tools; it may only add one bounded advisory
directive when its judgment is high-confidence or detects stagnation. The
directive contains a recommended action, blocker, and target. This separates
"worker provides context" from "worker improves the next action" and makes the
effect measurable in paired baseline/novelty runs.

Required comparison metrics are verified completion, first successful
mutation, validation-after-mutation, recovery after an injected tool error,
redundant action ratio, blocked-necessary-action rate, and worker latency.
The hard action gate is retained as an opt-in ablation because the live repair
experiment showed that it can prevent a necessary targeted reread.

## First verified SymPy #13878 hybrid repair

The baseline 35B run produced a near-complete candidate with 17 CDF methods
and one remaining failure: `asin` was used by `ArcsinDistribution._cdf` but
was not imported. A repair run used `qwen3.6:35b-mlx` as the coding model and
`qwen3.5:4b` as the asynchronous novelty worker.

| Run | Mutations | Validations | 4B worker calls | Independent grade |
|---|---:|---:|---:|---:|
| Hybrid repair | 1 | 1 | 1 | **20/20 passed** |

The clean grader applied the withheld official test patch to a fresh copy and
ran `test_arcsin` plus 19 PASS_TO_PASS regression tests. The result was
`20 passed, 43 deselected`; the 35B added the missing `asin` import.

The action-gate ablation was also tested. It reduced open-ended exploration,
but was too restrictive for a repair that still needed a targeted reread, so
it remains opt-in via `--novelty-action-gate`.

## Atomic editor A/B (2026-08-16)

The frozen `multi_file_transaction` benchmark was run twice with the same
Qwen3.8 27B llama.cpp actor and identical budgets. Only the mutation editor
changed. The actor used the local Qwen3.8-27B Q4_K_M GGUF with MTP, Flash
Attention, 32K context, Q8 KV cache, 2048 batch/micro-batch, and mlock.

| Editor | Artifact grader | Workflow scorecard | Direct run | Recovery |
|---|---:|---:|---:|---:|
| `patch_file` | PASS | PASS | 6 iterations / 86.2s | none |
| `apply_patch` | PASS | FAIL | 12 iterations / 113.5s | verifier repair passed in 64.1s |

`apply_patch` made a valid atomic first edit, but the actor then produced a
stale patch and repeatedly called `finish_task` while the second file was
still wrong. The host rejected those completion attempts and the bounded
verifier repair later fixed the remaining file. This is evidence that the new
primitive protects workspace integrity; it is not yet evidence that it makes
the actor converge faster. The benchmark fixture was unchanged.

## Runtime configuration A/B notes (2026-08-16)

MTP was confirmed by llama.cpp logs (`draft_n` and `draft_n_accepted`). The
tuned flags averaged about 181.4 prefill tok/s and 15.6 decode tok/s in two
samples, versus roughly 180–186 prefill and 16–18 decode with only MTP plus
Flash Attention. A context-capacity check using a 3,017-token request found
8K: 185.7/16.4, 16K: 185.2/16.4, and 32K: 185.2/14.7 prefill/decode tok/s.
These are operational measurements, not claims of universal superiority.

## Atomic editor A/B replication

The `apply_patch` arm was repeated with no source, fixture, model, or runtime
changes. It reproduced the first result: one correct atomic edit, a stale
second patch, repeated `finish_task` calls, and host-enforced recovery. The
artifact passed after verifier repair, but the primary workflow scorecard
failed again. The second run took 147.3 seconds total, including 63.4 seconds
for verifier repair. This is strong evidence of a repeatable actor/tool
interaction problem rather than random noise.
