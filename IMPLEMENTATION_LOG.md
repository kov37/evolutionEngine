# Implementation log

Running record of what's actually been built against `AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md`, in the order it happened — not a design doc, a build log. Each entry says what changed, why, and how it was verified.

## Phase 4 — Evidence-gated controller (phase machine, completion gate, subgoals, hypotheses, stagnation detector)

**Status: done**, items 1-5 of the build order added to the plan doc (`controller/completion.py` §"Generic (pre-Phase-6) implementation"). Item 6 (checkpoint/restore) is the explicit stretch item and **not built** — deferred, not silently dropped.

### The central design constraint, and how it's enforced everywhere here

Per the plan doc's "Enforceable evidence versus semantic claims": nothing here can verify that a model's claim is *true*, only that memory/reducers.py's `reduce_state()` shows something real happened. Every gate in `controller/` follows the same shape — a **weak-but-real** check, not a semantic one:

- `controller/phases.py`'s `derive_phase()` — pure reducer over `reduce_state()`, no new tool call, can't be gamed by prose.
- `controller/subgoals.py`'s `subgoal_complete()` — rejects unless a real, non-bookkeeping tool call succeeded *after* the subgoal was created (`has_real_progress_since()`). Doesn't check whether the subgoal's actual claim is correct — only that something happened.
- `controller/hypotheses.py`'s `hypothesis_resolve()` — uses the doc's `prediction_observed`/`prediction_disconfirmed` vocabulary (never `confirmed`/`rejected`), and requires citing a real `evidence_id` that postdates the hypothesis. Doesn't verify the citation actually supports the claim — only that it's a real, later event.
- `controller/completion.py`'s `evaluate_completion_gate()` — the generic (pre-Phase-6) implementation of the doc's 8-predicate gate; outcome is `"unverified"` on every generic pass, never `"resolved"` (reserved for Phase 6's external verifier).

All four are **stateless**, matching `memory/reducers.py`'s own design: every call re-derives its ledger from the run's own event log rather than holding private in-process bookkeeping. `subgoal_create`/`subgoal_complete`/`hypothesis_record`/`hypothesis_resolve` are ordinary tool functions (closures bound to one `RunStore`, same factory pattern as `docker_verify_tools.make_run_shell_in_container`) — `dispatch_tool_calls` records them exactly like `read_file`, so nothing extra was needed to make their ledgers reconstructable.

### What was built

- **`controller/phases.py`** — `derive_phase()`: `orient → reproduce → localize → patch → verify → review`, derived from `reduce_state()`'s existing lists. `finish` deliberately isn't derived here — `agent.py` already knows synchronously via `TASK_STATE`.
- **`controller/completion.py`** — `evaluate_completion_gate()`, gating `finish_task` for the first time (previously: any call succeeded unconditionally). Generic predicates: `changed_entities` non-empty, a test/command ran after the last write, the *most recent* such check didn't fail, the diff was reviewed. Plus the two cheap substitutes added to the plan doc: `current_git_head()` unchanged since run start, and an optional `forbidden_paths` glob param.
- **`controller/subgoals.py`** — `subgoal_create`/`subgoal_complete`. Creation is cheap (the model stating its own plan — HiAgent's own ablation found this is where most of the benefit comes from); completion is evidence-gated as described above. IDs are assigned positionally from the event log (`sg-01`, `sg-02`, …), not parsed back out of a result string.
- **`controller/hypotheses.py`** — `hypothesis_record`/`hypothesis_resolve`, same citation-gate pattern.
- **`controller/progress.py`** — `stagnation_nudge()`, a *second*, broader stall signal layered on top of (not replacing) `agent.py`'s existing repetition/confidence-checkpoint/watchdog mechanisms, which fire on "no successful **write**" and were built for single-symbol stalling. This fires on "no successful **anything**" (not even a new read or a declared subgoal) — the cross-subgoal stall the existing mechanisms can't see.
- **`agent.py`** — wires all of the above in: captures `initial_git_head` at run start, builds the four new tools bound to `run_store`, adds them to the toolbelt, adds the stagnation check alongside the existing watchdog block, and replaces the old unconditional `if TASK_STATE["done"]: return True` with the gate — a rejection resets `TASK_STATE["done"]` and injects a message naming exactly what's missing, mirroring `patch_file`'s ERROR/REJECTED convention. System prompt updated to describe the new tools and that `finish_task` can be rejected.
- **`controller/test_controller.py`** — 18 self-tests covering Phase 4's four stated acceptance criteria directly, plus the two real bugs below as explicit regression tests.

### Two real bugs a live run caught (and fixed) — the payoff of testing against the actual model, not just synthetic cases

A live run against a genuine two-function bug (`add()`/`multiply()`, wrong operators) surfaced two real gate bugs within the first attempt:

1. **A superseded failure blocked completion forever.** The model tried `python` (not installed — `/bin/sh: python: command not found`), then correctly switched to `python3` and passed. The gate's first version checked "did *any* failure happen since the last write" — the long-since-corrected `python` failure kept rejecting `finish_task` for the rest of the run. Fixed: judge only the *most recent* verification event, ordered by real event sequence (`memory/events.event_seq`), not by `iteration` (which can't distinguish multiple events in the same turn). This is precisely the "bounded best-effort" paralysis trap the plan doc's failure-mode table warns about — caught in practice, not hypothetically.
2. **A *failed* diff-review call satisfied the diff-review predicate.** The model called `git_diff` against a non-git scratch directory; it errored (`not inside a git working tree`); the gate counted the attempt as review performed regardless. Fixed: require the diff-review tool call to have *succeeded*, with re-reading every changed path afterward accepted as an equivalent substitute (since `git_diff` is unusable outside a git repo and `diff_files` needs a second file to compare against — without a substitute the predicate would be unsatisfiable for a plain, non-git project).

Both are now explicit regression tests (`test_completion_gate_ignores_superseded_earlier_failure`, `test_completion_gate_rejects_failed_diff_review_call` / `test_completion_gate_accepts_reread_as_diff_review_substitute`).

### Verification performed

- `python3 controller/test_controller.py` — 18/18, covering `derive_phase` transitions, all of `evaluate_completion_gate`'s predicates (including the two regression cases above), subgoal creation/rejection/double-completion, hypothesis citation gating, and stagnation-nudge firing/reset.
- Full existing suite (`memory/`, `context/`, `kernel/`, `verification/`) — all still green, no regressions.
- Three live runs against the real model: the first caught both bugs above; the second (after the fix) completed cleanly with the `python`→`python3` self-correction correctly not blocking completion; a third, with a task explicitly asking for subgoal use, produced a fully clean trace — two subgoals declared upfront, each independently fixed and verified, both `subgoal_complete` calls correctly allowed, `finish_task` passed on the first attempt with `outcome: "unverified"`.
- **Honest limitation, not glossed over**: the second live run (no explicit subgoal prompting beyond the system prompt's mention) solved both bugs as a single combined edit rather than declaring separate subgoals — tool *adoption* isn't guaranteed by a system-prompt mention alone, only tool *behavior* is guaranteed once called. The third run's explicit task-level nudge did produce full adoption, but this isn't something the completion gate or evidence checks can force the way they force evidence quality.

### Not yet done

Item 6 (checkpoint/restore, reusing the `post_content` artifacts Phase 2 already captures — deferred, not blocking). Phase 5 (hierarchical retrieval, `memory_expand`/`memory_recall` tools). Phase 6 (`swe/` adapter — real `base_commit` lineage, real forbidden-file policy from instance metadata, and the external SWE verifier that would finally let `outcome` be `"resolved"` instead of always `"unverified"`). The actual headline question — does this fix "plan instability" on a real multi-file SWE-bench instance like `pylint-4551` — still can't be answered until Phase 6 exists to run that comparison for real.

## Phase 3 — Bounded context compiler and policy baselines

**Status: done**, scoped to 3 of the plan's 4 named policies. `append-all` (the current behavior, mathematically unchanged) and `sliding-window` are baselines; `bounded-structured` is the real new mechanism (task contract + Phase 2's `reduce_state()` output + a short recent tail, each budgeted). **Not built**: `flat-summary` and `hierarchy` — both need an LLM-generated summary keyed to subgoal boundaries, and subgoals are Phase 4's controller, which doesn't exist yet. Building a summarization policy against invented boundaries now would mean redoing it once Phase 4 provides real ones.

### What this changes, and what it doesn't

This is the first phase where the model's actual prompt can change — but the *default* doesn't: `agent.py`'s `memory_policy` param already existed (Phase 0, metadata-only until now) and still defaults to `"append-all"`, whose policy function returns `system_and_task + tail` — the exact same list `agent.py` sent before this phase existed, not an approximation of it. Nothing about default behavior changed; `bounded-structured`/`sliding-window` are opt-in via the existing param, per the plan's own design principle 10 ("make every policy ablatable" — starting from a real, unmodified baseline, not a reconstructed one).

### What was built

- **`context/budget.py`** — token *estimation* only (chars/4 — no tokenizer vendored; real usage is still measured exactly via Ollama's `prompt_eval_count`, this only drives assembly-time trim decisions) and the four budgets this compiler actually uses (`system_policy`, `task_contract`, `structured_state`, `recent_tail`). The plan's own initial 32K table also lists `retrieved_evidence` (Phase 5, doesn't exist) and a separate `action/verification instructions` line (already inside `agent.py`'s system prompt, not a separate section) — both skipped rather than reserved as unused budget lines.
- **`context/render.py`** — `render_structured_state()`, turning Phase 2's `reduce_state()` output into a compact text block: files touched (deduped by path, changed-entities take precedence over inspected, stale ones flagged and sorted last — "penalize stale evidence," applied directly), recent test runs, recent failures with taxonomy — each with an `event_id`/`artifact_id` reference back to raw evidence, never the raw tool output itself inline. That's the concrete mechanism behind "exclude redundant raw tool output": the model gets *that a file was read and what happened*, not the file's full previously-seen contents replayed again.
- **`context/policies.py`** — `group_into_turns()` is the one genuinely important correctness piece here: a tool-role message is only ever valid immediately after the assistant message whose `tool_calls` it answers, so every policy keeps or drops **whole turn blocks**, never splits mid-pairing (which would send Ollama an invalid request). `policy_sliding_window` keeps the last N whole turns; `policy_bounded_structured` keeps task contract + rendered state (computed fresh from the run's *own* live event log, mid-run — `reduce_state()` doesn't care whether the run is finished) + a recent tail trimmed turn-block-at-a-time to fit budget, never truncating the state block itself.
- **`context/compiler.py`** — `compile_context(memory_policy, messages, run_dir)`, the one call site `agent.py` now goes through every turn instead of passing `messages` directly to `chat()`.
- **`agent.py` / `memory/store.py`** — `record_model_call()` gained an optional `compiled_context_tokens_estimate`, and `compute_metrics()` now reports `compiled_context_tokens_by_turn` and `peak_compiled_context_tokens_estimate` — Phase 3's own "context-size metrics" deliverable, actually comparable across policies now.
- **`context/test_context.py`** — covers Phase 3's four stated acceptance tests directly: context stays bounded over 200 synthetic turns (sliding-window and bounded-structured both stay under 1/5 of append-all's size; append-all is *expected* to grow — that's the point of keeping it as a baseline), the task contract survives byte-identical under all three policies, a test failure recorded at turn 3 still appears in the rendered state at turn 200 (because `reduce_state()` folds the *entire* event log every call, not just the recent tail — state doesn't decay with turn count the way pure sliding-window detail does), and every `ref=` in the rendered state block resolves to a real event.

### Verification performed

- `python3 context/test_context.py` — 7/7, including the 200-synthetic-turn acceptance tests.
- Full existing suite (`memory/test_memory.py`, `memory/test_reducers.py`, `kernel/test_io_tools.py`, `verification/test_bypasses.py`) — all still green.
- Two live runs against the real model, same task (a `subtract()` sign bug), one under the default `append-all` and one explicitly under `bounded-structured` — both completed correctly (`WON: True`), and the `patch_file` whitespace-fallback fix caught the same recurring mistake pattern in both. On a task this short (4-5 turns), the two policies' `compiled_context_tokens_by_turn` look nearly identical — expected and worth being upfront about: `bounded-structured`'s 4-turn recent window covers almost the entire run at this length, so there's nothing yet to compact. The real divergence is what the 200-turn synthetic test demonstrates; a live task long enough to show it live doesn't exist yet without SWE-bench back (Phase 6).

### Not yet done

Phase 4 (SWE controller, subgoal DAG, evidence-gated completion — the actual mechanism expected to fix "plan instability," and the first phase that gives `bounded-structured` real subgoal boundaries instead of a fixed recent-turn count), `flat-summary`/`hierarchy` policies (blocked on Phase 4), retrieval (Phase 5), and the `swe/` adapter (Phase 6) needed to validate any of this against a real multi-file SWE-bench trajectory again.

## Side fix — `patch_file` whitespace-tolerant fallback

Not part of the plan; found because Phase 0/1's live smoke test hit it directly. `kernel/io_tools.py:patch_file()` required the model's `search` string to match file content byte-for-byte, including whitespace. The smoke-test run (see below) failed twice in a row on exactly this: the model wrote three spaces before a trailing `# comment` where the file actually had two — same failure signature as `SWEBENCH_ANALYSIS_PARALYSIS_FINDINGS.md`'s `pylint-4551` runs.

Fix: on exact-match failure, retry with runs of whitespace in `search` turned into a flexible `\s+` pattern. If that finds exactly one location, use it; if it finds zero or more than one, still reject (never silently guess which occurrence). Exact matches take the same code path as before — no behavior change when the model gets it right. Added `kernel/test_io_tools.py` (exact-match, fallback, ambiguous-rejection, genuinely-not-found cases). Re-ran the same smoke-test task against the real model after the fix: same whitespace mistake, now recovered — `WON: True` in 5 turns instead of exhausting a 6-turn budget on an unfixed bug.

## Phase 2 — Automatic materialized state

**Status: done**, scoped down from the plan's full list. Built: repository/entity reducer (reads + writes, with staleness), test reducer, shell-command reducer with a structured failure taxonomy, and a lightweight git-call passthrough. **Deliberately not built**: hypothesis/evidence-claim extraction — nothing in the current pipeline produces a claim to track (that needs either parsing free model prose, which design principle 6 explicitly rules out, or an explicit `memory_*` tool call, which is Phase 5), and SWE verifier fields (fail-to-pass/pass-to-pass/resolved) — no verifier is wired up until Phase 6's `swe/` adapter replaces the deleted `evolve/swebench/` scaffolding. Phase/subgoal state is Phase 4's controller, also not here.

### What was built

- **`memory/reducers.py`** — `reduce_state(run_dir)`, a pure function over `memory.events.read_events()` (plus the run's own `artifacts/` directory for full tool-result text — that still counts as "the event log," not live external state). Produces `inspected_entities`, `changed_entities`, `test_runs`, `shell_runs`, `git_calls`, and `failures`. Entity tracking is scoped to tools with one unambiguous target path (`read_file`, `search_file`, `write_file`, `patch_file`) — `grep_dir`/`list_dir`/`list_symbols`/`git_status`/`git_diff` operate on a whole directory, not one entity, and aren't folded into entity state (they're still in the raw event log, just not structured here).
- **Stale-evidence invalidation**: a write to path `P` marks every prior `inspected_entities`/`changed_entities` record for `P` as `stale: True`, purely from event order — no content-hash comparison needed for this property to hold.
- **Structured failure taxonomy**: `product_failure`, `test_environment_failure`, `patch_application_failure`, `missing_dependency_failure`, `timeout_or_resource_failure`, `unknown_failure`. `run_tests` failures are exact (parses `workspace/run_tests_tool.py`'s own deterministic `"Ran N tests: P passed, F failed, E errors"` format). `run_shell` classification is pattern-based on stdout/stderr (`ModuleNotFoundError` → missing dependency, `SyntaxError` → environment, `AssertionError`/`Traceback` → product) and honestly documented as heuristic, not a real per-framework parser — an "unknown_failure" fallback is safer than a wrong specific label.
- **`agent.py` / `memory/store.py`**: `RunStore.record_tool_call()` gained an optional `post_content` parameter. `agent.py`'s tool-call recorder now reads a file's actual on-disk content immediately after a successful `write_file`/`patch_file`, while it's still there, and stores it as an artifact linked to the event (`post_content_artifact_id`). This exists so `changed_entities` has real post-write content without ever reading the project's filesystem at *reconstruction* time — the plan's own "before/after hash, diff" deliverable, captured at record time so it survives even if the underlying checkout is later deleted.
- **`RunStore.record_task_finished()`** now also writes `state.json` (via `reduce_state`), alongside the existing `metrics.json` — the run directory finally matches Phase 0's originally-planned layout, populated only once something actually produces the data (no placeholder file written earlier with nothing in it).
- **`memory/test_reducers.py`** — self-test covering all four of Phase 2's stated acceptance criteria directly: a synthetic trajectory produces expected state, editing a file invalidates prior file facts, a failed test creates a machine-readable failure record, and `reduce_state()` is a pure function of the run directory (called twice, same result).
- **`memory/report.py`**: `--expand` now also accepts a raw `artifact_id` (`sha256:...`), not just an `event_id` — needed to inspect a `post_content_artifact_id`, which isn't an event's own primary artifact.

### Verification performed

- `python3 memory/test_reducers.py` — 6/6 new self-tests pass.
- `python3 memory/test_memory.py`, `kernel/test_io_tools.py`, `verification/test_bypasses.py` — all still pass (no regressions).
- Applied `reduce_state()` to the two real runs already recorded from Phase 0/1's smoke tests — correctly reconstructed both `patch_file` failures (pre-fix run) and the successful write (post-fix run) purely from their event logs, including catching that the pre-fix run's `cat -A` shell error correctly falls back to `unknown_failure` rather than a wrong specific label.
- One more live run against the real model (a second scratch bug, `multiply()` returning `a + b`) exercised the full pipeline end-to-end, including the new `post_content` capture and `state.json` write-on-finish — the model hit the *same* whitespace-mismatch pattern a third time, and this run's `patch_file` call succeeded silently via the fallback from the prior fix. Verified the captured `post_content_artifact_id` byte-for-byte against the real file on disk afterward.

### Not yet done

Phase 3 (bounded context compiler — first phase where the model's actual prompt starts changing; nothing through Phase 2 changes what the model sees), Phase 4 (SWE controller, subgoal DAG, evidence-gated completion), and the `swe/` adapter (Phase 6, needed before any of this can be validated against a real SWE-bench trajectory again).

## Phase 0 + 1 — Baseline instrumentation + lossless event/artifact store

**Status: done.** Scoped exactly to the plan's own "First implementation ticket" section: event/artifact recording only, current `agent.py` behavior unchanged, no RL/embeddings/summarization/new controller.

### What was built

- **`memory/schema.py`** — versioned run/event record shapes (`SCHEMA_VERSION = 1`) plus `validate_event_record()`. Deliberately does *not* yet include the plan's `phase`/`subgoal_id`/`repo_tree_hash` fields — nothing populates those until Phase 2 (reducers) and Phase 4 (controller), and an unused field on a record no one reads is the exact kind of speculative structure this plan's own design principles argue against.
- **`memory/artifacts.py`** — content-addressed storage (`sha256:<hex>` → file). This is the actual fix for the plan's core complaint: `dispatch.py`'s `MAX_MESSAGE_CONTENT_CHARS` (4000) still caps what the model sees on future turns — unchanged — but now the full, untruncated result is written here *before* truncation, so it's byte-for-byte recoverable later instead of gone forever.
- **`memory/events.py`** — append-only `events.jsonl` per run, one `EventWriter` per run directory. Every `append()` is `fsync`'d immediately (a crash right after a call returning "recorded" must not lose that event). On construction, a writer replays the existing log to resume `event_id` numbering and `parent_event_id` chaining — so a process that dies mid-run and gets re-invoked against the same run directory doesn't restart numbering or break the parent chain. `read_events()` never raises on a corrupt line; it yields a `corrupt_event` marker and keeps going, so one bad line can't make an otherwise-real run unreconstructable.
- **`memory/store.py`** — `RunStore`, the actual integration surface. Owns `.evolution/runs/<run_id>/` (`run.json`, `events.jsonl`, `artifacts/`, `metrics.json`). `episodes/` and `checkpoints/` from the plan's directory layout are not created yet — same reasoning as the schema fields above, they're Phase 4/5. `compute_metrics()` derives token/latency/tool-call/write-call/turns-to-first-write purely from the event log, so it can be recomputed standalone from any run directory, including one from an interrupted process.
- **`memory/report.py`** — `python3 -m memory.report <run_id>` reconstructs a run turn-by-turn from the event log and prints its metrics; `--expand <event_id>` pulls back a full artifact. This is Phase 0's acceptance test #1 ("a run can be reconstructed turn by turn") as a runnable command, not a claim.
- **`memory/test_memory.py`** — self-test in this project's own established convention (`python3 memory/test_memory.py`, exit 0 iff every assertion passes — same pattern as `curriculum.py`'s `GRADUATION_CONTRACT`, not pytest). Covers artifact round-trip (including a >4000-char payload, past `dispatch.py`'s truncation threshold), event ordering/parent chains, interruption recovery, corrupt-line tolerance, schema validation, and a full `RunStore` run.

### Integration points

- **`dispatch.py`** — `dispatch_tool_calls()` gained one new optional parameter, `recorder=None`. When given, it's called with `(tool_name, arguments, full_result)` — the *full* result, before the existing truncation block runs. Default `None` means `harness.py`'s call site is untouched; only `agent.py` passes one. This was a deliberate choice against the plan's own file-layout comment ("`dispatch.py` — tool execution + event recording") — `dispatch.py` is shared with `harness.py`'s curriculum bootstrap loop, which has no run store and shouldn't need one, so the hook is opt-in rather than baked into every caller.
- **`agent.py`** — `run_agent()` now creates one `RunStore` per run (`task_id` param added, defaults to `adhoc-<timestamp>` since the old SWE-bench runner that used to supply a real one was removed — see below). Each model call is timed (`time.monotonic()`) and recorded with real token counts read off Ollama's own `ChatResponse.prompt_eval_count`/`.eval_count` — not estimated. Each tool call is recorded via the new `recorder` hook. `finish_task` and budget-exhaustion are both recorded as `task_finished` events. None of the existing repetition/confidence-checkpoint/watchdog logic changed.
- **`_live_model_options()`** (new, in `agent.py`) queries `ollama.show(MODEL).parameters` at run start instead of hardcoding assumed values into `run.json`. Real finding from doing this instead of guessing: **`num_ctx` is not set anywhere for `qwen3.6:35b-mlx`** — only `temperature=1, top_k=20, top_p=0.95, min_p=0, presence_penalty=1.5, repeat_penalty=1` are in the Modelfile. This project has been assuming a 32K effective context window throughout the SWE-bench investigation; that number was never actually being requested at the Ollama API level. Worth resolving before trusting any context-budget-related finding from here on — flagged, not yet fixed, since fixing it is a model-settings question, not a memory-architecture one (matches the plan's own Phase 0 note: "test thinking mode, temperature, presence penalty, and context budget separately").

### What was explicitly *not* touched

The SWE-bench experiment scaffolding (`evolve/swebench/`, `evolve/hard_family/`, `evolve/logs/`, the `swebench_venv`) was deleted at the user's request before this phase started — it predated the new `swe/` adapter this plan calls for (Phase 6) and was ad hoc (the container/host sync bug documented in `SWEBENCH_ANALYSIS_PARALYSIS_FINDINGS.md` lived there). Consequence: Phase 0's own acceptance criterion "five SWE tasks × three seeds" and "run one known SWE trajectory end to end" could not be exercised this round. **Verified instead against a real (tiny) local task** — a one-line arithmetic bug in a scratch file, run through the actual `qwen3.6:35b-mlx` model via `agent.py` with `iteration_budget=6` — to prove the instrumentation works against a live model call, not just synthetic unit tests. Full SWE-bench validation is deferred to whenever the `swe/` adapter (Phase 6) is rebuilt.

### A real bug the smoke test caught

`compute_metrics()`'s first version counted any `patch_file`/`write_file` call as a write, regardless of whether it succeeded. The live smoke-test run had two `patch_file` attempts that both failed (`ERROR: search text was not found verbatim`) — `turns_to_first_write` reported turn 2 as if a write had succeeded there, when the model never successfully wrote anything in the whole 6-turn run. Fixed by checking the result text for the same `ERROR`/`REJECTED` prefix `agent.py`'s own `wrote_this_turn` check already uses, and added a regression test (`test_memory.py`) encoding this exact scenario. This is precisely the class of bug the plan's design principle #6 ("evidence, not prose, for progress") exists to prevent — the mechanism intended to detect false signals had, itself, a false-positive bug, caught by running it against a real model instead of only synthetic cases.

### Verification performed

- `python3 memory/test_memory.py` — 6/6 self-tests pass.
- `python3 verification/test_bypasses.py` — 7/7 pre-existing security scenarios still pass (unaffected by this phase's changes).
- Live smoke-test run against `qwen3.6:35b-mlx` via `agent.py`, 6-turn budget, tiny scratch task — completed without error, produced a correct `run.json`/`events.jsonl`/`metrics.json`, and `memory/report.py` reconstructed it turn-by-turn correctly, including catching the write-counting bug above.

### Not yet done (next phases, not started)

- Phase 2 (automatic materialized state / reducers — repo/diff/test/hypothesis/evidence, stale-evidence invalidation).
- Phase 3 (bounded context compiler — this is where the model's actual prompt starts changing; nothing here changes it yet).
- Phase 4 (SWE controller, subgoal DAG, evidence-gated completion — the actual mechanism expected to fix "plan instability").
- Rebuilding a `swe/` adapter to replace the deleted `evolve/swebench/` scaffolding, needed before Phase 0's full 5-task×3-seed acceptance criterion can be honestly claimed.
