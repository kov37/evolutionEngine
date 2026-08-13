# Model-Agnostic Context Design

The context manager must improve coding agents as a class, not memorize the
behavior of Qwen or SymPy.

## Boundaries

The manager communicates through a provider-neutral chat adapter. It consumes
plain text tool events and emits a small validated judgment schema. No control
path branches on model name, tokenizer, vendor, or hidden reasoning format.
Ollama is only the current deployment adapter.

Worker settings are explicit configuration: model tag, context budget,
interval, and output limit. A test can inject any callable with the same chat
contract, which is how deterministic tests avoid depending on a specific
model.

## Anti-overfitting rules

- Do not encode Qwen-specific phrases, token patterns, or tool-call quirks.
- Keep local heuristics authoritative for fingerprints, mutation counts,
  failures, and context pressure.
- Treat worker judgments as advisory and validate their enum/JSON shape.
- Fall back deterministically when a provider returns malformed output,
  refuses structured output, or is unavailable.
- Bound worker latency and calls; never make task completion depend on a
  worker response.
- Do not let the worker edit files, run commands, or declare success.
- Evaluate policies across multiple primary/worker model pairs and at least
  one injected fake adapter before promotion.

## Transfer matrix

The first transfer matrix should include:

| Primary | Worker |
|---|---|
| qwen3.6:35b-mlx | qwen3.5:4b |
| qwen3.6:35b-mlx | qwen3.5:9b |
| qwen3.6:27b | qwen3.5:4b |
| qwen3.6:35b-mlx | llama3.1:8b |

The matrix is not a leaderboard. A context policy is promoted only when it
does not regress the no-worker baseline across representative models and
improves at least one hard-task progress or recovery metric. If a model is
unavailable, that cell is recorded as unavailable rather than silently
replaced.

## Task transfer

Use both SymPy #13878 and synthetic fixtures with different shapes: a single
file edit, a multi-file change, a failing-test repair, and a research/tool-use
task. A policy that only improves the repeated-distribution task is treated as
task overfitting.

