# SWE-bench Analysis Paralysis: Findings and Interventions

## Why this investigation happened

Three earlier rounds of calibration on synthetic single-function bugs (a boundary/
off-by-one family, then a harder classic-algorithm family — count-occurrences,
insert-interval, min-subarray-len, rotate-array, run-length-decode, search-rotated)
all failed the same way: the local model (`qwen3.6:35b-mlx`) recognized these
problems too reliably, at any budget, to show genuine headroom for improvement. The
diagnosis: these are famous, heavily-represented training-data shapes, and the model
had full read access to the (small) buggy source — fixing them was closer to pattern
recall than genuine debugging.

Moving to a real SWE-bench instance — a real, historical, unfamiliar-codebase bug
with a real human-written test suite — immediately produced something qualitatively
different: not "too easy" or "too hard," but a precise, reproducible, literature-
matched failure mode.

## Infrastructure built (all independently verified before being trusted)

- SWE-bench Lite metadata fetched directly via HuggingFace's `datasets-server` REST
  API (`evolve/swebench/fetch_instance.py`) — no `datasets` library dependency.
- Real Docker-based environment building via the official `swebench` PyPI package
  (Python 3.10+, isolated in `evolve/swebench_venv/`) — after a naive
  `git clone` + `pip install -e .` genuinely failed (setuptools/pep517
  incompatibility installing a historical commit under today's toolchain).
- Two-direction environment verification (`evolve/swebench/verify_instance.py`):
  empty patch → bug genuinely reproduces (`resolved: False`); reference patch →
  fully resolves (`resolved: True`, all `PASS_TO_PASS` held). Proven trustworthy
  before ever pointing a real agent at it.
- `agent.py` (unmodified orchestrator, not `harness.py` — SWE-bench's "fix a real
  issue in an existing codebase" shape matches the consumer, not the bootstrapper)
  pointed at a real local checkout via `evolve/swebench/run_agent_on_instance.py`,
  scored independently through the same official grading path.
- A real, live-container verification loop (`evolve/swebench/docker_verify_tools.py`
  + `start_verify_container.py`): syncs the agent's own uncommitted edits into a
  persistent container running the real pinned environment, so `run_shell` inside
  the agent's loop sees exactly what the official grader will see — closes a real
  gap where local-checkout verification hit an unrelated missing-module error and
  never surfaced the agent's actual mistake.
- One real production bug found and fixed along the way: `dispatch.py` only caught
  `TypeError`/`ValueError` from tool calls (despite its own docstring claiming
  "never raises"). A model-authored tool crashed the whole agent on an uncaught
  `FileNotFoundError`. This is the exact fix two independent synthetic self-edit
  experiments had already found and validated earlier the same day — it had never
  been merged back into the real file until this run exposed it live.

## The instance used throughout

`pytest-dev__pytest-7220` — "Wrong path to test file when directory changed in
fixture." Real bug: `_makepath()` in `src/_pytest/_code/code.py` computes a
display path via `py.path.local().bestrelpath(path)`, where `py.path.local()`
(no arguments) returns the *current* working directory — which a test fixture can
change via `os.chdir()`, producing a misleading relative path like
`../test_path_error.py` instead of the expected `test_path_error.py`.

## The core finding: Analysis Paralysis, precisely characterized

Across every single run, the model **correctly and independently diagnosed the
exact root cause** — typically by turn 12–25 of a 35-turn budget. Diagnosis was
never the bottleneck. The bottleneck, observed consistently, was converting a
correct diagnosis into a `patch_file`/`write_file` call. This matches, exactly,
the "Analysis Paralysis" pattern named and quantified in Cuadron et al.,
["The Danger of Overthinking"](https://arxiv.org/abs/2502.08235) — a paper that
studies this same phenomenon on this same benchmark family (SWE-bench Verified).
That paper's own two demonstrated mitigations (native function calling — already
in use here via Ollama's `tools=` API; "Lowest Overthinking@K" — sampling K
trajectories and selecting the one with the lowest overthinking score) were
considered; the second was judged likely inapplicable here, since every sampled
trajectory exhibited the *same* all-or-nothing pattern rather than the kind of
mixed success/failure variance the technique selects between.

## Timeline of interventions — 10 real runs, same instance

| # | Condition | Turn checkpoint fired | Wrote code? | `resolved` |
|---|---|---|---|---|
| 1 | Baseline | — | crashed (unrelated `dispatch.py` bug, found + fixed) | — |
| 2 | Baseline, retry | — | No (0/35 turns) | — |
| 3 | + soft "avoid analysis paralysis" prompt | — | No (0/35 turns) | — |
| 4 | + polite mechanical watchdog | fired 7× (turns 29-35) | No (0/35 turns) | — |
| 5 | + forceful/capitalized prompt + early fixed watchdog (turn 20) | 20 | **Yes**, turn 20 | **False** — reversed the relative-path computation direction |
| 6 | + repetition detector (precise timing), moderate phrasing | 22, 23 | No (0/35 turns) | — (found: backstop-counter reset bug) |
| 7 | + repetition detector + forceful phrasing + fixed backstop | ~20 | **Yes** | **False** — `AttributeError: module '_pytest' has no attribute '_original_cwd'` (right idea — stable cwd reference — wrong namespace) |
| 8 | Identical mechanism to run 7, re-run | fired repeatedly from 28 | No (0/35 turns) | — (confirms real stochastic variance) |
| 9 | + first-mention confidence checkpoint (HIGH/MED/LOW) | 26 (ignored), repetition again at 33 (ignored) | No (0/35 turns) | — |
| 10 | + enforced-grammar confidence checkpoint (1-10 scale, required `CONFIDENCE:`/`ACTION:` template) + live container verification | 23 (syntactic compliance, no immediate behavioral follow-through) → repetition at 26 → watchdog through 28-29 | **Yes**, turn 29 | **True** — genuinely resolved |

## Run 10 in full detail

The checkpoint fired at turn 23. The model's very next message correctly used the
required format — `CONFIDENCE: 8` / `ACTION: ...` — but confidence 8 should have
meant an immediate write per the stated rule, and instead the model's stated
ACTION described *more investigation*, and it called `run_shell`, not
`patch_file`. **Structural compliance with the enforced grammar did not translate
into behavioral compliance with what that grammar was supposed to commit it to.**
It took the repetition detector firing again (turn 26) and the fallback watchdog
escalating through turns 28-29 before a write actually happened.

The fix itself took a genuinely different strategy from run 7's failed attempt —
not a refinement of the same idea:

```diff
     def _makepath(self, path):
         if not self.abspath:
             try:
+                p = py.path.local(path)
                 np = py.path.local().bestrelpath(path)
             except OSError:
                 return path
-            if len(np) < len(str(path)):
+            if len(np) < len(str(path)) and not np.startswith(".."):
                 path = np
         return path
```

Instead of trying to fix *what* `py.path.local()` is computed relative to (run 7's
approach, which introduced a namespace bug), this rejects any `../`-style relative
result and falls back to leaving the path unchanged — sidestepping the "what's the
correct stable reference point" question entirely.

**The model never confirmed this itself.** It ran out of budget at turn 35
attempting to find a related local test to run (`grep_dir` for `_makepath` in
`testing/`, which returned empty — the actual `FAIL_TO_PASS` test only exists once
`test_patch` is applied, which only happens during official grading, not in the
agent's own local checkout). The correctness of this fix was established entirely
by the independent, Docker-based grading step afterward — not by anything the
agent itself verified.

Official grading (`report.json`, `run_id=verify-real-agent-patch-v10`):

```json
{
  "resolved": true,
  "tests_status": {
    "FAIL_TO_PASS": {"success": ["testing/test_nodes.py::test_failure_with_changed_cwd"], "failure": []},
    "PASS_TO_PASS": {"success": [<all 11 tests>], "failure": []}
  }
}
```

## Mechanisms now live in `agent.py`

1. **Broad exception handling in `dispatch.py`** — a real production fix, not
   experimental.
2. **System prompt**: explicit "avoid analysis paralysis" instruction.
3. **`turns_since_last_write` fallback watchdog** — fires unconditionally after
   `WATCHDOG_TURNS_WITHOUT_WRITE = 28` turns without a successful write.
4. **Repetition detector** — `DIAGNOSTIC_PHRASE_PATTERN` + `BACKTICK_SYMBOL_PATTERN`
   flag when the same backtick-quoted symbol is named as the cause, alongside
   diagnostic language, on two separate turns. Fires a hard forced-write message.
5. **First-mention confidence checkpoint** — on the *first* diagnostic mention of a
   symbol (before repetition), forces an explicit `CONFIDENCE: <1-10>` /
   `ACTION: <...>` declaration with a real escape hatch (name the specific missing
   information) rather than a blind forced guess. Adapted from the *mechanism* of
   [arxiv.org/html/2505.23480v1](https://arxiv.org/html/2505.23480v1) (a different
   flavor of overthinking — redundant self-verification in math chain-of-thought,
   fixed there by forcing an explicit input-validation step) — not a direct
   transplant of that paper's own technique, which doesn't target agentic tool-use.
6. **Live container-routed verification** (`evolve/swebench/docker_verify_tools.py`)
   — available to the SWE-bench experiment specifically, substituted into the tool
   list in place of the plain host `run_shell`, without touching the general
   `kernel/exec_tools.py` used everywhere else.

## Honest caveats — what this does and doesn't prove

- **n=1 genuine success in 10 real runs.** This cannot be cleanly attributed to the
  1-10 confidence scale specifically versus the cumulative effect of every
  mechanism layered together, versus ordinary stochastic variance already observed
  between otherwise-identical runs (5 vs. 8 vs. 6/9/10 all differ despite
  overlapping mechanisms). The honest claim is "the combined pipeline produced a
  correct, independently-verified result once," not "compliance is solved."
- **Structural compliance ≠ behavioral compliance.** Run 10's own transcript shows
  the model correctly filling in a required template while not acting on what it
  had just declared — a real, separate gap from "will it write code at all."
- **The container-verification fix was built and confirmed working in isolation**
  (directly tested against the known-broken run 7 patch, correctly surfaced the
  real error) **but was never exercised end-to-end inside a run that used it to
  self-correct** — no run since it landed has both written a patch and gotten real
  local test feedback on it before running out of budget.
- **Only one instance has been tested.** No evidence yet that any of this
  generalizes to a different bug, a different repo, or a different failure shape.
- **The full recursive-vs-independent generation loop** (the original motivation
  for moving to SWE-bench) was never built on top of this — every intervention
  here was manually designed and manually tested, turn by turn, across real runs.
  That is exactly the kind of design space a self-modifying system would need to
  search on its own; none of this replaces that experiment, it's the groundwork
  that made a real, non-trivial version of it possible.
