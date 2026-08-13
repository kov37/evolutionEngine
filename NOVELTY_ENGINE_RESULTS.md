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
