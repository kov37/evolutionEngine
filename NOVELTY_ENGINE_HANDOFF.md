# Novelty Engine Handoff

Updated: 2026-08-13

## Current objective

Improve general agent progress through model-independent context, validation,
and recovery mechanisms. The primary actor is the larger local model; the 4B
worker remains advisory and cannot edit, execute, or declare success.

## Current failure-to-repair design

The agent now uses an explicit state machine:

```text
mutation -> validation -> pass / repair -> validation
```

After a failed behavioral check, `agent.py` enters `repair_required` mode. It
temporarily offers focused inspection and mutation tools, removes validation
and finish tools, and requires a successful mutation before revalidation.

`validation_contract.py` creates a task-derived contract by extracting
acceptance clauses, interfaces, and typed fields from the user task. It does
not branch on model, provider, benchmark name, or expected source code.

## Structured repair packet

Each failed validation now creates a packet containing:

- the exact failed probe;
- expected interface and response evidence;
- observed failure output;
- the next repair focus;
- a constraint to make one concrete mutation before rerunning the check.

This is task-grounded feedback, not model-specific implementation. For
example, a task mentioning `/api/tasks` causes the packet to identify that
interface; it does not prescribe a Todo-specific implementation.

## Verification status

Deterministic checks currently pass:

```text
python3 -m py_compile agent.py validation_contract.py action_governor.py
python3 validation_contract.py
python3 task_contract.py
python3 action_governor.py
git diff --check
```

The latest real-app benchmark used llama.cpp, the Devstral Q4 actor, the 4B
novelty worker, action critic, action gate, and a 24-iteration budget. The
previous completed 24-iteration run made 11 mutations and 10 validations, but
still failed the independent Todo grader because the API response shape did
not converge. A later rerun became stale after more than 21 minutes without a
completion record and was stopped; it is not treated as a model result.

The controlled `bug_repair` eval subsequently passed with the structured
repair packet enabled:

```text
task: bug_repair
iterations: 18
mutations: 4
validations: 4
worker_calls: 4
worker_failures: 0
independent grader: PASS
```

This confirms the failure-to-repair state transition works on a seeded defect.
The remaining Todo failure is a harder multi-interface convergence problem,
not evidence that repair mode is completely ineffective.

## Next handoff actions

1. Read the newest `state/benchmark/agentic/results.jsonl` entry.
2. If the grader still fails, inspect the latest repair packet and generated
   artifact before changing policy.
3. Compare mutation count, validation count, repair transitions, duplicate
   checks, and independent pass/fail—not just model completion.
4. Update this document after every code or benchmark-policy change.

## Important files

- `agent.py`: actor loop and validation/repair state machine.
- `validation_contract.py`: task-derived acceptance and repair packets.
- `action_governor.py`: capability classification and evidence bookkeeping.
- `novelty_context.py`: asynchronous 4B context worker.
- `agentic_benchmark.py`: independent agentic graders.
- `state/benchmark/agentic/results.jsonl`: benchmark history.
