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

## Next experiment

Test an opportunistic policy that runs the 4B model only after context pressure,
failed validation, repeated actions, or a meaningful mutation—not on routine
reads. Compare it on the harder SymPy #13878 task, where retrieval and recovery
may have measurable value. Keep the baseline as the default until a repeated
verified improvement appears.

