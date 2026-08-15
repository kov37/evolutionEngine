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
