# Novelty Engine Handoff

Updated: 2026-08-17

This file is the active handoff for NoveltyEngine, an agentic coding-benchmark
harness: a local model calls tools (read/edit/patch/run_tests/finish) against
a task in an isolated workspace, and an independent grader — never the model's
own claim — decides pass/fail. This version has been aggressively trimmed:
older day-by-day debugging narratives for retired conditions were compressed
into short changelog bullets (the fix, not the blow-by-blow), while the most
recent (2026-08-17) work is kept close to verbatim. Git history has the full
prior narrative if a past incident ever needs to be re-read in detail.

## Current state (2026-08-17)

- **Actor policy: single-actor baseline only.** One Qwen3.8-27B Q4_K_M GGUF
  model via llama.cpp on `127.0.0.1:8080`. There is **no** 4B worker, **no**
  novelty/action-critic/action-gate multi-agent condition, and **no** MLX
  backend in current use. Ollama may be running but is not used by these
  runs. Everything below that mentions `--condition novelty`, a 4B worker, or
  MLX is historical: it explains why a generic mechanism exists, not a
  currently-exercised code path.
- **`swebench_runner.py`** is the current benchmark entrypoint — an instance
  registry, not a SymPy-only script. Registered instances: `django__django-14034`
  and `sympy__sympy-13878`. `--mode baseline` matches the single-actor policy.
- **`agentic_benchmark.py`** is a separate, older harness with its own task
  set (`lru_cache`, `bug_repair`, `real_app`, `websocket_chat`,
  `cascading_loop`, `multi_file_transaction`, `3d_scene`, `wifi_simulator`,
  `feature`, `data_report`, `recovery`, `dependency-graph`). Most of these
  were exercise beds for the retired novelty/4B condition and are not part of
  the current benchmark cycle. `lru_cache` and `bug_repair` reruns on
  2026-08-17 (baseline condition, correct interpreter) are still current —
  see below.
- Deterministic suite as of this read: `.venv-swebench/bin/python -m pytest -q
  tests` → **252 passed, 38 subtests passed, 1 warning**. Trust the doc's own
  recent claims as a baseline; older test counts quoted in the changelog below
  are the count *at that historical point*, not the count today.
- **In flight:** a django-14034 baseline run using `--editor edit_range` for
  the first time is running in the background to check whether the turn-2
  block-mismatch loop (see below) is actually resolved. Its outcome is not
  yet known — do not report a result for it until it reports.

## Operating instructions for the next agent

Working directory: `/Users/digitialchameleon/noveltyEngine`, branch
`noveltyEngine`. Read this file before changing code. The worktree is
routinely left intentionally dirty with in-progress work — inspect `git
diff`/`git status` before editing or resetting anything; do not discard
uncommitted changes.

**First commands to run:**

```bash
cd /Users/digitialchameleon/noveltyEngine
git status --short
git log -1 --oneline
python3 -m py_compile agent.py swebench_runner.py agentic_benchmark.py
./.venv-swebench/bin/python -m pytest -q tests
```

If these fail, fix the failure before starting a model benchmark.

**Running a benchmark.** Check for an already-running server before starting
one (`pgrep -fl 'llama-server|agent.py|swebench_runner.py|agentic_benchmark.py'`,
`curl -fsS http://127.0.0.1:8080/health`). Example current-condition run:

```bash
./.venv-swebench/bin/python swebench_runner.py --instance django__django-14034 \
  --mode baseline --editor edit_range --thinking --chat-timeout 240
```

Monitor from another terminal: `tail -f state/benchmark/runs/<run_id>.log` and
`state/benchmark/agentic/monitor-*.jsonl`. A run is complete only when its JSON
result is printed and the independent grader result is recorded — a timeout is
an infrastructure result, not a model-quality result, and `finish_task` alone
is never evidence of success.

**Run-monitoring rule.** Every live benchmark run must be actively monitored
and reported: state the task, mode/condition, backend, and iteration budget
before starting; poll for milestones and stalls while it runs (iterations,
tool calls, mutations, validations, errors); report the independent grader
result and final metrics when it ends. Do not launch a long run without an
active monitoring loop, and do not walk away from one silently.

**What not to do:**

- Do not add model-, task-, or benchmark-specific branches to the engine
  (no Qwen-, Devstral-, Todo-, SymPy-, or Django-specific logic in the
  generic control plane).
- Do not treat a model's own success claim, or a 4B/worker judgment, as
  authoritative — local deterministic evidence and the independent grader win.
- Do not remove or weaken the independent grader, or weaken an assertion, to
  make a run pass.
- Do not launch an uncapped overnight benchmark.
- Do not reset, checkout, or delete existing worktree changes without
  understanding what they are.
- Do not commit benchmark output blindly without inspecting what changed, and
  do not commit unless asked.

**Definition of a useful improvement.** A change is useful only if it
improves independently verified completion, repair convergence, or context
efficiency across more than one task shape — not one lucky run. Minimum
evidence is a deterministic regression test (that fails before the fix) plus
a paired benchmark comparison (same fixture, change on vs. off). Record
iterations, mutations, validations, repair transitions, duplicate actions,
elapsed time, and the independent grader outcome — not just pass/fail.

**Methodology for every change** (condensed from the 2026-08-15 phased plan,
still the right discipline): state one generic failure mode → change one
model-agnostic host mechanism → add a regression test that fails before the
change → run the deterministic preflight → run a frozen task with the change
→ run the same task with the component disabled → compare outcome,
iterations, first-mutation time, validations, total time → repeat on a second
task shape → record verified vs. unverified evidence here.

### llama.cpp server tuning (current actor backend)

The Qwen3.8-27B Q4_K_M GGUF actor server should run with GPU offload
(`--n-gpu-layers 99`), Flash Attention, MTP draft decoding (`--spec-type
draft-mtp` — confirm draft acceptance in the server log, don't assume it),
Q8 KV cache, and `--load-mode mlock` (the older `--mlock` spelling means the
same thing). Measured on the M4 Max with these flags: ~181-186 prefill
tok/s and ~15-18 decode tok/s, with roughly 74% MTP acceptance on large
prompts. Context size is capacity, not a speed lever in this setup — 8K,
16K, and 32K measured within ~1 tok/s of each other on the same prompt; use
16K when a task fits and 32K only when the agent actually needs the room. A
pure-CPU server (`--device none`) is much slower (~90s/turn observed) and
should be avoided.

## Architecture, plain English

NoveltyEngine is a host-controlled coding agent, not a model given a free
shell. The actor (currently the single Qwen3.8-27B baseline model) proposes
tool calls; it does not get to decide its own code is correct. The Python
host is the control plane: it validates each call, executes it in an
isolated task workspace, records the event, and runs verification at
controlled points. A bounded factual packet is sent back to the actor instead
of an unbounded raw transcript.

Authority is deliberately ordered: **host evidence and dispatch rules >
deterministic FSM and grader > actor tool proposal > actor's natural-language
success claim.** (A 4B advisory worker used to sit between the actor and its
own claim; it is not part of the current condition.)

Layers, and the module that owns each:

1. **Benchmark/workspace** — `swebench_runner.py` (current) creates an
   isolated instance workspace from dataset metadata, applies the base
   commit, and invokes the independent grader; `agentic_benchmark.py` (older
   harness) does the same for its own frozen fixtures.
2. **Actor/tools** — `agent.py` exposes read/edit/write/execute/finish tools
   with strict Pydantic contracts (`tool_contracts.py`, `extra="forbid"`).
   A tool call is a request, not an unrestricted command; `dispatch.py`
   checks names, paths, lifecycle state, budgets, and repeated/rejected
   mutations before anything executes.
3. **Lifecycle/FSM** — `lifecycle_fsm.py` / `lifecycle_policy.py` track
   `ORIENT → ACT → VALIDATE → REPAIR → RECOVER → COMPLETE/FAILED` as an
   explicit table of legal transitions; invalid transitions raise
   `InvalidTransition` instead of silently falling through. Setup/dependency
   failures and genuine product/behavior failures are a first-class separate
   plane: a missing runner or dependency can never unlock speculative product
   mutation, and only an actual assertion/behavioral failure enters repair.
4. **Validation/grading** — `validation_contract.py` derives a task-grounded
   acceptance contract (interfaces, typed fields, method+path-aware coverage)
   from the task text; `independent_grader.py` records `PASS`, `FAIL`,
   `TIMEOUT`, or `ENVIRONMENT_INVALID` and is the sole authority — a model
   claim or `finish_task` call is never proof by itself.
5. **Failure evidence** — `validation_packet.py` / the test runner
   (`workspace/run_tests_tool.py`) select one bounded, actionable failure with
   confined paths, exit code, primary error, `File:line` frames, and
   assertion diff operands — provenance, not an LLM diagnosis.
6. **Transaction/recovery** — `transaction_buffer.py` and `risk_layer.py`
   track in-flight multi-file changes and snapshot pre-mutation content;
   destructive rewrites (>65% of an existing file's lines removed) are rolled
   back automatically; a rejected mutation cannot be replayed byte-for-byte.
7. **Working memory** — `working_memory.py` (opt-in, `--working-memory`) is
   the current failure-tracking state machine: see the 2026-08-17 section
   below.
8. **Telemetry** — each run writes to `state/benchmark/agentic/results.jsonl`
   and `state/benchmark/agentic/monitor-<task>-<condition>-*.jsonl`; the
   swebench runner additionally writes a full actor transcript to
   `state/benchmark/runs/<run_id>.log`.

Key files: `agent.py` (actor loop + lifecycle), `swebench_runner.py` (current
SWE-bench entrypoint/instance registry), `agentic_benchmark.py` (older
fixture harness), `dispatch.py` / `tool_contracts.py` (tool surface and
validation), `lifecycle_policy.py` / `lifecycle_fsm.py` (state machine),
`validation_contract.py` / `validation_packet.py` (task-derived acceptance
and failure evidence), `independent_grader.py` (grading, isolated from the
actor workspace), `working_memory.py` (failure state board),
`kernel/io_tools.py` / `kernel/exec_tools.py` (read/write/execute
primitives), `workspace/run_tests_tool.py` (test execution and evidence
extraction), `registry.py` (tool registry/manifest).

## 2026-08-17 — line-anchored editor and reproduce-first phase

### `edit_range`: batched line-anchored editing

New model-facing editor addressing the largest measured waste source in the
django series: block-mismatch rejections started on turn 2 of every run
(before any drift existed) and consumed ~40-45% of all iterations across
222 logged turns (45 mismatches x ~2 turns each).

`edit_range(path, edits=[{start_line, end_line, replacement, expect?}])`:

- line anchors come from read_file's window headers — no verbatim quoting;
- edits apply bottom-up atomically, so all line numbers stay valid within
  one batch (kills multi-section drift and micro-edit ping-pong);
- an optional short `expect` token verifies the region; on drift the whole
  batch is rejected with the actual region text (a self-correcting
  re-read);
- a range may span a whole function — cohesive rewrites stay one move.

Registered in the model surface via `--editor edit_range` (registry swaps
patch_file out; both stay host-callable). Strict Pydantic contract with
`extra=forbid`. Kernel coverage: batch merge, expect rejection, stale
bounds, syntax rejection, atomicity. Deterministic suite: `252 passed`.

### `--reproduce-first` (TDD red phase)

Product mutations on existing files are locked until a genuinely failing
execution demonstrates the bug (a passed check or rejected tool call is not
evidence; creating the reproduction script itself stays legal). The control
block shows the REPRODUCE phase contract; evidence unlocks mutations with a
transcript marker. Off by default; the swebench runner exposes
`--reproduce-first`.

### Delivery-nudge dead code revived

The probe runs exposed that `completion_nudge_pending` was never armed
anywhere (render+consume only), so models with accepted validation evidence
could re-validate until budget instead of being told to finish. It now arms
when a passing validation covers all task-required evidence; the COMPLETE
control block outranks a still-open validation phase, and the nudge message
is no longer suppressed while validation is open.

### Research-backed editor refinements

Probe findings plus the literature review produced five refinements:

- Snapshot-hash anchoring (Hashline pattern): read_file headers carry a
  `[snapshot <hash12>]` token; edit_range accepts it and rejects the whole
  batch if the file changed since the read; responses carry the new
  snapshot so chained edits stay anchored.
- patch_file ambiguity guard: a block occurring more than once is rejected
  with occurrence count and line numbers instead of silently replacing the
  first match (the corruption risk documented in the field).
- Shell side-channel closed: python -c writes via shutil.copy/copyfile/
  copy2/move, os.replace/rename, and write_bytes now classify as MUTATE.
- write_file rejection names both editors instead of a hard-coded
  patch_file.
- swebench_runner accepts `--editor` for the same A/B surface.

Deterministic suite: `252 passed, 38 subtests passed, 1 warning`.

## 2026-08-17 — working memory Phase 1 (failure state machine)

New module `working_memory.py`, opt-in behind `--working-memory` in agent.py
and `swebench_runner.py` (off by default; every phase stays A/B-able).

Design (locked in the spec review with the user):

- Failure identity = normalized content fingerprint (exception type +
  assertion core; numbers/hex/paths stripped) scoped to a validation
  target epoch. Locations are metadata, never identity. Target identity is
  normalized (verbosity flags dropped); a target switch starts a new
  comparison epoch but preserves history so switch-loops stay visible.
- Two stagnation signals, tracked separately: `cycles_unchanged` on the
  active fingerprint and `mutations_since_last_validation` (blind editing),
  each with event counters for run telemetry.
- Goals derive from the task's validation-contract interfaces when present,
  else a single mechanical `acceptance` goal. Transitions
  (`unverified->failing`, `failing->passing`) are recorded facts only —
  the renderer is banned from semantic progress claims ("improved",
  "closer", "likely").
- Renderer: priority-ordered token budget (~900 chars) — unresolved goals
  and current failures are lossless; edit concentration drops first. The
  board re-renders from state every turn and after every checkpoint
  rebuild; context surgery cannot erase it.
- Run-end telemetry: `🧠 [working memory metrics]` with target switches,
  per-target cycles/failures/resolved, stagnation events, blind-edit
  events, mutation totals.

Deterministic suite: `241 passed, 38 subtests passed, 1 warning`
(15 new tests in `tests/test_working_memory.py`). Phase 2 (coverage,
edit concentration, ranked localization, host-enforced stagnation
inspection surface) and Phase 3 (compressed decision history) are
deliberately not started yet.

Two loop-hygiene fixes landed with the first experiment run:

- The board is now printed to the transcript each turn
  (`🧠 [working memory] ...`) in addition to the prompt message, so runs
  are analyzable from logs.
- One chat timeout no longer ends the run: the loop retries once inside
  the same logical turn (the existing bounded retry policy), so a single
  slow provider turn cannot kill a 60-iteration run. The first
  working-memory django run died this way at iteration 5 before any
  mutation, with zero board events accumulated.

### Working memory Phase 1 experiment result (django-14034)

The first full experiment run (60 iterations, 9/10 graded) produced two
findings:

1. The board worked mechanically (goals, transitions, cycles_unchanged,
   STAGNATION from cycle 3, edit concentration) but the failure
   fingerprint was degenerate: the delegated unittest summary carries a
   traceback location with no extracted exception line, so every failure
   collapsed into "Failure: traceback". `normalize_fingerprint` now falls
   back to the runner's own summary error label so distinct failures stay
   distinct.
2. The model ignored the displayed stagnation signal for 40+ iterations
   and kept the identical behavior — empirically confirming that board
   text alone does not break fixation. Phase 2's host-enforced inspection
   surface is the required intervention.

### Working memory Phase 2 (coverage, localization, enforced stagnation)

- Mechanical read telemetry: per-file `reads` and `relevant_reads`
  (whether the read content contained the active failure's tokens — an
  exposure claim, never a comprehension claim).
- Ranked localization: failure-token search over bounded text files
  (noise dirs skipped, test-owned paths excluded); identifier matches
  rank above filename matches; capped at 5. Rendered as
  `UNINSPECTED candidates implicated by the failure tokens` when the
  active failure is stagnant.
- Stagnation policy enforcement: while the same failure is unchanged,
  a mutation on a file that already has edits is REJECTED at dispatch
  with a policy message, and the rejected-mutation recovery mechanism
  forces one bounded inspection before another edit. Edits to files with
  zero edits (e.g. an uninspected candidate) remain legal — the model
  keeps agency over which file, the host constrains the action.
- Board additions: `STAGNATION ACTION` policy line and per-file
  `(read Nx, relevant Mx)` coverage on the edit-concentration line.

Deterministic suite: `246 passed, 38 subtests passed, 1 warning`
(20 working-memory tests).

## 2026-08-17 — single-actor classification and SWE-bench instance registry

### Grader environment classification

Three lru_cache runs failed at the independent grader with
`python3.14: No module named pytest` because the benchmark was launched with the
host interpreter instead of `.venv-swebench/bin/python`. The baseline contract
had counted that environment failure as a valid product baseline, so the model
burned full budgets on a doomed environment.

`independent_grader.py` now classifies a checker failure whose cause is a
missing import of a module that is not a task product file (per the task's
setup modules or the workspace itself) as `ENVIRONMENT_INVALID` instead of
`FAIL`. Consequences: the baseline gate refuses the run before any model call;
the acceptance phase records `ENVIRONMENT_INVALID` honestly; `agentic_benchmark.py`
skips the verifier-repair pass on `ENVIRONMENT_INVALID` because a host
environment gap is not actor-repairable.

Deterministic verification: `213 passed, 38 subtests passed, 1 warning`. New
coverage is in `tests/test_adversarial_preflight.py`.

### lru_cache rerun with the correct interpreter

`./.venv-swebench/bin/python agentic_benchmark.py --task lru_cache
--condition baseline ... --model qwen3.8-27b --backend llama-cpp`:

- acceptance grader: PASS; 5 iterations; 7 tool calls; 2 repair mutations; 1
  successful repair cycle; clean `finish_task`; 147.1s; iteration target met;
- hidden shadow grader: FAIL — the actor satisfied the visible suite but did
  not implement the eviction-hook firing or TTL cleanup semantics, which no
  visible test exercises. By design the shadow detail never reaches the actor.

Trajectory was test-first and focused. The remaining gap is hidden-semantics
coverage, which is exactly what the shadow layer measures; no task-specific
hint was added.

### SWE-bench instance registry and django__django-14034

`swebench_runner.py` is now an instance registry instead of a SymPy-only
script. `--instance django__django-14034` is added with:

- base commit `db1fc5cd3c5d36cdb5d0fe4404efd6623dd3e8fb` extracted to
  `assets/benchmarks/django-14034`;
- a dedicated Python 3.10 venv at
  `assets/benchmarks/django-14034/.venv-django` (Django 4.0 does not support
  the host 3.14) with Django installed non-editable; the runner prepends the
  workspace to `PYTHONPATH` so the candidate source shadows the installed copy;
- grader applies the dataset test patch to a separate copy and runs
  `tests/runtests.py --verbosity 2` over the parsed FAIL_TO_PASS/PASS_TO_PASS
  labels. Probe result: 10 runnable tests, base commit fails the F2P test as
  required.

The problem statement, test patch, and test lists come only from the dataset
metadata; the harness carries no task-specific answer. `--mode baseline`
matches the single-actor policy.

### django__django-14034 loop-fix saga (condensed changelog)

A first 15-iteration smoke run ended 9/10 graded: the actor fixed the
sub-field validation semantics but the required-attribute rendering still
failed, and ~8 of its final turns were consumed by a recovery loop — after a
`patch_file` block-mismatch rejection, the one bounded recovery inspection
was repeatedly satisfied by `list_symbols`, which cannot supply exact block
text, so each following patch was guessed from memory and rejected again.

Across the next several runs, each surfaced and fixed one generic loop
defect, all still load-bearing today:

- **Exact-read requirement.** A block-mismatch rejection is tracked
  separately from other mutation rejections; the recovery surface narrows to
  `read_file` alone (a symbol listing cannot feed another guessed block), and
  the allowance is consumed only by an actual `read_file` of the rejected
  path.
- **Symbol map on rejection.** The recovery checkpoint injects a bounded
  symbol map (kind/name/line) of the rejected file so the forced read lands
  on the right region of a large file instead of the header (a second run
  showed the forced read landing on offset 1 while the target class was much
  deeper).
- **Replay-guard parity.** `_rejection_needs_exact_read()` treats
  replay-guard rejections (`already failed/succeeded in the current run`) the
  same as block mismatches — otherwise a `list_symbols` call could consume
  the recovery allowance while the same hallucinated patch replayed forever.
- **`run_tests` delegates to the declared interpreter.** When
  `VIRTUAL_ENV` differs from the running interpreter, `run_tests` now
  delegates the whole check to `$VIRTUAL_ENV/bin/python`, passing the sandbox
  root and repository `PYTHONPATH` explicitly. Without this, the actor saw
  misleading Python-3.14 collection errors (`No module named 'asgiref'`) and
  patched an unrelated package file.
- **Harness-evidence classification.** `run_tests` classifies a run whose
  errors are import failures, setUp/tearDown failures, or one repeated
  framework-initialization traceback (zero assertion failures) as harness
  evidence, and tells the actor to use the project's own test entry point via
  `run_command` instead of continuing to patch product code.
- **Runner metadata in the prompt.** The runner appends the instance's
  supported test entry point (e.g. `tests/runtests.py --verbosity 2 <label>`
  for Django) to the task prompt as harness metadata — naming the runner,
  never the tests to satisfy.
- **Thinking-mode timeout tuning.** Thinking turns are much slower (32.9s to
  91.7s observed); forced-action turns in thinking mode are capped at
  `THINKING_ACTION_MAX_TOKENS=1536` (~100s), and swebench_runner needs a
  correspondingly larger `--chat-timeout` (240s worked) for thinking runs.
  The llama.cpp adapter surfaces `reasoning_content` as `message.thinking` and
  extracts inline `<think>...</think>` blocks.
- **git-init per candidate.** Candidate workspaces are plain archive
  extracts, so `git_status`/`git_diff` rejected them as "not inside a git
  working tree" even though those tools are offered. The runner now
  initializes each candidate as a real git repo (git init + add + commit,
  ~5s) before the agent starts; the grader copy inherits it.
- **Disk-full fix.** The system volume hit 100% and killed three runs
  because `swebench_runner.py` never removed per-run temp workspaces and
  copied the full source tree twice per run (~120MB). The runner now removes
  both temp workspaces after the report/transcript are written, and uses
  `_clone_tree` (APFS `cp -cR` copy-on-write clone, real-copy fallback) so a
  116MB Django clone adds ~0 bytes to the volume. Per-run marginal disk cost
  is now only the actor's edit deltas plus the applied test patch. The Django
  base checkout and its venv under `assets/benchmarks/` are intentional
  persistent assets.

Deterministic suite after the full saga: `226 passed, 38 subtests passed, 1
warning` (accumulated through the individual fixes above; `252 passed` after
the later edit_range/reproduce-first work).

## Condensed changelog: durable generic fixes from the retired novelty/4B era

Everything in this section predates the single-actor baseline policy and was
originally driven by `agentic_benchmark.py` fixtures (`cascading_loop`,
`websocket_chat`, `multi_file_transaction`, `real_app`, `3d_scene`,
`wifi_simulator`, `feature`, `dependency-graph`, an old `Todo` app, and a
now-abandoned SymPy MLX/4B trajectory). The 4B worker, novelty context,
action-critic/action-gate, and MLX backend that drove these fixes are
retired. What follows is *not* narrative — it is the list of generic,
model-agnostic mechanisms those hundreds of turns produced that are still
live in the code today.

**Lifecycle FSM & control plane** (`lifecycle_fsm.py`, `lifecycle_policy.py`,
`action_governor.py`):

- Explicit states `ORIENT/ACT/VALIDATE/REPAIR/RECOVER/COMPLETE/FAILED` with a
  strict transition table; illegal transitions raise `InvalidTransition`
  instead of silently falling through scattered conditionals.
- Setup/dependency failures and genuine product/behavior failures are a
  first-class separate plane. A missing runner, missing dependency, or
  zero-test discovery never unlocks product mutation; only a real assertion
  failure does. This one distinction fixed a long series of loops where the
  actor rewrote correct code to compensate for a broken test harness.
- Command-plane / tool-plane guards classify shell commands with tokenized
  parsing (`shlex`) rather than raw regex over command text, to correctly
  separate mutation from inspection from output-only commands across many
  false-positive shapes: inline interpreter readbacks (`node -e`, `python -c`
  that reads+prints a file), redirects vs. comparisons/arrows/markup inside
  quoted code (`>`, `=>`, `->`), `sed -i`/`perl -pi`/`tee`/`writeFileSync`
  (case-insensitively), Python's file `open()` vs. `urlopen()`, and
  output-only commands whose printed text merely contains words like
  "assert" or "passed".
- Orientation gate: a bounded read-only exploration window before mutation/
  validation tools are forced; a zero-length or `lines 1-0` read no longer
  counts as orientation evidence; inventory tools (`list_workspace`,
  `list_dir`, `find_files`) are distinguished from tools that return actual
  source evidence, so inventory can't consume the one inspection allowance.
- Repair-recovery bounded checkpoints: after repeated repair turns without a
  mutation, context compacts to the task, last accepted mutation, and latest
  failure packet, exposing only a narrow legal-action surface. A
  block-mismatch or replay-guard rejection requires an exact re-read before
  another mutation is legal (this mechanism is the direct ancestor of the
  django-14034 exact-read/symbol-map fix above). Rejected mutation results
  are promoted into the next repair packet so a malformed patch isn't blindly
  repeated.
- Protected supplied-test paths: test files present at run start are
  snapshotted and protected; an attempted edit is restored and rejected, with
  a path-level block preventing replay. Newly actor-created test files remain
  editable.
- Destructive-rewrite rollback (`risk_layer.py`): a `write_file` on an
  existing file that removes more than 65% of its non-blank lines is
  automatically rolled back to the pre-edit snapshot rather than accepted.
- Multi-file transaction buffer (`transaction_buffer.py`): tracks accepted
  product mutations, defers validation only while a transaction genuinely has
  multiple pending files (not on every ordinary repair mutation), and
  immediately replays the last known failing test once a transaction's
  second file lands. It never rolls back an ordinary failed validation — only
  `risk_layer.py`'s narrow destructive-rewrite/protected-test guards do that.
- 4B-specific mechanisms (synchronous triage, action-critic, action-gate,
  freshness-gated advisory judgments) were fully retired after paired
  ablations repeatedly showed no capability or speed benefit and sometimes a
  regression (e.g. stale advice steering the actor away from the actual
  failing check). The deterministic FSM/policy engine was authoritative
  throughout and remains so.

**Validation contract** (`validation_contract.py`):

- Task-derived acceptance extracts interfaces and typed fields from the task
  text; coverage is method+path-aware (a `POST /api/tasks` probe no longer
  satisfies a required `GET /api/tasks` check).
- Three-way outcome classification — `setup` / `verification` /
  `non_evidence` — so dependency installs, process starts, and zero-exit
  no-assertion commands can't be mistaken for product evidence, and can't be
  mistaken for product *failures* either (they reopen validation, not
  repair).
- Weak-probe detection: file dumps, source listings, version/help output,
  npm/pip/pytest setup output, and environment metadata are recognized as
  non-evidence so a correct artifact isn't rewritten to satisfy a bad probe.
- Explicit checker-failure summaries (`N failed`, `checks: N failed: [...]`)
  are recognized even when the wrapper exits 0; an empty failure list
  (`failed: []`) and zero-count terms (`0 failed`, `0 errors`) are correctly
  parsed as success rather than failure.
- Endpoint/interface extraction excludes conventional temp/home/workspace
  paths and verifier traceback text, so a grader's own file paths can't be
  mistaken for a required application endpoint.

**Test runner** (`workspace/run_tests_tool.py`):

- Preserves bounded failure/error names, traceback tail, `File:line` frames,
  and assertion diff (`+`/`-`) operands as actionable evidence instead of
  aggregate pass/fail counts only.
- Falls back to pytest when unittest discovers zero tests, and further falls
  back to direct execution of top-level `test_*` functions when pytest is
  unavailable, without pretending to implement pytest fixtures/plugins.
- Evicts stale project modules from `sys.modules` before each run so edits
  are re-imported instead of validating against a cached earlier version.
- Delegates to the task's declared interpreter (`VIRTUAL_ENV`) when it
  differs from the runner's own interpreter.
- `find_files` glob matching handles `**/` correctly for root-level files
  (Python's `fnmatch` otherwise treats `**/` as requiring a subdirectory).

**Grader / benchmark harness** (`independent_grader.py`,
`agentic_benchmark.py`, `swebench_runner.py`):

- Grader source and execution run outside the actor's own workspace (used to
  be an in-workspace file with only a prompt convention protecting it).
- Supplied-test integrity check: hashes snapshotted pre-run; any actor
  tamper on a supplied test → `UNSAFE_WORKSPACE_CHANGE`, which the grader
  cannot turn into a pass and which skips verifier-repair.
- `fail_to_pass`/`pass_to_pass` preconditions: the baseline must genuinely
  fail before the actor starts; timeout/invalid-environment results are not
  accepted as a valid baseline failure.
- Hidden/shadow acceptance layer: a benchmark-owned held-out check the actor
  never sees in its prompt or repair feedback; only a generic "hidden
  acceptance evidence failed" signal reaches the actor on failure.
- Mutation-guided grader-strength tests: the host applies known-wrong
  mutations to a private workspace copy and asserts at least one grading
  layer rejects each one, proving the checker isn't trivially satisfiable.
- `ENVIRONMENT_INVALID` classification (2026-08-17, still current): see
  above.
- Grading has its own wall-clock budget (`--grade-timeout`, separate from the
  run budget) so one hanging/slow test can't dominate the cycle.
- Scorecard tracks `run_completed`/`finish_called`/`handoff_reconciled`
  separately — a correct partial artifact without `finish_task` is not
  silently a pass; narrow reconciliation applies only to a clean process
  exit plus an explicit provider-loss signature plus an independent pass.
- A deterministic, model-free adversarial preflight
  (`tests/test_adversarial_preflight.py`) runs before every real-model
  benchmark call and has grown into a large corpus of command-plane/
  evidence-classification edge cases, so most regressions are caught for
  free before spending a model call.
- Per-run temp workspaces use `_clone_tree` (APFS copy-on-write, real-copy
  fallback) and are deleted after the report/transcript are written.
- Candidate workspaces are initialized as real git repos (`git init` + one
  baseline commit) so `git_status`/`git_diff` tools work.

**Provider/transport** (`agent.py`, llama.cpp adapter):

- Tool-call arguments are serialized to JSON strings at the transport
  boundary (some OpenAI-compatible servers require a string, not a dict).
- The llama.cpp adapter translates `tool_choice` to the string `"required"`
  form it expects, sends `enable_thinking` explicitly, surfaces reasoning as
  `message.thinking`, and extracts inline `<think>...</think>` blocks from
  content when the template returns reasoning inline.
- Terminal vs. retryable provider errors are distinguished: connection
  refused / DNS failures are terminal immediately; specific transient
  disconnect signatures (`RemoteDisconnected`, remote-end-closed, peer reset)
  are retried once with a short delay, then terminal — a dead provider can't
  create an overnight retry storm.
- A hard wall-clock guard (`SIGALRM`) wraps model calls in addition to the
  transport timeout, so a stalled generation can't hang the run.
- Forced/recovery-turn token budget is raised (4096 tokens; capped lower —
  1536 — in thinking mode) so forced tool calls have enough headroom to
  complete instead of truncating mid-call.
- The command tool rejects literal newlines in single-line argv tokens for
  providers that can't safely encode them; `run_command`'s own argv path (no
  shell) safely allows genuine multiline interpreter arguments.
- Context bounding (`_bound_live_tool_results()`) keeps raw tool output
  within a percentage of the provider's context window (discovered via
  `/props` or configured `NUM_CTX`), with an exact pre-request `/tokenize`
  measurement and remeasure-and-trim loop when the provider exposes it,
  raising `PromptBudgetError` rather than sending or retrying a doomed
  oversized request.

**Editor evolution**: `patch_file` (exact block match) → ambiguity guard
(rejects multi-occurrence matches instead of silently replacing the first) →
renamed/stricter fields (`find_exact_block`/`replace_with_block`, strict
Pydantic, `additionalProperties: false`) → `apply_patch` (atomic multi-file
editor, A/B'd twice against `patch_file` on the frozen `multi_file_transaction`
task — technically sound and transaction-safe, but not a convergence win: the
actor tended to make one correct edit, then replay a stale second patch and
repeat `finish_task` instead of recovering, relying on bounded verifier-repair
to finish the job; not adopted as default) → **`edit_range`** (current
default, see the 2026-08-17 section above). `write_file` rejects overwriting
an existing file and directs the actor to the active editor instead; shell
write side-channels (`shutil.copy*`, `os.replace`/`rename`, `write_bytes`,
`sed -i`, `perl -pi`, `writeFileSync`, `tee`, redirects) are all classified
as MUTATE so they can't bypass the editor contract.

## Registered tasks

**`swebench_runner.py`** (current entrypoint): `django__django-14034`,
`sympy__sympy-13878`.

**`agentic_benchmark.py`** (older harness, mostly retired-condition test
beds — kept only because `lru_cache`/`bug_repair` are still exercised):
`lru_cache`, `bug_repair`, `cascading_loop`, `multi_file_transaction`,
`websocket_chat`, `3d_scene`, `real_app`, `wifi_simulator`, `feature`,
`data_report`, `recovery`, `dependency-graph`. The last several were single
runs used to find and fix one generic engine defect each (folded into the
changelog above); do not treat their individual pass/fail history as a
current capability claim.

## Open items

- **django-14034 `edit_range` validation run** — in flight as of this write;
  check `state/benchmark/runs/` and `state/benchmark/agentic/results.jsonl`
  for the outcome before drawing conclusions about whether the turn-2
  block-mismatch loop is actually gone under the new editor.
- **Turn-efficiency on django-14034** remains the main open gap: even
  successful runs used far more turns than the frozen `agentic_benchmark`
  fixtures' strict iteration targets ever allowed. `edit_range` is the
  current bet on reducing that; if it doesn't move the needle, look at
  multi-action-per-turn batching rather than more orientation suppression.
- **`apply_patch` vs. `patch_file`/`edit_range`**: not adopted as default;
  if revisited, A/B it specifically against `edit_range` on the frozen
  `multi_file_transaction`-equivalent case rather than assuming the older
  `apply_patch` result still applies.
- Working memory Phase 3 (compressed decision history) has not been started.
- Pydantic v2 strict/`extra=forbid` migration is applied to tool-argument
  models and is the intended pattern for any new model-facing contract;
  internal FSM/event objects stay dataclasses by design.
