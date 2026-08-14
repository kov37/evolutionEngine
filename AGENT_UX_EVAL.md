# Agent UX Evaluation Protocol

This is an evaluation protocol, not agent implementation logic. The Todo app
is one fixed external fixture used to measure a model-agnostic coding agent.
The engine must not gain Todo-specific branches, filenames, schemas, or repair
instructions because of this test.

## What to measure

For each fresh task workspace, record:

- independent correctness grader result;
- total elapsed time and watchdog timeout;
- `first_tool_s`;
- `first_mutation_s`;
- `first_validation_s`;
- iterations and tool calls;
- mutation and validation counts;
- validation failures and repair-cycle metrics;
- duplicate actions;
- worker latency, stale judgments, coalesced events, and failures.

The benchmark streams every actor line immediately and writes a durable
timestamped event log under `state/benchmark/agentic/monitor-*.jsonl`. Each
event has elapsed time, category, and raw text. Use that log to diagnose
latency stalls, repeated reads, repair loops, and worker lag; do not wait for
the final result JSON to understand a live run.

The terminal view is intentionally compact: it prints event categories, tool
names, paths, and short failure summaries while omitting large file contents
and full command arguments. The monitor JSONL remains the complete forensic
record, so live visibility does not consume the conversation context.

The benchmark launches the actor with Python unbuffered (`-u`) output. This is
required for the stream to be genuinely live rather than delayed until the
agent process exits.

The timing markers are generic agent events. They do not assume a web app,
Python, Todo objects, or any particular model/provider.

## Current fixture matrix

Use the Todo app in `agentic_benchmark.py` as the current end-to-end fixture,
then transfer the same measurement protocol to `bug_repair`, `feature`,
`data_report`, `recovery`, SymPy #13878, and future non-web tasks.

Compare fresh workspaces under:

```text
baseline: no novelty worker
novelty: 4B worker with critic/gate enabled
```

Keep model, backend, prompt, iteration budget, and watchdog fixed within a
comparison. The preferred current backend is llama.cpp with Devstral Q4.

## Promotion rule

Promote a context-policy change only when it improves verified completion,
repair convergence, or responsiveness across multiple task shapes. A Todo
pass alone is not sufficient evidence. A faster run that lowers correctness
is a regression; a correct run that takes unbounded time is also a regression.

## Cycle

1. Run deterministic tests.
2. Run paired baseline/novelty `bug_repair` as the cheap recovery gate.
3. Run paired baseline/novelty Todo as the end-to-end fixture.
4. Inspect independent grader results and all timing/repair metrics.
5. Transfer promising policies to another task shape.
6. Update `NOVELTY_ENGINE_HANDOFF.md` after every code or evaluation-policy
   change.
