# Novelty Engine Handoff

Updated: 2026-08-15

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

The reusable benchmark now includes `cascading_loop`, based on the multi-step
dependency specification: an isolated `target_code.py` first fails to compile,
then exposes a runtime type error after the syntax repair, and is graded only by
an independent pytest subprocess. The prompt does not name either expected
patch, replace the test, or add model/provider-specific logic. Run it with
`--task cascading_loop` to measure whether the actor moves through fresh failure
states instead of repeating the first repair.

The cascading grader prefers pytest when installed and otherwise invokes the
provided `test_calculation` function in a fresh Python subprocess. This keeps
the test valid on minimal environments without weakening the assertion or
coupling it to the expected implementation.

The first live cascading run exposed three generic loop defects. The command
boundary now normalizes a missing `python` alias to an available Python
interpreter; validation recognizes direct `test_*.py` checks as evidence; and
a successful, fully covered validation adds a deterministic delivery nudge so
the actor calls `finish_task` instead of rereading the corrected file. The
cascading scorecard now separates artifact correctness from workflow success
and requires both `finish_task` and the specified iteration target.

The next live pass found another false-positive path: executing a pytest-style
`test_*.py` file directly can exit zero without invoking any test function. The
validation contract now rejects that silent result and tells the actor to use a
runner or explicitly call the test function. A zero exit code alone is never
enough evidence for completion.

The execution boundary also converts a silent zero-exit direct `python
test_*.py` invocation into an actionable error. This prevents a model from
repeating a command that technically exits zero but never calls the test
function, and lets the normal recovery path select a real runner.

The hard cascade exposed the next generic failure mode: after a semantic test
mismatch, Mistral repeatedly reread the implementation while the 4B worker's
stale judgment was still rendered. The engine now extracts compact exception
and unittest assertion-diff evidence, renders the deterministic replacement
for stale worker judgments, and applies a repair lock after one targeted
inspection so the next turn must mutate the implementation. This is intended
to improve semantic repair without naming any fixture, file, model, or
provider.

The hard-cascade rerun then exposed a validation false negative: standard
`unittest` writes its successful report to stderr, while the silent-test guard
only checked stdout. The guard now treats both streams as evidence, preserving
the rejection of genuinely silent test modules without rejecting real passing
tests.

The following deterministic check found the validator also missed named
unittest output such as `test_report_contract ... ok` because it only matched
the standalone word `test`. Named `test_*` evidence is now recognized while
silent direct test modules remain rejected.

The next cycle found that a dead llama.cpp endpoint caused the agent to spend
all retry attempts on `Connection refused` and end with a traceback. Provider
errors that cannot improve through retry now terminate the run cleanly and
record the blocker immediately; transient malformed responses still retain
the bounded retry policy.

## Generic-to-specialized architecture priority

Build additions in descending order of how many workflows they help. The
universal core comes before domain adapters:

1. Persistent task memory: goals, completed steps, failures, artifacts, current
   state, and next action must survive beyond the prompt.
2. Checkpoint and resume: save state after meaningful mutations and validation
   so a timeout or process restart can continue safely.
3. Universal action/evidence ledger: record tool calls, results, file changes,
   processes, and validation evidence in structured form.
4. Generic failure recovery: handle repeated actions, malformed arguments,
   stale processes, missing dependencies, and incomplete validation.
5. Generic planning and milestone tracking: derive milestones from any task
   without embedding Todo, SymPy, or TUA-specific assumptions.
6. Environment and dependency state: track tools, packages, services, ports,
   working directory, and external resources.
7. Independent completion validation: require evidence that the objective was
   achieved, not merely that a command returned zero.
8. Model/provider compatibility: normalize tool schemas, structured outputs,
   timeouts, context limits, and provider-specific protocol differences.
9. Specialized adapters: add spreadsheet, image, browser, medical,
   engineering, and other domain support only after the universal core is
   reliable.

Architectural rule: persistent state is authoritative; prompt context is a
temporary working view; the 4B worker is advisory; validation decides whether
work is complete. TUA-Bench is a useful stress test for this roadmap because
its long, heterogeneous tasks expose memory and recovery failures that a small
Todo task cannot reveal.

## Benchmark-cycle strategy

TUA-Bench is part of the test cycle, but not the only test. Use three layers:

1. Fast deterministic tests and small agent tasks after every code change.
2. Focused agent tests—Todo, bug repair, dependency recovery, and selected TUA
   hard tasks—after meaningful architecture changes.
3. The broader TUA-Bench suite as a periodic long-horizon stress test after
   changes to memory, checkpointing, recovery, validation, or tool protocols.

TUA is valuable because its tasks are execution-graded and span terminal
workflows beyond coding, but its environments are slower and heavier than the
local loop. A TUA score must therefore be treated as a system-level signal,
not as the only regression test.

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

The frozen cascading multi-file repair cycle was then run against the real
Devstral/Mistral model through llama.cpp. The unchanged fixture progressed
through syntax repair, a runtime type repair, case-insensitive label ordering,
and validation-driven context compaction. The 4B worker produced asynchronous
judgments; stale judgments were explicitly ignored in favor of deterministic
local state. The context stayed within the 8,192-token provider window and
automatically pruned old tool output.

That run exposed a model-level semantic failure: after seeing actual `3.0`
versus expected `33.0`, the 35B repeatedly added float conversions instead of
changing the calculation's meaning. The engine now adds generic repair guidance
for assertion mismatches: compare actual and expected values semantically and
repair computation, shape, ordering, or meaning—not only types or formatting.
The validation repair packet carries the same model-agnostic contract reminder,
with a regression test. This is guidance infrastructure, not a fixture-specific
tax rule. The live attempt was stopped after the repeated semantic stall; the
fixture remains partially repaired and the test file remains unchanged.

The clean rerun confirmed the semantic guidance improved diagnosis: the model
removed the extra return wrapper and recognized that tax and ordering remained
wrong. It then made repeated syntactically invalid generator-expression patches;
the tool rejected them without writing. The engine now records a rejected
mutation and tells the next repair turn not to repeat the same edit, to use a
different valid mutation representation, and to account for the rejection
message. This is a generic tool-feedback recovery rule. The rerun is paused at
that repair point for the next validation cycle; the frozen test remains
unchanged.

The next live attempt correctly repaired the tax semantics (`30 * 1.10 =
33.0`) and reduced the remaining failure to output shape plus label ordering.
It then misread `['Beta', 'alpha']` versus `['alpha', 'Beta']` as descending
order. The failure diagnostic now states the unittest convention explicitly:
`-` is actual and `+` is expected, and case, nesting, and ordering are
meaningful. This remains generic assertion-diff guidance. Deterministic tests
remain green; the real-model run is ready to restart from the frozen fixture.

The faster Ollama run exposed a separate validator correctness bug. The
graduated `run_tests` tool runs inside the long-lived agent process, and
unittest reused an implementation module from `sys.modules` after the agent
patched it. The validator therefore reported an obsolete TypeError even though
the current file was correct. `workspace/run_tests_tool.py` now invalidates
caches and removes modules belonging to the target project before each
discovery. A regression test edits a module between two passing validations;
the suite is now 24/24, and the tool self-test passes.

The subsequent live run confirmed the cache fix but exposed evidence loss in
the same validator: its failure summary kept only the final three traceback
lines, often dropping the actual/expected assertion values. The test runner
now preserves bounded `AssertionError` and `+`/`-` diff lines alongside the
traceback tail. A regression test verifies that assertion operands survive in
the returned summary. The deterministic suite is now 25/25 and the runner
self-test still passes.

The next Ollama cycle showed the model could use the preserved diff: it
identified case-insensitive ordering and the total-plus-tax meaning. Its first
ordering patch used invalid Python generator syntax, and repeated retries hit
the same parser rejection. `kernel.io_tools.validate_python_syntax` now adds a
generic corrective hint when Python reports that a generator expression must
be parenthesized. This is language/tool feedback, not a fixture-specific
patch. The deterministic suite is now 26/26.

The clean rerun then exposed a small-model editing problem: the actor had the
correct repair text but omitted the common four-space Python block indentation,
so exact patch matching rejected it repeatedly. `patch_file` now has a narrow,
unambiguous fallback for a uniformly omitted outer indentation level; internal
indentation and content must still match, and the replacement receives the
original block indent. A regression test covers this behavior. The deterministic
suite is now 27/27.

The completion-only verification pass exposed one final generic lifecycle bug:
`_completion_ready` required a file mutation for every `code_change` task,
including tasks that explicitly only asked to run and verify an existing
implementation. The gate now permits mutation-free completion only when the
task text contains no build/change/repair verbs and accepted validation
evidence exists; repair/build tasks still require a mutation. A regression test
covers this distinction. The deterministic suite is now 28/28.

Using Ollama's faster local actor (`qwen3.5:9b`) with the asynchronous 4B
worker, a clean frozen-fixture run repaired the syntax/type defects, tax
semantics, case-insensitive ordering, and response shape. The validation
returned `Ran 1 tests: 1 passed, 0 failed, 0 errors`; an independent direct
`python3 test_pipeline.py` also passed. The frozen test file was not modified
and its current git-object hash is `270b1c949d28d4cf6f182a058127762d253f37ad`.
The first completion attempt hit the iteration budget while verifying
`finish_task`, so lifecycle completion still needs a larger-budget pass.

The harder frozen multi-file task then passed end-to-end with the real Ollama
actor and 4B worker. It required edits in `service.py`, recovered from a bad
first patch that removed the local `record` definition, fixed stable `rec-N`
IDs, removed an incorrect collection wrapper, revalidated, and emitted `DONE`.
The independent `python3 -m unittest test_service -v` check passed. Metrics were
16 iterations, 4 validation failures, 2 successful mutations, 6 validations,
and 4 worker calls; stale worker judgments were ignored by deterministic state.

The next stateful catalog task exposed a registry boundary bug. When the model
called the graduated `run_tests()` tool without its optional path argument,
the function defaulted to the agent process cwd and ran unrelated checks. The
registry now fills omitted path parameters with `.` before confinement, so they
resolve to the active project root. A regression test covers the default-path
behavior. The deterministic suite is now 29/29.

The resumed catalog run exposed a false-positive validation path: a command
like `python -m unittest.main` can exit 0 while discovering no tests. The
contract now rejects runner commands that report zero tests, even with exit 0,
and gives a focused-test next action. A regression test covers the warning-only
case. The deterministic suite is now 30/30.

The third stateful catalog task then completed with a clean bounded verification
pass and `DONE`. It exercised persistent add/update/filter behavior after the
actor repaired one ID defect and one response-shape defect. The run confirmed
the registry path fix in practice: an omitted path resolved to the active
project, and the focused test ran once with `1/1` passing.

The fourth, more stateful configuration task also completed with `DONE`. It
tested missing-file defaults, overrides, disabled jobs, persistence, and
malformed JSON recovery. The actor made one generic implementation mutation in
`config_store.py`; the focused test passed with `1/1`, and an independent
`python3 -m unittest test_runner -v` check passed. The run used the 9B actor and
4B worker, with two worker judgments and no stale-state interference.

The next real-model cycle exposed repeated command-schema drift from the small
actor: JSON-encoded command strings, `argv` instead of `command`, and
millisecond timeout fields. Dispatch now normalizes only these unambiguous
aliases before invoking trusted execution tools. A regression test covers all
three forms. The deterministic suite is now 31/31.

The live verification of that normalization found one adjacent schema artifact:
the actor supplied `cwd` wrapped in literal quote characters, including
`"."` and `""`. Dispatch now strips only matching outer quotes and removes
empty/default cwd values so the trusted tool uses the active sandbox root. A
regression assertion covers the normalization; all deterministic tests remain
green.

Live telemetry showed the `--action-first` contract was blocking the actor's
usual first `list_workspace` call, wasting a turn before the same bounded
inspection succeeded. The initial contract now includes `list_workspace`,
`find_files`, and `run_tests` alongside read/mutate/execute/finish tools. This
keeps broad network or shell exploration unavailable while removing an
avoidable routing failure. Deterministic tests remain 31/31.

The live check confirmed the improvement: the first action contract offered
`run_tests`, the actor validated `test_runner.py` in one call, and the agent
emitted `DONE` in two iterations with no blocked discovery call.

The follow-up real-model verification succeeded: the actor emitted a quoted
default cwd (`./`), dispatch normalized it, the test runner executed the
project's one test, and the agent emitted `DONE`. This confirms the command
normalization path works in the live loop, not only in unit tests.

The next harder ingestion task initially exposed a fixture setup omission:
`report.py` had not been created even though it was part of the declared
isolated task. It was restored without changing the frozen test. The untouched
baseline then failed on the intended malformed CSV row. With the 9B Ollama
actor and 4B worker, the agent repaired `parser.py` to skip missing/non-numeric
amounts, then repaired `report.py` to sort names case-insensitively. It passed
the frozen test in iteration 12 and emitted `DONE` in iteration 13. An outside
`python3 test_report.py` run also passed; the frozen test hash was
`bd01d713a877e3e6234d3b5d25a49fbcf9ccda1e0e4e61d1df31b235f71d5ce5`.
Metrics were 15 events, 2 mutations, 5 validations, 3 validation failures,
and 2 worker calls. The worker identified stale/repeated activity, while the
deterministic event state kept the actor on the concrete repair path. The
repository deterministic suite remains 31/31, and no generic engine change
was justified by this run.

The following event-log cycle exposed a generic argument-boundary issue rather
than a task-specific repair issue. The actor repeated a malformed scalar
timeout value (`"30,\\nbackground=False]"`) while trying to run the test. Dispatch
now salvages a numeric prefix for string timeout values, while leaving command
content and other arguments untouched. A regression assertion covers this
case; the deterministic suite remains 31/31. The event-log implementation was
then repaired by the actor to treat a missing log as an empty log. Both frozen
tests passed, including duplicate-event suppression and delete replay, and an
outside `python3 test_state_store.py` run passed. The live run used the 9B actor
and 4B worker, with one implementation mutation, six validations, five worker
calls, and no test-file edits.

The next transactional-batch task was intentionally harder: duplicate IDs,
blank customers, malformed amounts, negative amounts, aggregation, and stable
ordering. The untouched baseline failed as expected. The real actor did not
finish within 24 iterations. It repeatedly reread the same frozen test,
misread the actual/expected arithmetic (`2 + -1` versus expected `2`), made
several malformed or incomplete rewrites, and ended without calling
`finish_task`. This is a genuine agent failure; the fixture and test were not
changed. The run had 25 events, 6 validations, 5 validation failures, 7
mutations, and 8 worker calls, with 29 stale judgments. The failure was not a
provider/context overflow: it was semantic analysis paralysis after a repeated
assertion mismatch.

To address that generic failure mode, repeated semantic validation failures now
add an explicit recovery instruction: trace the reported actual value back to
each contributing input or state transition, compare that trace with expected,
then make one mutation; do not speculate or reread unchanged files repeatedly.
The deterministic suite remains 31/31. This change must be re-tested against
the same frozen batch task and then against a different semantic task before it
is considered effective.

The repeated-failure recovery was then tested against the same frozen batch
task from a clean implementation baseline. This time the actor reached the
semantic trace: it identified that `2 + (-1)` produced the observed `1.0`,
while the contract required negative records to be ignored. It also repaired
blank-customer filtering, malformed amounts, first-seen ID deduplication, and
case-insensitive ordering. The focused test passed in iteration 17 and the
agent emitted `DONE` in iteration 18. An independent `python3 test_batch.py`
run passed; the frozen test hash is
`ad06e7088b5546853ed74e128745d8a5dea598b7c09de074833ba7732483af79`.
Metrics were 18 events, 4 validation failures, 4 repair-mode entries, 6
mutations, 6 validations, and 2 worker calls. The first run had failed at 24
iterations; the rerun demonstrates that the generic trace instruction changed
the outcome rather than the benchmark being edited.

The dependency-graph benchmark was the next harder cycle. Its frozen contract
requires stable topological ordering, skipping a task whose dependency is
missing, and raising on a real cycle. The actor repeatedly confused an
unresolved external dependency with a cycle, then produced increasingly broad
rewrites, incomplete functions, and finally a non-terminating planner. The
real run was stopped at its controlled 24-event budget; it did not reach
`finish_task`, and the frozen test was never changed. This is a substantive
agent failure, not a passing benchmark artifact.

The failure exposed two generic protections. Mutation errors reported as
`ERROR:` are now treated like `REJECTED:` mutations, so the next repair turn
removes `write_file` and prefers a narrow `patch_file` recovery. Also,
`workspace.run_tests` now has a 30-second execution bound and reports a stuck
implementation as ordinary failure evidence instead of hanging the parent
agent. A deterministic regression uses a shortened timeout and passes. The
repository suite is now 32/32. These changes still need a fresh real-model
graph run and an independent outside verification before they are considered
effective.

The clean graph rerun after those protections still failed. Starting from the
original planner, the actor added cycle detection, then repeatedly damaged the
implementation while trying to reconcile the frozen contract's two distinct
cases: an external missing dependency should be omitted, while an all-internal
cycle must raise. It lost initialization variables, produced incomplete
functions, and never reached a passing validation. The run was stopped during
a later provider call after 19 events because it was repeating the same repair
pattern; the frozen test remained unchanged (hash
`ab6475c5caca076d9fdc0bd71301da848082b429a6b071b6b3812a3ba9b90c57`). This
confirms the graph task is currently beyond the actor's reliable semantic
repair ability, even though the engine's deterministic suite is 32/32 and the
new test timeout prevents implementation hangs from trapping the parent.

Because both graph runs showed valid-but-destructive whole-file rewrites after
repeated assertion failures, the repair gate now removes `write_file` after two
behavioral validation failures (while retaining it for initial work and setup
failures). The actor must preserve the current artifact and use `patch_file`
for subsequent localized repairs. The deterministic suite remains 32/32.

The graph was rerun from a clean baseline after that escalation rule. The
actor still failed to solve the two-case contract: it repeatedly generated
incomplete cycle-detection implementations and whole-file rewrites, despite
the later patch-only restriction. The run reached 18 events with two
validation failures before the actor entered another malformed-response loop;
the frozen test was unchanged. This task is currently a reliable stress case
for small-model semantic drift, but its missing-dependency wording is more
ambiguous than the earlier cascading fixtures.

The same run exposed a provider-loop issue: Ollama returned repeated XML tool
call parse errors while retrying the same turn. Agent retries now stop after
two repeated XML syntax failures instead of consuming the full 20-attempt
backoff. The deterministic suite remains 32/32. The graph fixture is left
untouched for future regression work; do not treat its corrupted implementation
as an engine artifact.

The clearer TTL/LRU cache task was then run with the real 9B actor and 4B
worker. The baseline failed on recency eviction. The actor entered another
analysis loop, made an invalid broad rewrite, and ended at its 16-iteration
budget without passing validation. The frozen test was unchanged. This run
exposed a lifecycle classification bug: a rejected mutation could still be
classified as successful by the action governor, reopening the repair cycle.
Mutation results beginning with `ERROR:` or `REJECTED:` now force
`success=False`, preserving the repair lock. The deterministic suite remains
32/32. The cache task remains an unsolved real-model benchmark for the next
cycle.

The cache task was rerun from its untouched baseline after the mutation
classification fix. The actor correctly reached the first expiry/eviction
failure, but misread the test sequence, changed the expiry comparison instead
of implementing access-order LRU, then entered repeated broad reasoning and
provider latency. After two validation failures, the patch-only repair gate
was active and prevented further broad edits; the run was stopped while the
actor produced no actionable repair. The frozen test was unchanged. This
confirms the gate behavior, but the cache task still does not pass with the
current small actor.

The larger locally installed `qwen3.6:35b-mlx` actor was then tested against
the untouched cache fixture with the 4B worker. It never reached iteration 1:
the MLX runner failed with a Metal command-queue/GPU timeout. A subsequent 9B
health check also failed because the Ollama service was no longer listening on
port 11435. No real-model result is claimed after that point; deterministic
engine work continued independently.

Recent cache and graph runs showed the actor narrating hypotheses without
tracing stateful operations in test order. The validation-repair prompt now
explicitly requires exact operation-order tracing for stateful behavior and
one concrete mutation before further explanation. The deterministic suite
remains 32/32. Real-model revalidation is pending Ollama recovery.

## 2026-08-14: activate the existing risk checkpoints

Inspection found that `risk_layer.py` already took in-memory snapshots before
mutations, but no runtime path used them. A generic safety gate now uses those
snapshots during repair turns: if `write_file` on an existing file removes
more than 65% of its non-blank lines and leaves a much smaller artifact, the
engine restores the exact pre-edit content and returns a rejected mutation
requiring `patch_file`. Initial file creation, normal non-repair rewrites,
and surgical patches remain allowed. This is based on edit shape, not on a
model, provider, task, or filename.

Added regression coverage for rollback and for allowed surgical edits. The
deterministic suite is now 34/34, plus `risk_layer.py`'s standalone self-test
passes. The next live cycle should rerun the frozen cache or dependency-graph
task from an untouched fixture and inspect whether destructive rewrites are
rolled back without preventing legitimate cascading fixes.

The first restored Mistral/llama.cpp cascading run used the correct Devstral
Q4 GGUF actor and Qwen3.5 4B worker, but the actor was placed on CPU with a
16,384-token context and did not produce its first tool call within the
60-second chat timeout. The frozen fixture and grader were unchanged; this is
recorded as a serving/timeout result, not an agent capability result. The next
run should keep the same models and task while increasing only the actor chat
timeout to accommodate the measured CPU load time.

The longer run exposed a more important integrity failure: after the syntax
repair, Mistral attempted to patch the supplied `test_metrics.py` rather than
repairing `target_code.py`. The risk layer now snapshots novelty-mode runs as
well as the older summary modes, marks test files that existed at run start
as protected evidence, and automatically restores/rejects mutations to those
files during repair turns. Newly created test files remain allowed for tasks
that explicitly add coverage. Added deterministic coverage for both cases;
the suite is now 35/35. The frozen cascading fixture was not changed by the
engine; its failed run remains a recorded capability result.

The protected-test rerun confirmed the safeguard in a real run. Mistral first
fixed `target_code.py`, then tried to append a direct call to the supplied
`test_metrics.py`; the risk layer restored the original test and returned a
clear rejection. The actor subsequently repaired the second implementation
bug, and the independent grader confirmed the final artifact passed. The run
still failed the workflow scorecard because CPU inference consumed the
600-second watchdog before the final validation and `finish_task` turn. This
is evidence that the integrity gate works, while actor serving latency and
completion latency remain separate issues.

The next reusable correction classifies the validator's “test module produced
no test evidence” result as setup/runner failure. The repair turn therefore
keeps `run_command`, `run_tests`, and `run_shell` available so the actor can
select a real runner or explicitly call the supplied test function; it does
not need to edit the protected test. This classification is provider-,
model-, and task-independent. Added regression coverage; the deterministic
suite is now 36/36.

The follow-up live run showed the actor repeating the same protected-test
patch after the first rejection, consuming another long CPU inference turn.
Dispatch now maintains a path-level block after a protected-test rejection:
the same path cannot be mutated again during that run, while implementation
patches and validation commands remain available. Added a dispatch regression;
the deterministic suite is now 37/37.

The latest cascade reached both implementation repairs and an exit-0
validation, but the actor was still spending another long turn trying to
request `finish_task`. The orchestrator now treats complete independent
validation as authoritative and marks the run done immediately, with a
machine-generated summary. This saves a model turn and prevents a correct
artifact from timing out behind a slow actor; it does not weaken validation or
allow completion without a mutation and accepted behavioral evidence. The
deterministic suite remains 37/37. The next live rerun should verify that the
same frozen cascade now emits `DONE` immediately after its passing check.

The next cascade attempt showed the model still proposing protected-test
edits before running a fresh check. Repair handling now enters a temporary
validation-only phase after the first protected-test rejection: `run_tests`,
`run_command`, and `run_shell` remain available, while mutation tools are
removed until a fresh executable result arrives. The stale run was stopped so
it could not mix code versions; the new code passes the full 37/37 deterministic
suite. Rerun the frozen cascade from a clean fixture before judging this
change, then run the supplied WebSocket task as the next harder workflow.

The supplied broken WebSocket chat is now registered as a separate frozen
`websocket_chat` benchmark. Its grader checks the frontend protocol contract,
safe DOM insertion, the declared `ws` dependency, a real two-client local
exchange, ping/pong, and a send after one peer closes. It does not rewrite the
fixture or accept static pattern matching as success. The project/model
services were stopped before this test; restart only the required Mistral
llama.cpp actor and Qwen worker, run the deterministic suite first, then run
this benchmark from a clean workspace.

The first WebSocket live run found a generic validation weakness: the actor
started only the dependency install, then used curl against a WebSocket URL;
curl exited 0 but returned `Upgrade Required`. The orchestrator incorrectly
accepted that as behavioral evidence and stopped while `index.html` was still
unchanged. Validation now requires actual interaction/assertion output for
web tasks that do not expose ordinary HTTP endpoints, with regression tests
for rejecting protocol-error-only output and accepting a reported client
exchange. The independent grader remains the final authority; rerun the
WebSocket task after the deterministic suite.

The first regression was intentionally strict enough to expose an overly
narrow evidence classifier: a legitimate client probe reported “received
pong and message” but the older generic keyword gate did not recognize it.
The classifier now recognizes observed exchange terms such as received,
connected, response, message, pong, WebSocket, and passed, while the separate
web-task rule still rejects an empty or protocol-error-only result.

The follow-up unit test found one representation edge case in the tool
adapter: compact command results without explicit `STDOUT:` labels were being
treated as empty output. The validator now treats the unlabeled result body
as output, preserving the same evidence rule across providers and command
wrappers.

The second live attempt reached the repaired files but the grader collided
with the llama.cpp actor because both used port 8080. This was a benchmark
isolation defect, not evidence about the app. The frozen WebSocket grader now
launches the app with an isolated `PORT=18767`, and the task explicitly
requires `process.env.PORT || 8080`; the actor/frontend behavior remains
unchanged. Rerun the task after this harness-only correction.

The third run showed the actor still hardcoded port 8080. More importantly,
the engine treated `process_status: EXITED code=1` as observation rather than
failed validation, allowing repeated dead-service checks without a repair
transition. The orchestrator now includes process-status results in the
validation phase and failure packet path; a failed service must be repaired or
restarted before the actor can proceed. Added a regression for nonzero service
status. Rerun the WebSocket task after the deterministic suite.

The fourth run found that `npm init` output contained the generic word
`test`, which was mistaken for behavioral evidence. The web-specific evidence
vocabulary now requires concrete interaction terms such as received,
connected, response, message, pong, handshake, round-trip, or an explicit
assertion/pass result. Dependency setup and package metadata no longer count.
Added a regression for npm setup output.

The fifth live run confirmed the stricter setup handling and exercised the new
process-status failure path: package installation was rejected, the server
was repaired once, the frontend validation attempted to start the app, and
`EXITED code=1` correctly triggered repair. Mistral then spent the remaining
900-second budget revisiting the port conflict and did not finish the frontend
repair or pass the independent grader. The final WebSocket result is a real
failure, not a coerced pass. Deterministic coverage is 41/41. All model and
test background services have now been stopped; the next continuation should
inspect the preserved fifth workspace and improve repair convergence before
rerunning the live task.

The next architecture change is a bounded repair-turn controller. After three
repair turns without a mutation, the engine enters recovery mode, compacts the
repair context to the task foundation plus recent evidence, and exposes only
mutation/review tools. It does not use `pkill -f node`, a fixed three-second
timeout, or premature completion. Process handles remain scoped, validation
remains mandatory, and the recovery state is tracked in repair metrics. Added
a deterministic checkpoint-bounding regression; run the full suite before the
next real-model WebSocket attempt.

The latest run found a contract-parser false positive: the second slash in
`http://localhost:8080` was extracted as an application endpoint. That made
the validator skip the no-HTTP-endpoint WebSocket evidence rule and accept
dependency setup as validation. `_PATH_RE` now excludes URL host slashes,
with a regression test. The artifact still had a correct server repair but an
unchanged frontend; rerun the benchmark after the deterministic suite.

The subsequent clean run made four inspection calls before any validation or
mutation, so the repair budget could not help: there was no failure yet. A
separate orientation budget is now added. After three no-mutation turns, the
next actor call is restricted to mutation, validation, recall, or finish, and
receives an explicit progress checkpoint. This is independent of task type,
model, and provider; it prevents analysis paralysis without removing the
initial opportunity to inspect an unfamiliar workspace.

The first orientation-budget run showed that allowing `run_command` and
`run_tests` in orientation recovery still let the actor reread files through
shell commands. Orientation recovery is now a true mutation gate: only
`patch_file`, `write_file`, `recall`, and `finish_task` remain available. A
successful mutation immediately returns the normal validation tools. This
keeps the policy engine-level and prevents shell-tool substitution.

## TODO: speculative context compression with the 4B worker

Do not forget this next architecture feature. The current Qwen3.5 4B worker
is an asynchronous event critic; it is not yet a semantic context compressor.
Implement compression as a bounded, speculative sidecar that never blocks the
35B actor and never declares task success.

Trigger it only on measurable stalls: repeated action fingerprints, two or
more failed validations, a no-progress repair turn, or exhaustion of the
three-turn repair budget. Send the worker a compact packet rather than the
entire transcript: recent tool events, latest failure packet, files/symbols
 touched, last mutation, validation status, and repeated-action counts.

Have the worker return strict bounded JSON with: `diagnosis`, `facts`,
`affected_files`, `last_mutation`, `validation_state`, `repeated_actions`,
`next_action`, `target`, and `confidence`. Validate and truncate every field;
unknown facts must not become durable state. Use a deterministic local
fallback when the worker is stale, unavailable, malformed, or over its time
budget.

Inject the result as an explicit engine-generated `Repair checkpoint`, clearly
marked as advisory rather than ground truth. On recovery, give the actor the
checkpoint plus the exact failure evidence and restrict tools to the smallest
valid action set. Keep raw history recallable but out of the active prompt.

Add deterministic tests for bounded output, stale-worker fallback, malformed
JSON, trigger conditions, and no-context-growth. Then measure whether it
reduces time-to-next-mutation, repeated-action rate, repair turns per fix,
false completion, and prompt tokens on the frozen cascading and WebSocket
benchmarks. Compare worker-on versus worker-off with the same actor and
fixtures; do not tune it to one model, task, or endpoint.

The first frozen cascade run with the orientation and repair budgets forced
real progress. Mistral initially inspected three turns, then the mutation
gate caused an implementation write. After a no-test validation and a blocked
protected-test edit, repair recovery forced a patch to `target_code.py`; the
independent grader confirmed the artifact passed. The run still hit the
720-second watchdog before its final validation/completion turn, so this is
partial workflow success, not a passing scorecard. The next check should keep
the same fixture and code while allowing enough time to measure the new
completion shortcut separately from CPU inference latency.

The three-layer refactor has begun. Layer 1 now has an assertion-driven tool
contract that separates execution success, setup-only state, failure, and
behavioral evidence before task-specific criteria run. Layer 2 now exposes a
bounded synthesized failure packet with diagnosis and one next repair focus
instead of replaying an unbounded raw log. Existing risk checkpoints,
stale-process cleanup, validation locks, and independent completion checks are
the initial Layer 3 proactive hooks. Add further owned-port and post-mutation
hooks only with deterministic transition tests.
### 2026-08-14 — zero-test runner feedback tightened

The frozen cascade exposed a generic convergence gap after the artifact was
repaired: the actor selected `python -m unittest test_metrics`, but the
supplied module contained a plain test function and unittest discovered zero
tests. The validation layer already rejected that as non-evidence; the repair
packet now explicitly says that no assertions ran and directs the actor to
inspect the supplied test and either invoke its function directly or select
the correct installed runner. This is model- and benchmark-independent and is
covered by `test_failure_feedback_replaces_zero_test_runner_with_explicit_assertion`.
### 2026-08-14 — explicit lifecycle FSM

Layer 3 now has an explicit `lifecycle_fsm.py` state machine. It governs the
high-level lifecycle only: `ORIENT`, `ACT`, `VALIDATE`, `REPAIR`, `RECOVER`,
`COMPLETE`, and `FAILED`. Mutation, validation failure, validation success,
and repair-budget exhaustion are named events with strict transition rules.
Invalid transitions raise `InvalidTransition`, making lifecycle drift visible
in deterministic tests instead of silently falling through scattered runtime
conditionals. Tool parsing, risk checks, and task-specific validation remain
separate policies; the FSM is not coupled to a model or benchmark. The suite
now passes 48 tests, including a complete repair/recovery path and an invalid
transition assertion.
### 2026-08-14 — setup failures no longer trigger destructive recovery

The FSM test exposed an important Layer 3 boundary. A missing test runner,
missing dependency, or zero-test discovery result is a validation/setup
failure, not evidence that the implementation should be rewritten. Previously
the repair-recovery hook could still force a mutation after three such turns;
the actor then replaced a correct cascade repair with a placeholder. The
`_force_repair_recovery` guard now permits forced implementation mutation only
for a genuine product/behavior failure. Setup failures retain bounded command
and runner recovery. This is covered by a deterministic regression test; the
suite passes 49 tests.
### 2026-08-14 — recovery guard initialization fix

The first post-FSM rerun caught an orchestration wiring error before the model
could act: `setup_failure` was initialized only inside the validation branch,
while the recovery transition is evaluated on every turn. It is now
initialized at the start of each loop iteration, and a regression test covers
the pre-validation path. This reinforces the rule that lifecycle guards must
be total over all states, not only the states where they usually fire.
### 2026-08-14 — preserve failure cause through repair turns

The next live cascade showed that a read-only repair turn could clear the
previous validation packet. That made a missing-runner/no-test-evidence
problem look like a product defect when the recovery budget fired. The loop
now retains the last nonempty failure packet and derives setup-vs-behavior
classification from both the packet and its compact summary. This prevents
the FSM from taking a destructive product-repair transition after the reason
for the failure has been compacted away. The deterministic suite passes 51
tests. The interrupted live comparison had an independently passing artifact
before it was stopped, but did not satisfy the completion scorecard.
### 2026-08-15 — structured 4B repair checkpoints

The 4B worker is no longer limited to a generic inspect/repair signal. On a
validation failure, NoveltyEngine now sends one bounded checkpoint containing
the lifecycle state, legal actions, protected paths, and compact failure
packet. The worker returns structured `diagnosis`, `failure_class`,
`next_action`, and `preserve_files` fields. A deterministic local classifier
is installed immediately, so the actor gets a safe setup-vs-behavior
recommendation even while the asynchronous 4B call is running or stale.
Setup failures recommend validation/runner recovery and preserve the
implementation; behavior failures recommend a targeted patch. The 4B still
does not execute tools—the FSM and orchestrator retain authority. Added
worker parsing, fallback, and rendering tests; the suite passes 53 tests.
### 2026-08-15 — recovery completion transition

The first live run with structured checkpoints reached the correct direct
assertion and returned exit code 0, but the new FSM crashed because it only
allowed `VALIDATE -> COMPLETE`; setup recovery was still in `RECOVER`. The
transition table now explicitly permits `RECOVER -> COMPLETE`, with a
regression test. This preserves the FSM invariant that every accepted
validation result, including one after setup recovery, can terminate cleanly.
The deterministic suite passes 54 tests. The live artifact itself passed, but
the run is recorded as failed because the missing transition prevented clean
completion.
### 2026-08-15 — synchronous 4B triage gate milestone

The 4B worker now has a bounded synchronous gate at validation-failure
boundaries, while routine event observation remains asynchronous. The gate
receives FSM state, legal actions, protected paths, and the compact failure
packet; it returns a structured failure classification and can only remove
tools for the next actor turn. Setup triage removes `write_file` and
`patch_file`, preserving the implementation while runner recovery occurs.
Behavior triage removes `finish_task` until repair/validation proceeds.
Malformed, stale, low-confidence, or busy-worker results fall back to the
deterministic policy. Structured checkpoint diagnoses are preserved when newer
ordinary events arrive. The deterministic suite passes 56 tests.

The latest real cascade reached independent validation success and clean
orchestrator completion through `RECOVER -> COMPLETE`; its benchmark scorecard
still reports false only because the actor needed 10 iterations rather than
the benchmark's under-3 target. This is progress in correctness and lifecycle
safety, but convergence speed remains an open optimization target.
### 2026-08-15 — deterministic precedence over 4B triage

The first real synchronous-gate run proved the gate could remove mutation
tools for a setup failure, but it also exposed a trust boundary: the 4B
hallucinated that a pytest decorator was required and reclassified a missing
runner as a progress problem. The gate now gives deterministic setup evidence
precedence. The 4B may refine the command or target, but it cannot convert a
known setup/runner mismatch into a product defect. Added a regression test with
a deliberately hallucinating worker; the suite passes 57 tests.

### 2026-08-15 — dependency setup is not product evidence

The WebSocket benchmark exposed another generic validation-plane error. A
successful `npm install` produced no behavioral assertion, but the repair
packet was classified as a product behavior failure because the fallback did
not recognize package-manager setup output. The deterministic checkpoint now
classifies common dependency-install commands and success summaries (`npm
install`, `npm ci`, `pip install`, `added ...`, `audited ...`, and equivalent
setup-only markers) as setup. The 4B cannot override that classification, so
the actor is directed toward the next real probe instead of editing product
files. The deterministic suite passes 58 tests. The WebSocket benchmark remains
open: its actor made a valid server mutation but timed out before completing
the independent frontend and runtime checks.

### 2026-08-15 — explicit validation-plane separation

The validation contract now labels executable outcomes as `setup`,
`verification`, or `non_evidence` in addition to its existing acceptance
decision. Successful dependency installation, process startup, and zero-exit
commands without assertions are setup/non-evidence; a passing test or an
asserted request/round-trip is verification. During setup recovery the actor
can still use explicit argv commands to select a runner or install a declared
dependency, but unrestricted `run_shell` is withheld along with file mutation
tools. This is an engine policy, not a model or benchmark rule. Added plane
classification regression checks; the deterministic suite passes 58 tests.

### 2026-08-15 — Qwen3.8 runtime tuning and MLX candidate

The first Qwen3.8 test used `--device none`, which forced the 27B GGUF onto
CPU and produced roughly 90-second actor turns. A controlled llama.cpp test on
the M4 Max found the useful configuration: `MTL0`, all GPU layers, Flash
Attention, one slot, 16K context, and `--reasoning off`. Warmed short requests
then reached about 0.17 seconds to first streamed byte and 1.1 seconds total;
thinking-budget probes around 128–1024 tokens took about 3.0–4.7 seconds and
did not improve the action response. This is the current Qwen coding-agent
baseline. llama.cpp exposes native reasoning budgets, including per-request
`thinking_budget_tokens`, but Qwen's high/xhigh labels are not direct
llama.cpp modes; they must be experimentally mapped to token budgets.

The optimized Qwen GGUF WebSocket run made two useful mutations (`server.js`
and `index.html`) in eight iterations, with first tool 10.1s and first
mutation 60.0s. The independent grader then crashed with `NameError: name
'port' is not defined`, so the run is not a benchmark pass or fail. The native
MLX 4-bit candidate requested by the user was downloaded to
`~/.cache/mlx/Qwen3.8-27B-4bit` (about 15 GB); it should be compared next.

### 2026-08-15 — earlier orientation convergence gate

The Mistral control run and the Qwen3.8 probe both showed the actor spending
its short real-model budget on repeated reads. The orientation governor now
switches to executable progress tools after two read-only turns instead of
three. This is a model-neutral convergence policy: it does not prescribe a
file, patch, or benchmark-specific action; it simply prevents a third
unchanged exploration turn. The next real run must compare first-mutation
latency and artifact progress against the prior three-read baseline.

The four-iteration comparison reached the new gate as expected: Mistral made
its first mutation on iteration 3, then ran `npm install`; the new checkpoint
classified that result as setup and withheld mutation plus unrestricted shell
tools. The short run ended before the actor had a repair turn, so it is not a
pass/fail capability result. First mutation was 182.9 seconds and first
validation was 262.2 seconds on this CPU-only server. A longer unchanged run
is required to measure whether the actor can now continue from setup into the
real WebSocket verification probe.

### 2026-08-15 — provider-neutral tool-call argument normalization

The first MLX agent run exposed a transport compatibility defect. The actor
adapter parsed tool-call `function.arguments` into a Python dict for dispatch,
then reused that dict in the next assistant message. MLX's OpenAI-compatible
server expects the field to remain a JSON string and returned `the JSON object
must be str, bytes or bytearray, not dict`. The transport boundary now
serializes non-string arguments before any provider receives the next message.
This is provider-neutral and does not branch on MLX or model name. The
deterministic suite passes 59 tests. Re-run the MLX benchmark after this fix;
the prior MLX result is a protocol failure, not a model score.

### 2026-08-15 — Qwen3.8 capability review and policy decision

The official [Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B)
describes a 27B dense vision-language model with native 262K context,
multi-token prediction, flexible thinking control, and explicit support for
`reasoning_effort` values `low`, `medium`, and `xhigh`. Its published results
include Terminal Bench 2.1 **73.0**, SWE-bench Pro **61.7**, DeepSWE 1.1
**42.2**, QwenSWEBench **79.0**, WebArena-Verified **64.8**, and
ClawEval-MM **57.4 Pass@3 / 56.9 average**. These are directional evidence,
not a direct apples-to-apples comparison: the table uses different harnesses,
benchmark versions, prompts, and context lengths than our local runs.

For comparison, the [Devstral Small 2 model card](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512)
reports SWE-bench Verified **68.0**, SWE-bench Multilingual **55.7**, and
Terminal Bench 2 **22.5**. Qwen's Terminal Bench 2.1 number is not directly
comparable to Mistral's Terminal Bench 2 number, while Qwen's SWE-bench Pro
and Mistral's SWE-bench Verified are different tasks. The public evidence
supports testing Qwen seriously for long-horizon terminal work, not declaring
it universally better.

The current NoveltyEngine constraints divide into two groups:

* Keep invariant: assertion-driven validation, setup/verification plane
  separation, protected test paths, FSM transitions, bounded repair budgets,
  rollback, and tool permissions. These protect the environment and are
  model-agnostic.
* Make adaptive: hardcoded `reasoning_format=none`, `reasoning_effort=none`,
  low temperature, fixed output reserve, and a universal two-turn orientation
  cutoff. These can suppress Qwen3.8's trained reasoning and long-horizon
  planning. Qwen's own guidance warns that lower reasoning effort can reduce
  per-turn latency but increase total retries on multi-turn agents.

Recommended actor policy is therefore a bounded two-speed profile, selected
by observed task state rather than model name: fast/no-thinking for orientation,
simple setup, and verification; bounded `medium` reasoning for a fresh
behavior failure; and `xhigh` only for a repeated, high-value diagnosis with a
hard token/time ceiling. The FSM and validator remain authoritative in every
profile. We should retain Mistral as the control actor, test Qwen3.8 GGUF and
MLX under the same frozen tasks, and choose by first mutation, accepted repair
rate, validation success, total wall time, and tool-call validity—not by model
card scores alone.

### 2026-08-15 — WebSocket grader harness repair

The WebSocket benchmark's independent grader had a real harness bug: it
created the Node probe with port `18767` but never defined the Python variable
used to set `env['PORT']`. Every recent WebSocket run therefore failed with
`NameError: name 'port' is not defined` before the application could be
graded. The grader now defines the same isolated port before launching the
server. This changes no acceptance assertion or expected behavior; it only
restores execution of the existing independent check. Added a regression test
that compiles the generated grader and verifies the port contract. The
deterministic suite passes 60 tests. A positive-control application now passes
the repaired grader, while the original intentionally broken fixture reaches
the expected WebSocket URL assertion and fails. This confirms the grader can
both accept the target behavior and reject the broken starting state. Model
comparison is paused here; the next benchmark run should be interpreted as a
real agent result rather than a grader-harness result.

### 2026-08-15 — validation runner compatibility and setup recovery gate

The reusable `run_tests` tool still uses unittest as its standard-library
path, but it now attempts a bounded pytest run when unittest discovers zero
tests and pytest is already installed. It never installs dependencies and
preserves the existing no-tests result when pytest is unavailable. This keeps
the runner compatible with both unittest projects and common function-style
pytest modules without making either framework mandatory.

The validation recovery policy now combines the raw repair packet with the
validator's failure-plane suggestion. A successful command that only prints a
value, starts a process, or contains no assertion is setup/non-evidence, not a
product failure. After one setup inspection, read-only tools are removed and
the actor must issue an explicit assertion-bearing command or test invocation.
This prevents correct code from being rewritten to compensate for a weak
probe.

### 2026-08-15 — lifecycle FSM completion invariant

The live cascading run found a real FSM inconsistency: after a validation
failure, a corrected executable check could pass while the FSM remained in
`REPAIR`; the orchestrator then sent `validation_passed`, which was rejected
and crashed the run even though the artifact was correct. `REPAIR` now has an
explicit `validation_passed -> COMPLETE` transition for setup recovery, and a
regression test covers that path. The transition remains table-driven and
validation evidence remains authoritative; no model or benchmark-specific
exception was added.

### 2026-08-15 — centralized validation action policy

The lifecycle FSM is now paired with a pure `lifecycle_policy.py` policy
function. It derives one immutable validation-phase action surface from the
current recovery snapshot: setup failures get one inspection and then only
explicit runner/command tools; behavior failures get one inspection and then
targeted mutation; protected or repeated failures narrow the surface further.
`agent.py` consumes this policy instead of reconstructing the validation tool
set through several independent branches. The 4B gate can still remove tools,
but cannot add tools or override the deterministic plane. Added unit coverage
for the setup-then-command and repair-then-patch transitions. The deterministic
suite passes 65 tests.

The first live policy run caught an implementation error in that policy: setup
recovery still exposed mutation tools during its initial inspection turn. The
policy now removes `patch_file` and `write_file` for every setup failure, not
only after inspection. This preserves the core invariant that runner,
dependency, and test-discovery problems cannot trigger speculative product
edits.

The next live trace found an orientation-policy conflict: after two idle
turns, the old allow-list removed `read_file`, causing the actor's targeted
inspection to be blocked and encouraging blind edits. Orientation recovery now
uses the centralized policy surface: targeted read/search plus mutation and
finish remain available, while broad listing/exploration stays unavailable.
Setup recovery separately removes mutation tools, so these two policies do
not conflict. Added deterministic coverage; the suite now passes 66 tests.

The following live run exposed stale-worker authority: the 4B gate removed
mutation tools during a current behavior-repair state because its pending
judgment still described setup. The deterministic policy now filters worker
restrictions by the active plane. A behavior repair always retains its legal
mutation tool; a setup recovery may still retain the worker's mutation ban.
Added regression coverage. The deterministic suite passes 67 tests.

The attempted observation-only ablation found a flag-wiring defect: the agent
was consuming synchronous 4B gate restrictions whenever novelty context was
enabled, even when `--novelty-action-gate` was off. Gate consumption is now
explicitly opt-in. This makes the planned ablation honest: novelty context can
observe and record events without changing the actor's legal tools. Added
coverage for both disabled and enabled gate behavior; the deterministic suite
passes 68 tests.

### 2026-08-15 — frozen cascade 4B ablation

With the same Qwen3.8 actor, MLX server, frozen cascading fixture, and 18-turn
budget, three conditions all repaired and independently verified the artifact
in 9 actor turns: deterministic/no 4B **170.3s**, 4B observation-only
**172.4s**, and 4B action gate **170.9s**. The observation and gated runs each
made 3 worker calls and recorded 9 stale judgments. None met the historical
three-actor-turn scorecard, but all reached lifecycle `COMPLETE` and passed the
independent grader.

This task provides no evidence that the 4B improves capability or speed. Keep
the worker as optional telemetry/critic infrastructure for future tasks, but
keep action authority disabled by default and out of the critical path. The
deterministic FSM and validation policy are the production control plane until
a frozen benchmark demonstrates a measurable worker benefit.

To make that demotion real, synchronous 4B triage is now invoked only when
`--novelty-action-critic` or `--novelty-action-gate` is explicitly enabled.
Plain `--novelty-context` records asynchronous events without waiting for or
injecting a worker judgment. This removes the worker from the default latency
path while preserving an experimental path for future ablations. Deterministic
tests and compilation remain green (68 tests).

### 2026-08-15 — bounded provider failure handling and token-endpoint capability cache

The live WebSocket benchmark exposed an engine-level latency failure rather
than a WebSocket-specific defect. After its first validation failure, the MLX
actor provider returned `RemoteDisconnected: Remote end closed connection
without response`. The retry loop treated that request-specific server close as
retryable and spent the remaining run budget repeating the same request five
times. The run ended at its 600-second watchdog with no additional mutation.

`agent.py` now classifies remote closed-response, broken-pipe, and aborted
connection errors as terminal for the current actor turn. The run exits with a
recorded model error instead of burning the whole budget on an unchanged
request. This is a transport policy, not a model or benchmark exception.

The same run showed that this MLX OpenAI-compatible server does not expose
`/tokenize`; the context manager previously retried that optional probe on every
turn. The provider capability is now cached by server root after a 404, so the
existing bounded fallback context policy is used without repeated failed HTTP
requests. Exact token measurement remains enabled for providers that expose the
endpoint.

Evidence:

```text
python3 -m unittest -v tests.test_agent_tools
Ran 67 tests in 0.147s — OK (1 skipped: pytest unavailable)
```

The next real-model check should verify that the same provider failure ends in
one bounded turn and that a healthy small request still works. Do not weaken
the independent WebSocket grader or alter the fixture to compensate for a
provider transport failure.

### 2026-08-15 — short development profile and external mini-SWE baseline

The next WebSocket run was intentionally interrupted at 501.2 seconds after
14 actor iterations to avoid spending the remaining budget. Its independent
grader passed the generated artifact, but the actor had not called
`finish_task` and exited with return code `-15`. The benchmark incorrectly
recorded `passed: true` because it considered artifact correctness alone for
tasks without the historical iteration scorecard. `run_completed` is now part
of the scorecard, and an interrupted, timed-out, or nonzero actor run cannot be
reported as a passing benchmark even when its partial artifact happens to pass.

The benchmark now has an explicit `--profile smoke` mode. It keeps the same
fixture, actor, and independent grader, but caps a development run at 8 actor
iterations, 45 seconds per model call, and 300 seconds total. `full` remains
the default for final measurements. This separates fast convergence debugging
from expensive capability measurement without changing task semantics.

As an external baseline, mini-SWE-agent v2.4.6 was run against the same frozen
WebSocket fixture using the Qwen3.8-27B MLX server. Its local-model adapter
required the absolute loaded model path in the request; an alias caused MLX to
look for a nonexistent Hugging Face repository. With the correct path, it made
10 model calls / 8 assistant turns in the short run, created `package.json`
and installed `ws`, but never mutated `server.js` or `index.html`. The
independent grader failed on the original `http://` WebSocket URL. Several
turns searched the whole filesystem or reread the trajectory, and two model
responses exceeded mini-SWE-agent's one-bash-command format. This is a useful
external baseline for analysis paralysis; it is not evidence against the
NoveltyEngine implementation.

Do not add mini-SWE-agent-specific branches to the engine. Future comparisons
should use the smoke profile first, then a full run only after a real mutation
and validation boundary are observed.

### 2026-08-15 — evidence-aware orientation gate and shell inspection guard

The first short WebSocket smoke run ended after 4 actor iterations with no
mutation. The actor had inspected both files, then the orientation recovery
surface removed `read_file`; it bypassed that restriction by issuing
`run_command cat ...` for both files. This is a generic shell/tool-plane
failure: restricting tool names does not prevent a shell tool from recreating
the restricted action.

The orientation policy now has two deterministic surfaces. Before useful
inspection evidence exists, one targeted read/search remains legal. After
evidence exists, only mutation, validation, finish, exact recall, and diff
tools remain. In addition, simple single-command file/list inspection commands
(`cat`, `sed`, `head`, `tail`, `grep`, `rg`, `find`, `ls`, and similar) are
blocked during evidence-ready orientation recovery, even when emitted through
`run_command` or `run_shell`. Pipelines, interpreters, test runners,
installers, service probes, and compound commands remain available because they
can produce behavioral evidence or perform legitimate setup.

The deterministic suite had passed 74 tests in the preceding run. The current
portable `unittest` invocation passes 72 tests because two pytest-dependent
checks are not collected in this environment. The next WebSocket smoke run must
verify the actor reaches a mutation rather than merely receiving a rejection;
the independent grader remains unchanged.

### 2026-08-15 — command-plane guard covers inline interpreter readbacks

The next WebSocket smoke trace confirmed that blocking `read_file` and simple
shell readers was not sufficient. After receiving usable source evidence, the
actor switched to two commands of the form
`node -e "console.log(require('fs').readFileSync(...))"` and printed both
application files through the command tool. It made no mutation and no
validation call. This was another generic action-plane escape hatch, not a
WebSocket-specific rule.

`lifecycle_policy.is_inspection_command()` now also recognizes a narrow class
of inline interpreter commands from Node, Python, Ruby, Perl, PHP, Bun, and
Deno when they both read a file and print it. It deliberately leaves ordinary
scripts, test/assertion snippets, process or network probes, installers, and
mutating snippets available. Dispatch rejects the command only during the
evidence-ready orientation recovery state; the shell remains available for
behavioral validation and setup recovery.

The deterministic suite passes 72 tests with one environment-dependent skip.
The next check is the same bounded WebSocket run, looking specifically for a
mutation after the first useful reads. If the model still finds a new generic
readback route, classify that route and extend the policy with a focused test
rather than adding a WebSocket-specific prompt.

### 2026-08-15 — fixed false orientation evidence from zero-line reads

The guarded run revealed why the actor remained stuck even after every shell
readback was rejected. The model had requested `read_file` with `limit: 0`.
The tool returned a non-empty header such as
`--- server.js [lines 1-0 of 27] (0 chars) ---`, and the orientation detector
mistook that header for useful source evidence. The engine then removed the
read tools even though the actor had never seen the file contents.

`_has_orientation_evidence()` now rejects zero-character and `lines 1-0`
read results. This keeps targeted reading legal until actual evidence exists;
it is a generic tool-result contract fix, not a benchmark-specific exception.
The next WebSocket run should distinguish this case from true post-inspection
paralysis. The per-request Qwen thinking probe also confirmed that this MLX
server accepts `chat_template_kwargs: {"enable_thinking": true|false}`; that
will remain an optional provider capability experiment, not a replacement for
the deterministic action policy.

### 2026-08-15 — FSM-owned orientation recovery and explicit thinking payload

The live trace after the zero-line fix showed a second architectural gap: the
actor had real source evidence, but the lifecycle still reported `ACT` while a
separate counter changed the tool list. The engine could restrict tools without
recording a state transition that made mutation mandatory.

`LifecycleFSM` now has an explicit `ACT -> RECOVER` `orientation_stalled`
transition. In `RECOVER`, a code-change contract with usable evidence exposes
only `patch_file` and `write_file`, and the llama.cpp adapter requests a tool
call. This is a generic contract-driven recovery path; diagnosis tasks are not
forced to mutate. A second-pass live run reached the new state correctly, but
the mutation-only provider call timed out, so the benchmark was not counted as
success.

The second pass also caught and fixed a missing `LifecycleState` import before
the next run. The llama.cpp adapter now sends
`chat_template_kwargs: {"enable_thinking": false}` explicitly when the agent
is not using thinking, rather than relying on the server startup default. This
keeps the provider behavior stable and leaves room for a later adaptive
orientation/mutation experiment. The deterministic suite now passes 73 tests
with one environment-dependent skip; compilation and `git diff --check` are
clean.

### 2026-08-15 — bounded WebSocket result and sampling-control smoke test

The post-FSM WebSocket run reached real mutation and independent artifact
success: first mutation at 110.6s, first validation at 186.7s, 4 mutations,
4 validations, and an artifact grader pass in 312.5s. It did not call
`finish_task`, so the benchmark exposed a completion-contract bug: the runner
previously marked a correct partial artifact as `passed` whenever the child
process exited normally.

`agentic_benchmark.py` now requires all three conditions for a pass: artifact
grader success, clean process completion, and the actor's explicit finish
signal. The same run is therefore correctly classified as incomplete rather
than fully passed. This preserves useful artifact evidence while preventing a
false capability claim.

A small direct llama.cpp A/B probe compared baseline sampling, explicit
`repetition_penalty: 1.0`, and Qwen's suggested
`presence_penalty: 1.5` plus `repetition_penalty: 1.0`. All three produced the
same valid required `write_file` call in 3.61–3.88s; the 0.27s difference is
too small to claim a real gain. No immediate tool-schema damage was observed.
The next tuning test should use repeated multi-turn repair prompts and measure
first mutation, invalid tool calls, and finish rate before adopting the
penalties as defaults.

The deterministic suite now passes 74 tests with one environment-dependent
skip.

### 2026-08-15 — command-plane mutation enforcement during validation

The latest trace exposed a remaining generic bypass. After `write_file` was
correctly unavailable during validation, the actor issued
`bash -c 'cat > index.html'` and `bash -c 'cat > package.json'` through
`run_command`. The ledger classified these as mutations, but dispatch did not
enforce the validation/setup plane or protect the files before execution.

The command classifier now handles both argv and string command forms and
recognizes redirects, `tee`, inline Python/Node writes, and common file-copy or
in-place edit forms. During ordinary validation and setup recovery, dispatch
rejects mutation-shaped shell commands with a specific command-plane message;
behavior-repair turns may still use shell mutation when the task genuinely
requires it. This preserves shell-based dependency installation and repair
while preventing a validation-only command from silently becoming a product
edit.

The deterministic suite passes 76 tests with one environment-dependent skip.
The next live run should verify that the shell writes are rejected and that the
actor is redirected to an assertion-bearing behavioral check, then either
finish or enter the normal repair state.

### 2026-08-15 — validation contract rejects source dumps as evidence

The next live run showed a validator failure independent of the command
guard. The actor used `run_command` to print `server.js` and `index.html`.
Because the printed source contained words such as `WebSocket` and `message`,
the text heuristic treated the file dump as a successful behavioral exchange
and the orchestrator called completion. The independent grader correctly
failed the unchanged frontend.

`ValidationContract.assess()` now rejects recognized read-only commands before
examining their output. The inspection classifier unwraps `bash/sh/zsh/fish
-c` wrappers, recognizes version/help probes, and preserves compound commands
as potentially behavioral. This prevents source listings, file dumps, and
environment metadata from becoming validation evidence while retaining real
client/test commands. The deterministic suite passes 77 tests with one
environment-dependent skip.

### 2026-08-15 — deterministic preflight gate before expensive runs

The validator fix was verified with 77 deterministic tests, but the broader
lesson is procedural: a real-model run should not be the first place a parser
quirk is discovered. `agentic_benchmark.py` now runs
`python -m unittest tests.test_agent_tools` before launching any task. A
preflight failure exits immediately and reports the failure; `--skip-preflight`
exists only for debugging the benchmark harness itself.

This gate is intentionally cheap and model-independent. It catches command
classification, validation-plane, completion-score, context, and dispatch
regressions before consuming a long llama.cpp run. It does not replace the
real-model benchmark, because tool selection and convergence still require a
live actor.

### 2026-08-15 — adversarial model-free preflight matrix

The short smoke run timed out at the Qwen mutation call under the 45-second
smoke cap, but it did not reach a false completion. Rather than immediately
spending another expensive model run, the preflight was expanded with an
adversarial matrix covering:

- direct and wrapped file readbacks (`cat`, `bash -c`, inline Python/Node);
- version/help and compound-command distinctions;
- redirects, `tee`, inline writes, `sed -i`, and copies;
- source text containing misleading words such as `WebSocket`, `message`, and
  `pong`;
- genuine client evidence;
- FSM illegal transitions and recovery transitions;
- the artifact/finish/process completion truth table.

The benchmark preflight now runs both `tests.test_agent_tools` and
`tests.test_adversarial_preflight`. The combined model-free gate passes 84
tests with one environment-dependent skip in 0.13 seconds. This should be the
first check before every long real-model cycle; model calls remain necessary
only for actor action selection and convergence.

### 2026-08-15 — dependency-free function-test fallback and cache isolation

The first cascading-loop run after the preflight work found a real runner
contract defect: after the actor repaired the syntax error, the workspace's
valid `test_metrics.py` used a top-level `test_calculation()` function. Since
pytest is not installed in this environment, `run_tests` returned
`Ran 0 tests: no tests discovered` and prevented the actor from seeing the
second, real TypeError.

`workspace/run_tests_tool.py` now has a dependency-free fallback for ordinary
top-level `test_*` functions. It loads matching test modules, executes the
functions through unittest's result machinery, and reports bounded assertion
or exception evidence. It does not pretend to implement pytest fixtures or
plugins; unsupported behavior fails visibly. The runner also evicts stale
temporary test and project modules after collection, preventing repeated
validation calls from observing an earlier workspace's code.

Regression coverage now passes 85 deterministic tests. This is model-agnostic
runner behavior, not a special case for the cascading benchmark. The next
real-model run should expose the TypeError after the syntax repair and allow a
second product mutation instead of misclassifying the workspace as empty.

The repeated Qwen3.8-27B run confirmed that behavior: the first validation
reported the original SyntaxError, the first mutation repaired it, and the
dependency-free fallback then reported the real `int / str` TypeError. The
actor made the second repair and independent validation passed. The run used 7
model turns, with first mutation at 35.1s, first validation at 45.7s, 2
mutations, 3 validations, and an explicit finish. The artifact and workflow
were correct, but the benchmark scorecard remained false because its separate
iteration target is 3. This is now a turn-efficiency problem, not a context or
test-discovery failure; do not loosen the scorecard or alter the frozen task
to make it pass.

### 2026-08-15 — cascading validation replay and project-module isolation

The new model-free cascade replay initially caught one more cache defect. A
previous temporary workspace had left `target` in `sys.modules`; the next
workspace imported that stale module and reported an unrelated ImportError
instead of its current SyntaxError. The runner now evicts matching Python
modules for the whole target project, including non-test modules, before
discovery and after function-style collection.

The preflight now replays a three-stage generic cascade without a model:
syntax failure, repaired syntax exposing a TypeError, and final passing
repair. It verifies that each stage reports the current failure and never
falls back to “no tests discovered.” The combined deterministic suite passes
86 tests. This gives us a cheap regression gate for both context evidence and
the validation runner before another real-model call.

### 2026-08-15 — initial failure enters the repair FSM

The live cascade completed correctly but spent three turns orienting before
the first patch. The cause was structural: the initial `run_tests` failure was
recorded as evidence, but the lifecycle remained in `ACT`, so the next actor
turn could list the workspace instead of repairing the failure already in
hand.

`LifecycleFSM` now permits the explicit `ACT -> REPAIR` transition for a failed
initial executable check. The loop promotes that failure into the same
validation/repair policy used after a mutation. Setup failures still remain on
the setup plane and cannot unlock product mutation. This is a generic
failure-driven transition, not a benchmark-specific hint.

The deterministic suite passes 87 tests, including the new FSM transition
assertion. The next live cascade will measure whether this removes the wasted
orientation turns without weakening evidence or mutation safety.

The live measurement exposed a second interaction bug: during initial repair,
`list_workspace` was marked as the one permitted inspection. The following
targeted `read_file` was then rejected, and the actor exhausted its repair
budget without seeing the source. The policy now distinguishes inventory from
evidence: `list_workspace` and `list_dir` do not consume the repair-inspection
allowance; focused readers such as `read_file`, `find_files`, and
`search_file` do. This keeps exploration available without allowing inventory
to starve failure diagnosis.

The deterministic suite passes 88 tests, including the new classification
contract. The next live run should compare first-mutation time and repair-turn
count against the prior 101.3-second / 6-repair-turn trace.

The comparison run confirmed the first fix: the actor read the implicated
files, repaired both defects, and independently passed the task. First
mutation improved to 64.1 seconds, with 2 mutations, 3 validations, and an
explicit finish. It still spent one turn on `list_workspace`, so the frozen
scorecard remained false at 7 turns, but the artifact was correct.

### 2026-08-15 — behavior repair removes broad inventory

Behavioral repair now removes `list_workspace` and `list_dir` from the offered
tool surface while preserving `read_file`, focused search, mutation, and
validation. This is not a hard-coded file or benchmark rule: once an
assertion-bearing failure identifies the product plane, broad inventory is
lower-value than targeted evidence. Setup-plane recovery retains the broader
inspection tools because the missing fact may be environmental.

The deterministic suite passes 89 tests. The next live run should show a
targeted read on the first repair turn and reduce the turn count without
weakening setup recovery.

The next live run showed that `find_files` has the same distinction as
inventory: it locates candidate paths but does not expose implementation
contents. Counting it as the one inspection allowance again blocked the
subsequent `read_file`, and the actor eventually patched only after recovery,
leaving the second defect unresolved within the budget.

`find_files` is now classified as localization rather than repair inspection.
Only tools that return source evidence consume that allowance. The
deterministic suite remains green at 89 tests. This is the third preflight
edge case found through the test cycle: inventory, localization, and actual
source inspection are separate control-plane events.

The following live run reached the intended targeted path and completed the
task correctly: `find_files` on turn 2, `read_file` on turn 3, first mutation
at 64.5 seconds, then the second repair and passing validation. It still used
7 model turns because the actor chose localization before reading. The
artifact passed and `finish_task` was called; only the frozen 3-turn efficiency
target remained unmet.

### 2026-08-15 — preserve traceback file locations in failure evidence

The remaining avoidable localization came from the validation summary itself.
The runner retained exception names and source lines but discarded traceback
frames such as `File .../target.py, line N`. The actor therefore had to search
for a file whose location was already known to the test runner.

Failure summaries now preserve bounded `File ...` frames for both unittest and
function-style fallback results. This is generic error evidence, not a
benchmark hint, and the cascade preflight asserts that the implicated source
filename survives. The deterministic suite remains green at 89 tests.

The live verification confirmed the improvement. Qwen used `read_file`
directly on turn 2, patched on turn 3, exposed the second TypeError on turn 4,
patched again on turn 5, and passed on turn 6. First mutation fell to 49.4
seconds; the artifact passed and the orchestrator completed honestly. The
frozen 3-turn scorecard remains unmet because the current outer loop permits
one model response/action phase per iteration. The remaining optimization is
therefore multi-action turn handling, not more orientation suppression or
weaker validation.

### 2026-08-15 — separate failed validation from setup failure

The first harder WebSocket run exposed a multi-file workflow problem. After
repairing `server.js`, the actor tried `ls` and `node --version`; those commands
correctly produced no behavioral evidence, but `_is_validation_setup_failure`
classified that absence as setup failure. Product mutation then stayed locked,
so the actor could not repair `index.html` and spent the remaining turns
working around its own tool restrictions.

Setup classification is now limited to actual execution-plane failures:
missing runners/dependencies, import or permission failures, test discovery
failures, and unavailable processes. A command that runs successfully but
proves nothing remains a validation failure in the product plane; the actor
can choose a real probe or continue a related product repair. Function-style
test modules that silently run as scripts remain setup failures. The
deterministic suite passes 89 tests.

The repeat WebSocket run confirmed the classification change but exposed a
recovery precedence bug. The actor reached product repair, yet after a
rejected `write_file` call the recovery policy re-added `write_file`; the model
retried the same rejected call until the bounded run ended. Recovery now keeps
the rejected mutation method removed and explicitly directs the actor to the
remaining `patch_file` path. Deterministic coverage passes 90 tests.

The next WebSocket run confirmed that recovery could now reach `patch_file`:
the actor repaired `index.html`, but the run ended before a behavioral check
and the independent grader still found stale protocol code. The trace shows
the deeper workflow issue: the first `server.js` mutation was forced through
validation before the related `index.html` mutation could land. The actor then
had too little budget to recover from a partial first patch.

### 2026-08-15 — bounded multi-file change batch

The validation policy now permits one additional related product mutation
after a successful write, while keeping executable validation mandatory before
any further edits. The allowance is consumed once, is not available after a
failed validation, and remains separate from shell mutation guards and test
path protection. This gives multi-file tasks a coherent change point without
opening an unrestricted edit loop. Setup recovery and behavior repair rules
remain unchanged.

The deterministic suite passes 91 tests. The next WebSocket run is the live
verification of this batch contract.

The batch run reached the second mutation as intended: it wrote both
`server.js` and `index.html` before validation. That live run exposed a missing
FSM edge: `VALIDATE -> mutation` was not legal, so the orchestrator crashed
before the behavioral check. The FSM now accepts this one bounded batch event
and remains in `VALIDATE`, where executable evidence is still required. The
deterministic suite passes 92 tests. The grader also identified the next
product/setup requirement for this harder task: the workspace needs a
`package.json` declaring `ws`; the next live run will verify how the actor
handles that dependency contract.

The batch run reached `server.js` and `index.html` as intended, then exposed
that the WebSocket task also explicitly requires a third artifact,
`package.json`. The bounded batch allowance is now two follow-up mutations
after the first successful write, enough for an implementation, client, and
dependency manifest while still forcing validation immediately afterward. The
deterministic suite remains green at 92 tests.

The next live run successfully produced all three required artifacts and the
independent grader passed them. It stopped before `finish_task` because the
successful `npm install` command was incorrectly treated as a failed
behavioral check, consuming a repair turn and leaving the final smoke call to
time out.

Dependency installation is now an explicit setup event. Successful npm/pnpm/
yarn/pip installation is never accepted as behavioral evidence, but it also
does not enter product repair; the next turn remains focused on the required
smoke test. Deterministic coverage passes 93 tests.

The final WebSocket handoff run confirmed all three artifacts and a passing
independent grader, but the actor returned prose for five consecutive turns
while saying it would write a smoke test. The validation tool surface contained
only executable checks, while the novelty worker continued recommending
`patch_file`; this mismatch prevented the model from selecting `run_command`
and calling `finish_task`.

No-action recovery is now lifecycle-aware. It receives the legal tool names for
the current turn, recommends validation commands during validation, and
recommends mutation only during repair. Its generic directive no longer names
an unavailable tool. The deterministic suite passes 95 tests, including the
novelty-context tests.

### 2026-08-15 — make temporary validation probes executable without file mutation

The next live WebSocket run reproduced a separate, model-independent tool
contract edge case. After the actor had repaired all three artifacts and
installed `ws`, it correctly decided that a real client/server smoke test was
needed. It then repeatedly said it would write a temporary smoke-test file.
Validation intentionally exposes only executable checks and forbids product
file mutation, so the actor had no legal way to carry out that plan. The
recovery directive recommended `run_command`, but did not explain that an
inline `node -e` or `python -c` probe was the intended replacement.

Validation prompts now explicitly direct temporary probes through an inline
shell command and forbid creating helper files during validation. This keeps
the setup/product/evidence boundary intact while making the existing shell
surface sufficient for short-lived probes. The preflight now also includes the
novelty-context test module, so changes to the worker/recovery layer cannot
skip the cheap deterministic suite. The full targeted suite passes 96 tests
and the benchmark preflight passes before any model call.

The interrupted live run still had a correct artifact and an independent
passing grader, but no `finish_task`; it is recorded as incomplete rather than
counted as a success. The next run should verify whether the actor converts
the same smoke-test plan into an inline command and then completes the
handoff.

### 2026-08-15 — bound transient model-provider disconnect recovery

The verification rerun followed the intended path through the combined
application repair, dependency installation, server launch, and process
status check. The MLX endpoint then closed one HTTP connection before the next
actor turn. A direct provider health request succeeded afterward, and the
same failure repeated on the next run at the same boundary. This is distinct
from context overflow: the request was within the measured context budget and
the model server stayed alive.

The actor loop now retries only the specific transient disconnect signatures
(`RemoteDisconnected`, a remote end closing without a response, or a peer
reset) once, with a one-second delay. Refused connections, DNS failures, and
the second disconnect remain terminal, so a dead provider cannot create an
overnight retry storm. The deterministic suite passes 97 tests. The next live
run verifies whether this recovers the final smoke-test turn.

### 2026-08-15 — prevent malformed multiline shell tool calls at the provider boundary

The bounded retry correctly caught the transient disconnect, but the retry
failed for the same underlying reason. The MLX server log showed that Qwen's
tool parser received a `run_command` argument containing literal newlines in a
multiline `node -e` smoke test. Its parser raised `SyntaxError: unterminated
string literal` while converting the tool call, closed the HTTP connection,
and left the model server healthy. This was a serialization failure, not a
context-size failure.

The command tool contract now rejects literal newline characters in argv
tokens and explains that short probes must be one-line commands. Validation
guidance carries the same rule, while retaining the option to create a proper
helper during product mutation when that is actually part of the task. The
deterministic suite adds coverage for the command boundary and passes 98
tests. This is model/provider agnostic at the agent layer: any provider that
cannot safely encode multiline tool arguments gets a bounded, explicit error
instead of crashing its transport.

### 2026-08-15 — allow only dependency manifests during setup recovery

After the multiline parser fix, the live run reached the dependency phase
without an HTTP crash. It discovered `node_modules/ws` was absent and correctly
classified the check as setup, but the setup-plane mutation freeze also hid
`write_file`/`patch_file`. The actor therefore could not create the missing
`package.json`; it spent the remaining turns retrying an illegal product/setup
path and the grader eventually passed only because the source artifacts were
already correct.

Setup recovery now exposes mutation tools with a narrow deterministic path
allowlist. It may create or update conventional dependency manifests
(`package.json`, `requirements*.txt`, `pyproject.toml`, `Cargo.toml`, and the
equivalent ecosystem lock/manifests), while dispatch rejects product code,
tests, and lookalike files. Shell commands remain mutation-blocked in setup
recovery. The full deterministic suite passes 99 tests, and the next live run
will verify the intended sequence: manifest mutation, dependency install,
behavioral smoke test, and explicit completion.

### 2026-08-15 — give validation a safe helper-file lane

The next run showed a second setup/validation interaction. The model emitted a
single-line command, but the command was too deeply nested for the MLX parser's
Python-literal fallback: embedded JavaScript quotes and arrays made the tool
argument invalid. The model could have avoided this entirely by writing a
small probe file, but ordinary validation exposed no write tool.

Validation now offers `write_file` only as a temporary-probe capability. The
dispatch allowlist permits it only below `.agentic/`; dependency setup may
also write only an approved dependency manifest. A helper write does not count
as a product mutation or reopen the product-edit FSM, so the next legal action
remains execution of the helper. Product code, supplied tests, and arbitrary
paths remain frozen. The deterministic suite remains green at 99 tests.

### 2026-08-15 — feed independent verifier failures back into the agent

The helper lane enabled a complete live runtime check: dependency installation,
server launch, ping/pong, message broadcast, peer disconnect, and server
survival all passed. The actor called `finish_task`, but the independent grader
still rejected the unchanged client artifact. This exposed a validation-layer
gap: an agent can satisfy its own behavioral probe while missing a static
acceptance condition.

The benchmark harness now performs one bounded external-verifier repair pass
when a clean actor run is rejected. It reuses the same workspace, injects the
verifier's exact failure text as evidence, runs the normal agent/FSM again, and
regrades the artifact. It does not interpret the task, synthesize a
task-specific patch, or modify the grader, and it never retries the verifier
indefinitely. This is the generic handoff contract needed for any independent
test harness. Deterministic coverage is now 100 tests and preflight passes.

The first verifier-repair run fixed the stale client artifact and passed the
grader, but the repair actor did not issue a second `finish_task`; the harness
had incorrectly inherited the first run's finish signal. The scorecard now
requires the repair pass itself to finish whenever verifier feedback starts a
repair. Its prompt also tells the actor not to invoke the generated grader
directly—the harness runs it after handoff—keeping the repair budget focused on
the reported evidence.

### 2026-08-15 — expand model-free adversarial preflight coverage

The deterministic sweep found four command-plane edge cases without using a
model call: `sed -i` and shell redirection could be mistaken for inspection,
JavaScript `writeFileSync` was missed by a case-sensitive mutation check,
`perl -pi` was missed, and `readFileSync` was incorrectly treated as a write
because it shares a substring with `writeFileSync`. It also found that
`echo "assert passed"` and `printf "connected"` could look like behavioral
validation merely because their text contained assertion words.

The command classifiers now reject obvious writes from the inspection path,
recognize common interpreter writes case-insensitively, preserve interpreter
reads as observation, and classify output-only commands as non-evidence. New
adversarial preflight cases cover these combinations. The targeted deterministic
suite and benchmark preflight pass 104 tests. This is the intended fast gate:
probe control-plane invariants and representation boundaries locally first,
then spend real-model calls only on behavior that cannot be simulated safely.
The shared output-only classification lives in `lifecycle_policy.py` so the
action governor and validation contract cannot silently drift apart.

### 2026-08-15 — preserve completion after accepted evidence

The WebSocket run exposed a completion-plane edge case. The actor produced a
real smoke-test result with every client/server assertion passing, but a later
blocked attempt to inspect the generated grader was treated as a new product
repair failure. The repair policy then removed `finish_task`, so the actor
could not hand off the already-verified workspace and spent its bounded turns
on an irrelevant grader loop.

The validation policy now carries whether accepted behavioral evidence already
exists. If a subsequent failure is only a tool-plane restriction, `finish_task`
remains legal and the prompt directs the actor to hand off rather than rewrite
product code. Rejected validation evidence is also logged with its reason and
next action, making this boundary diagnosable in the live monitor. The
deterministic suite and preflight pass 105 tests.

### 2026-08-15 — keep package-manager setup separate from file mutation

The first live confirmation of the previous change found a false positive:
the mutation regex treated the word `install` in `npm install ws` as a file
write. That blocked the normal dependency setup path before the WebSocket
smoke test could run. The classifier now recognizes only the Unix `install`
file-copying utility as a mutation; npm, pip, and similar package-manager
commands remain setup commands. A regression matrix covers both forms. The
deterministic suite and preflight now pass 106 tests. The interrupted live run
was discarded because its failure was fully explained by this deterministic
classifier bug.

### 2026-08-15 — ignore verifier traceback paths during contract extraction

The next confirmation found a separate parser edge. Independent verifier
feedback included an absolute temporary path such as
`/private/var/.../.agentic_grader.py`. The task-derived validation contract
mistook that filesystem path for a required application endpoint, so a passing
WebSocket smoke test was rejected while the actor chased a nonexistent HTTP
interface.

Endpoint extraction now excludes conventional temporary/home/workspace paths
and `.agentic` artifacts, including method-prefixed forms. A regression test
replays verifier traceback text and confirms that it creates no endpoint. The
deterministic suite and preflight pass 107 tests.

### 2026-08-15 — distinguish JavaScript arrows from shell redirects

The handoff-recovery probe found one more command-plane representation bug:
the shell mutation regex saw the `>` in JavaScript arrow functions (`=>`) as
a file redirect. That blocked a legitimate inline Node behavioral probe even
though it never wrote a file. The redirect pattern now ignores `=>` and other
operator contexts while retaining real `> file` and `>> file` writes. A
regression case covers an inline `setTimeout(() => ...)` command. The
deterministic suite and preflight pass 108 tests.

### 2026-08-15 — reconcile verifier-confirmed provider termination

The final live attempt repaired the artifact and the independent grader passed,
but the model provider disconnected twice before the repair actor could call
`finish_task`. The benchmark previously recorded that as a total failure even
though the only authoritative artifact check was green.

The scorecard now records `finish_called` separately from
`handoff_reconciled`. It accepts the narrow combination of: independent
verifier pass, clean actor-process termination, and an explicit provider-loss
signature. It never treats a missing artifact, timeout, or ordinary model
stall as reconciled, and it does not pretend the model called `finish_task`.
The deterministic suite and preflight pass 109 tests.

### 2026-08-15 — require a tool call after repeated prose-only turns

The first harder `real_app` run revealed a generic action failure: Qwen
returned three consecutive long responses with no tool call, no mutation, and
no validation. The engine appended increasingly explicit prose reminders, but
that remained advisory and consumed roughly five minutes without touching the
workspace.

The loop now counts consecutive no-action turns and, after two, sends the
llama.cpp request with `tool_choice: "required"`. The counter resets after any
real tool call; it is independent of model name and task wording, and applies
only at the provider boundary that supports this structured control. The
deterministic suite and preflight pass 110 tests. The interrupted real_app run
is retained as evidence of the failure, not counted as a product result.

### 2026-08-15 — give forced tool turns enough generation headroom

The next real_app attempt showed that `tool_choice: "required"` alone did not
solve the no-action stall. Qwen's llama.cpp server log reported a truncated
tool-call parse, and the actor repeatedly returned at the output limit without
an executable call. Forced-action requests now use a 4096-token response
reserve, while ordinary turns keep their existing budget. This changes only
the transport envelope for recovery/action-first turns; it does not encode the
Todo task or a model name. The deterministic suite and preflight remain green
at 110 tests.

### 2026-08-15 — recover command-plane failures without patching the product

The following real_app run found a generic FSM error. A valid `/health` probe
was correctly rejected as incomplete evidence, but the actor then emitted a
malformed or currently unavailable `run_command` call. Because that dispatch
error was treated as a product validation failure, repair recovery narrowed the
tool surface to `patch_file`, `diff_files`, and `finish_task`; the actor then
made an unnecessary behavior-preserving patch to `server.py` and repeated the
stale repair checkpoint.

The validation contract now classifies unavailable-tool, bad-argument, and
unknown-tool errors as command-plane failures. The loop reopens executable
validation tools, clears product-repair mode, and explicitly tells the actor to
use the declared command schema without changing product code. A regression
test covers both malformed-tool errors and real assertion failures, so only the
former take this path. The deterministic suite and model-free adversarial
preflight pass 111 tests. The affected real_app run was stopped after the
failure was isolated; its workspace and monitor remain evidence, not a score.

### 2026-08-15 — do not treat printable arrows as shell redirects

The next confirmation reached the new command-plane recovery correctly, then
found another deterministic guard false positive. A legitimate inline Python
behavioral probe printed `PASS: ... -> ...`; the shell mutation detector saw
the arrow's `>` as a file redirect and blocked the entire verification command.

The redirect pattern now ignores `->` in addition to JavaScript `=>`, while
continuing to classify real `> file` and `>> file` writes as mutations. A
regression test covers a Python probe containing the printable arrow. The
interrupted live run is not scored; the model-free preflight will be rerun
before the next real-model attempt.

### 2026-08-15 — parse redirects instead of scanning raw command text

Static adversarial probing found two more guard false positives: `assert 2 > 1`
and HTML such as `<div>ok</div>` were classified as file writes. The common
cause was treating every `>` character in a command as a shell redirect,
including characters inside quoted Python/JavaScript code.

The command classifier now tokenizes shell punctuation with `shlex`. It sees
real `> file` and `>> file` operators, ignores quoted comparisons/markup/arrows,
and retains interpreter write detection such as `open(..., 'w')`. Regression
coverage now includes comparisons, markup, JavaScript strings, and a genuine
redirect. The model-free preflight is the required gate before the next live
run.

### 2026-08-15 — recover malformed command arguments as transport errors

The next live confirmation exposed one more instance of the same class: the
actor sent a multi-line argv argument and the execution tool rejected it before
launching anything. The command-plane classifier now also recognizes
single-line/schema/option errors as transport failures, so they reopen the
validation surface without entering product repair. A regression test covers
the multi-line argument rejection.

One follow-up check caught an interaction between the new parser and the
output-only shortcut: `echo ok > result.txt` was initially recognized as
output-only before the redirect guard ran. Output-only classification now
defers to the shared file-mutation detector, so redirected `echo`/`printf`
commands remain mutations. The deterministic suite and preflight are the gate
for the next live attempt.

### 2026-08-15 — allow multiline argv probes safely

The next live run showed that the model could produce a valid multiline Python
probe, but `run_command` rejected it and returned the actor to repair. That
restriction was unnecessary: `run_command` executes an argv list without a
shell, so newlines inside one argument cannot become redirects or shell code.

The command boundary now accepts multiline interpreter arguments while keeping
the shell-free argv semantics. Guidance still prefers short one-line probes for
provider compatibility, but a valid multiline probe is no longer converted into
a false product/validation failure. The deterministic suite and preflight are
the gate for the next live attempt.

### 2026-08-15 — separate weak probes from product failures

The final live confirmation built a working app and exercised health, HTML,
creation, and collection behavior, but the probe did not explicitly assert the
required JSON response shapes. The validator correctly rejected that evidence;
the old repair packet nevertheless told the actor to mutate the implementation,
and the actor spent turns checking process status instead of strengthening the
probe.

The contract now has a deterministic probe-quality classification. Missing
response-shape/assertion evidence reopens behavioral validation without entering
product repair, and process-status/cleanup tools are removed from ordinary
validation turns once execution is underway. Real assertion failures still use
the product-repair path. This prevents correct artifacts from being rewritten
because the test itself was too weak.
