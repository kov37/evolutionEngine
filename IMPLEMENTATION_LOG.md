# Implementation log

Running record of what's actually been built against `AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md`, in the order it happened — not a design doc, a build log. Each entry says what changed, why, and how it was verified.

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
