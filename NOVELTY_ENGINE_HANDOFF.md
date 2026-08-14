# Novelty Engine Handoff

Updated: 2026-08-14

## Latest test-cycle note

The latest real-app novelty run reached internal completion after one repair
cycle, but the independent grader failed its homepage assertion. The grader
now includes the first 500 response characters in that failure, so the next
run can distinguish bad HTML from a startup or routing failure. Treat the
run as failed until the independent grader passes.

The benchmark also supports `--keep-workspace`, which preserves the generated
temporary project for manual inspection after a run. Use it for demonstrations
or debugging; ordinary evals should leave cleanup enabled.

The first Qwen3.6 MTP Todo run exposed a provider-boundary issue: llama.cpp
expects `tool_choice` as the string `"required"`, while the actor adapter was
sending the structured OpenAI function-choice object. The adapter now performs
that translation at the llama.cpp boundary. Re-run deterministic checks, then
repeat the Qwen hybrid Todo benchmark; do not interpret the pre-fix run as a
model capability result.

## Current objective

Improve general agent progress through model-independent context, validation,
and recovery mechanisms. The primary actor is the larger local model; the 4B
worker remains advisory and cannot edit, execute, or declare success.

## Start here: operating instructions for the next agent

This file is the active handoff. Read it completely before changing code.
The working directory is `/Users/digitialchameleon/noveltyEngine` on branch
`noveltyEngine`. The last committed baseline is:

```text
3ee9d3a Add task-derived validation and repair loop
```

The worktree is intentionally dirty. Existing uncommitted work includes the
watchdog, repair metrics, response-shape validation, 4B event coalescing, and
tests under `tests/test_novelty_context.py`. Do not discard or reset these
changes. Inspect them with `git diff` before editing.

### First five commands

```bash
cd /Users/digitialchameleon/noveltyEngine
git status --short
git diff --stat
python3 -m py_compile agent.py agentic_benchmark.py validation_contract.py novelty_context.py
python3 validation_contract.py
python3 -m unittest -v tests.test_novelty_context
```

If these fail, fix the failure before starting a model benchmark. Update this
handoff after every code, test-policy, or benchmark-policy change.

### Exact next work sequence

1. Review the uncommitted diff, especially the watchdog and repair metrics.
2. Run the focused `bug_repair` novelty eval first. It is the controlled test
   for failure-to-repair and previously passed.
3. Run the same `bug_repair` task with `--condition baseline` for comparison.
4. Inspect the newest JSONL records and compare the `repair` metrics, not just
   pass/fail.
5. Only then run `real_app`; use the watchdog and actively monitor it.
6. If code changes are made, run deterministic tests, update this file, and
   commit only after the result is understood.

### Focused benchmark commands

Use the real llama.cpp actor only when `llama-server` is already listening on
`127.0.0.1:8080`. Do not start a second server if one is already running.

```bash
python3 agentic_benchmark.py --task bug_repair --condition novelty \
  --iterations 18 --chat-timeout 60 --run-timeout 600 \
  --backend llama-cpp --base-url http://127.0.0.1:8080/v1 \
  --action-critic --action-gate --action-first

python3 agentic_benchmark.py --task bug_repair --condition baseline \
  --iterations 18 --chat-timeout 60 --run-timeout 600 \
  --backend llama-cpp --base-url http://127.0.0.1:8080/v1 \
  --action-first
```

Monitor from another terminal:

```bash
pgrep -fl 'agentic_benchmark.py|agent.py|llama-server'
tail -f state/benchmark/agentic/results.jsonl
```

The benchmark is complete only when its JSON result is printed and the
independent grader result is recorded. A timeout is an infrastructure result,
not a model-quality result. Do not claim success from `finish_task`; use the
grader and the JSONL record.

### What not to do

- Do not add Qwen-, Devstral-, Todo-, or SymPy-specific branches to the engine.
- Do not treat a 4B judgment as authoritative; local evidence wins.
- Do not remove the independent grader or weaken assertions to make a run pass.
- Do not launch an uncapped overnight benchmark.
- Do not reset, checkout, or delete existing worktree changes.
- Do not commit benchmark output blindly without inspecting what changed.

### Definition of a useful improvement

A change is useful only if it improves independently verified completion,
repair convergence, or context efficiency across more than one task shape. The
minimum evidence is a deterministic test plus a paired benchmark comparison.
Record iterations, mutations, validations, repair failures, repair mutations,
successful repair cycles, duplicate actions, worker calls, worker failures,
elapsed time, and independent grader outcome.

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
For resource-oriented API wording it also derives narrowly explicit response
requirements: created resources are JSON objects with their typed fields and
an identifier, while collection responses are JSON lists whose items are
inspected. These requirements are used to reject smoke checks that exercise
an endpoint but never establish its response shape.

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

The benchmark runner now has a 600-second per-run watchdog by default. It
starts the agent in its own process group and terminates the group on expiry,
preserving partial agent output and recording `timed_out` plus the watchdog
message. This prevents stale long-running benchmarks from surviving overnight.
Override with `--run-timeout` when deliberately testing a longer run.
Timeout buffers are normalized before metrics and JSONL recording so a partial
run cannot fail while being reported.

The agent and benchmark now also record repair-cycle metrics: validation
failures, repair-mode entries, repair mutations, revalidation attempts, and
successful repair cycles. These distinguish genuine recovery from repeated
editing or repeated testing.

## Next handoff actions

1. Read the newest `state/benchmark/agentic/results.jsonl` entry.
2. If the grader still fails, inspect the latest repair packet and generated
   artifact before changing policy.
3. Compare mutation count, validation count, repair transitions, duplicate
   checks, and independent pass/fail—not just model completion.
4. Update this document after every code or benchmark-policy change.

## Run-monitoring rule

Every live benchmark run must be actively monitored and reported to the user.
Before starting, state the task, condition, backend, iteration budget, and
monitoring plan. While it runs, poll for progress and report meaningful
milestones or stalls, including iterations, tool calls, mutations,
validations, worker activity, and errors when available. After completion,
report the independent grader result and final metrics. Do not launch a long
run without an active monitoring loop.

## Latest context-manager implementation

`novelty_context.py` now tags every event and worker judgment with an event ID.
If the asynchronous worker is busy, the newest actionable event is retained in
a single pending slot and launched after the current judgment completes. This
coalesces bursts without replaying an unbounded backlog. The model-facing
context marks judgments that lag the newest event and falls back to the local
deterministic recommendation for action-critical advice. Metrics now include
`coalesced_events`, `stale_judgments`, `latest_event_id`, and
`judgment_event_id`.

Deterministic verification passes:

```text
python3 -m unittest -v tests.test_novelty_context
python3 -m py_compile novelty_context.py agent.py validation_contract.py
python3 message_compaction.py
python3 validation_contract.py
git diff --check
```

## Latest implementation change

The API validation contract now tracks inferred response shapes and created
resource identifiers. This addresses the latest real-app failures where the
independent grader observed a task collection containing strings or a created
response without the expected `title`/`id` object fields, despite the actor
having run a superficially successful request. Deterministic checks pass:

```text
python3 -m py_compile validation_contract.py agent.py action_governor.py
python3 validation_contract.py
python3 task_contract.py
python3 action_governor.py
git diff --check
```

`AGENT_UX_EVAL.md` defines the product test protocol. Todo is only an external
fixture; the timing and repair instrumentation are generic and must transfer
to non-web tasks before a policy is promoted.

The benchmark now streams agent events live and persists each event to a
timestamped `state/benchmark/agentic/monitor-*.jsonl` file. Monitor the live
terminal plus that file; a final result is not required to diagnose a stall.
The child actor is launched with unbuffered Python output so event delivery is
immediate; if a future provider buffers internally, record that as a transport
limitation rather than claiming live visibility.
The terminal stream is compacted to event summaries; full raw lines remain in
the monitor JSONL so active monitoring does not consume unnecessary context.

## Proactive run ownership

When a benchmark is started, the active agent owns the run through termination.
It must poll the process and monitor log, report meaningful milestones and
stalls in the conversation, and report the final independent grader result,
timeout state, and metrics without waiting for the user to ask. A run is not
considered complete merely because model output stopped; verify the JSONL
result and process state.

Recent failure feedback now handles two generic environment mistakes: missing
imports inspect project declarations and distinguish an explicitly required
dependency from an ad hoc probe dependency. Required dependencies may be
installed through the project's normal workflow using internet access and
recorded in the project declaration; probe-only dependencies should prefer the
standard library. Foreground service timeouts recommend a bounded background
lifecycle plus a probe. These rules are not tied to Todo or any framework.

The generic background execution primitives are available through
`run_command(..., background=true)` or `run_shell(..., background=true)`, with
`process_status(handle)` and `stop_process(handle)` for inspection and cleanup.
The agent cleans up owned background processes when its run ends.

The most recent Todo run was started before these primitives were loaded, so
it is not evidence about the new lifecycle behavior. It ran 22 iterations in
587.5 seconds, made 9 mutations and 9 validations, and failed independently
because the task collection contained strings. Treat that as a baseline
failure; run a fresh process after this change before evaluating lifecycle
support.

The action-gate unit test now explicitly checks that targeted reads are
available before the orientation threshold and close after it, preserving the
intended policy that reads do not become an exploration loop.

The latest lifecycle Todo run confirmed that background start/status/stop
works, but exposed a generic coverage hole: a passing `/health` probe reopened
the actor before `/api/tasks` had been exercised. `ValidationContract` now
keeps the focused validation phase active until every interface named by the
task has accepted evidence. The contract renders the remaining-interface rule
to the actor and exposes `uncovered_endpoints()` for the state machine. This
does not add Todo-specific behavior; it applies to any task with multiple
interfaces. The run still failed independently because the actor never
validated the create-task response and the grader found a missing `id`.

After this change, run:

```text
python3 validation_contract.py
python3 -m unittest -v tests.test_novelty_context tests.test_agent_tools
python3 -m py_compile agent.py validation_contract.py
git diff --check
```

Then repeat the real-model Todo benchmark and inspect whether the actor probes
each required interface before attempting completion.

The first rerun after that coverage change exposed and fixed another generic
scope error: inferred fields for a create response were being demanded from a
health probe. Response requirements are now operation-scoped. Write probes
validate created-object fields/shapes, collection probes validate list/item
shapes, and unrelated API probes are not forced to satisfy either response
schema. This keeps the contract task-derived without teaching it Todo-specific
paths.

The operation-scoping self-test uses an explicit write probe; health and read
probes must not look like create requests merely because they mention the same
API path.

The follow-up parser fix also bounds each endpoint's inferred response scope to
its own task clause. A later collection requirement can no longer leak into an
earlier health endpoint merely because both appear in the same prompt.

Repair packets now apply that same scope. A failed health or unrelated read
probe no longer displays create-response fields as if they were required for
that endpoint; write probes receive object/field guidance and collection
probes receive list/item guidance.

The repair-phase prompt now explicitly treats validation scripts as evidence,
not the thing to rewrite. Unless the failure is explicitly a probe dependency,
setup, or syntax problem, the actor must repair the artifact under test and
must not weaken or rewrite the check to make it pass. This is a generic repair
integrity rule intended to reduce small-model repair loops.

The validation contract also accepts a successful health response containing
the task-required `status=ok` as health evidence; curl does not need to emit a
literal assertion for that narrow health check. During a multi-interface
validation phase, the actor prompt now prints the exact interfaces that still
lack accepted evidence, so the small model is directed toward the next
behavior instead of repeating an already-good health probe.

The health-evidence path is covered by the validation self-test and keeps
endpoint detection ahead of operation-specific checks.

The self-test now covers the actual JSON form `{"status": "ok"}` as well as
the compact form, preventing quoted JSON keys from being misclassified.

The latest live trace found that `run_command` is intentionally classified as
an observation capability, whose generic success inference is `None`. During
the explicit validation phase, the engine now accepts the task-derived
validation contract's positive assessment directly instead of requiring the
capability classifier to call the shell command a mutation/validation tool.
This restores shell-based behavioral validation without changing tool
classification.

## Context ceiling incident and fix

The real llama.cpp trace showed prompt growth from 1,201 tokens to 15,937
tokens, followed by a request of 16,785 tokens against `n_ctx=16,384`.
Assistant scratch compaction had reduced model prose, but the novelty-only
run did not enable the optional raw-tool pruning branches, so repeated file
reads and shell output remained in the live transcript. This was a genuine
context-manager bug, not merely a llama.cpp configuration issue.

`agent.py` now applies `_bound_live_tool_results()` after every tool batch in
every mode. It discovers llama.cpp's `/props` context size when available and
keeps raw tool output to 18% of the provider window, using a conservative
4-character/token estimate. Other providers use their configured `NUM_CTX`,
and discovery failure falls back to a safe 16,384-token window. Older tool
results are replaced in place, preserving tool-call/result pairing. This is
percentage-based rather than a fixed token-count policy; the 4B novelty worker
remains responsible for useful semantic guidance, not for making an unbounded
transcript safe.

The provider may still be configured with a smaller context than
`agent.py`'s preferred `NUM_CTX`; the live transcript must remain bounded
regardless. Validate with:

```text
python3 -m py_compile agent.py
python3 -m unittest -q tests.test_novelty_context tests.test_agent_tools
python3 message_compaction.py
python3 validation_contract.py
git diff --check
```

The transport now also performs an exact pre-request measurement through
llama.cpp's `/tokenize` endpoint. It reserves `max_tokens` plus a 256-token
margin for the response, then replaces oldest tool payloads and remeasures
until the prompt fits. If `/tokenize` is unavailable, the percentage/raw-tail
bound remains the fallback. If the prompt still cannot fit, the request fails
with an explicit `PromptBudgetError` instead of sending a doomed request or
retrying the same oversized prompt.

The measurement payload includes the offered tool schemas as well as chat
messages. This matters because llama.cpp's reported prompt usage includes
those schemas; measuring messages alone undercounted the real request.

## Benchmark process cleanup

`agentic_benchmark.py` now terminates the complete actor descendant tree on
watchdog timeout, keyboard interruption, or abnormal stream termination. This
includes services launched by the actor with `background=true`, even though
those services intentionally create their own process sessions. Verify with a
controlled interruption and then confirm no `agent.py`, benchmark, or task
server process remains.

Repeated `stop_process` calls now report `Already stopped ... launch a fresh
process before probing` instead of pretending cleanup made new progress. This
gives the actor truthful stale-handle evidence and prevents a cleanup loop from
being mistaken for successful lifecycle validation.

## Important files

## Mandatory continuation protocol

This work is an active test-fix cycle. After every implementation change, the
next agent must run the relevant deterministic checks and then a real-model
evaluation when the change affects the agent loop, context, tools, transport,
or lifecycle. Do not stop after explaining a result or applying an isolated
patch. Monitor active runs, inspect the independent grader, fix the next
generic failure, and continue until the test completes or a real external
blocker requires user direction.

- `agent.py`: actor loop and validation/repair state machine.
- `validation_contract.py`: task-derived acceptance and repair packets.
- `action_governor.py`: capability classification and evidence bookkeeping.
- `novelty_context.py`: asynchronous 4B context worker.
- `agentic_benchmark.py`: independent agentic graders.
- `state/benchmark/agentic/results.jsonl`: benchmark history.

## Actionable test failure evidence

The real bug-repair cycle exposed a generic weakness in the shell/tool
contract: `workspace/run_tests_tool.py` previously returned only aggregate
counts. A small model then knew that validation failed but not why, so it
could reread the same implementation or rewrite its test. The runner now
keeps the count headline and appends bounded failure/error case names plus
the final traceback lines (up to 1,800 characters). This gives the next turn
actionable evidence without allowing a large traceback to consume context.

After changing this tool, run its self-test and the full deterministic suite,
then repeat a real llama.cpp bug-repair benchmark. Judge the result by the
independent grader and by whether a failed validation is followed by a
targeted implementation mutation and a passing revalidation.

The same cycle also found that agents commonly pass a focused test filename
to `run_tests`. The runner now accepts both directory and file targets; a
file is converted into parent-directory discovery with an exact filename
pattern. Its self-test covers both forms.

The next live run exposed a separate gate issue: after an execution/setup
failure such as a missing interpreter, repair mode had removed `run_command`
and `run_tests` from the available tools. `agent.py` now detects bounded setup
failure markers and temporarily permits those tools so the actor can recover
the execution path (for example by selecting an available interpreter). A
normal assertion failure remains mutation-first and cannot be papered over by
repeated probes.

The branch-local registry manifest was also still pointing at the original
`evolutionEngine` checkout. That meant live benchmarks could silently load
old graduated tools even when this branch's source and unit tests were green.
All manifest module paths now point at this checkout; verify this whenever a
new copy is created, before trusting a live result.

The live Todo run then exposed a validation false negative: a shell script can
prove a JSON object/list by printing the response and a successful field
assertion without using the literal words `object` or `list` in its command.
The contract now accepts concrete JSON delimiters (`{...}`/`[...]`) together
with bounded success-language evidence such as “returns task with id and
title”. A regression case covers this script-style evidence; it does not
accept a bare successful command or an unasserted payload.

A second live script format printed endpoint labels directly, for example
`POST /api/tasks: {"id": ...}`. The contract now recognizes endpoint-labeled
JSON output and JSON-key evidence as well. This remains bounded to required
fields and required interfaces, so it is not a generic “Exit code 0 means
success” bypass.

The final validator edge is raw JSON output from a focused request, such as
`curl /api/tasks` followed by `[ {"id": ...} ]` with no label. This is accepted
only when the command contains the required interface and the response has
the required shape/fields. A regression test covers the raw collection form.

The latest live Todo grader exposed the deeper contract bug: POST and GET
shared `/api/tasks`, so POST evidence incorrectly counted as GET evidence.
Validation contracts now retain required method/path operations and coverage
is method-aware. A passing `POST /api/tasks` probe can no longer satisfy a
required `GET /api/tasks` collection check.

The follow-up live run passed both operations and the independent grader but
still looped after `finish_task`. Completion verification was re-assessing
tool output with empty arguments, which discarded the original command's
endpoint/method. `_completion_ready` now uses the dispatch-time accepted
evidence ledger and method-aware coverage instead of re-parsing tool output
without its arguments. This lets a validated agent stop even when an
unrelated optional probe (such as an unavailable pytest command) fails.

The next live cycle exposed stale-service validation: after editing
`server.py`, the actor continued probing the already-running old process.
`kernel.exec_tools.active_background_handles()` and the actor loop now emit a
restart directive whenever a live managed process exists after mutation. The
actor must stop that handle, launch the updated service, and then validate;
this applies to any mutable long-running service, not just Todo apps.

The first guard implementation left that directive in permanent history and
caused repeated stop/start cycles. It is now a one-turn pending instruction:
the next actor turn performs one restart, after which normal validation
instructions resume.

The live evidence showed the 35B could still spend several turns deciding to
stop the stale process. The engine now automatically stops every live managed
background handle immediately after a workspace mutation, then gives the
actor one transient instruction to launch a fresh process. This makes service
freshness deterministic instead of depending on the actor to notice it.

The next live cycle showed combined API scripts need method-specific shape
checks too: a script containing POST and GET was treated as a write probe, so
the GET collection could incorrectly return a wrapper object. Shape assessment
now checks creation-object and collection-list requirements independently;
the validator regression suite includes a wrapped-collection failure case.
