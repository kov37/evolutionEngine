# Implementation log

Running record of what's actually been built against `AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md`, in the order it happened — not a design doc, a build log. Each entry says what changed, why, and how it was verified.

## Side fix — `patch_file` whitespace-tolerant fallback

Not part of the plan; found because Phase 0/1's live smoke test hit it directly. `kernel/io_tools.py:patch_file()` required the model's `search` string to match file content byte-for-byte, including whitespace. The smoke-test run (see below) failed twice in a row on exactly this: the model wrote three spaces before a trailing `# comment` where the file actually had two — same failure signature as `SWEBENCH_ANALYSIS_PARALYSIS_FINDINGS.md`'s `pylint-4551` runs.

Fix: on exact-match failure, retry with runs of whitespace in `search` turned into a flexible `\s+` pattern. If that finds exactly one location, use it; if it finds zero or more than one, still reject (never silently guess which occurrence). Exact matches take the same code path as before — no behavior change when the model gets it right. Added `kernel/test_io_tools.py` (exact-match, fallback, ambiguous-rejection, genuinely-not-found cases). Re-ran the same smoke-test task against the real model after the fix: same whitespace mistake, now recovered — `WON: True` in 5 turns instead of exhausting a 6-turn budget on an unfixed bug.

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
