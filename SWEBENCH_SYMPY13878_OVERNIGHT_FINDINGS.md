# Overnight run: sympy-13878 — a genuinely harder failure mode than Analysis Paralysis

## Setup

Follow-on from [SWEBENCH_ANALYSIS_PARALYSIS_FINDINGS.md](SWEBENCH_ANALYSIS_PARALYSIS_FINDINGS.md)
(`pytest-dev__pytest-7220`, resolved on run 10 after building a watchdog, a
repetition detector, and an enforced-grammar confidence checkpoint). This run
reused that same, unmodified `agent.py` — plus two reliability additions made
specifically for an unattended run (`max_wall_clock_seconds`, a 20-attempt
retry/backoff loop around `chat()`) — against a deliberately harder instance,
picked for requiring *sustained multi-part execution* rather than one
localized fix:

- Instance: `sympy__sympy-13878` — "Precompute the CDF of several
  distributions where integration doesn't work well." The reference fix adds
  a closed-form `_cdf` method to 12 separate distribution classes in
  `sympy/stats/crv_types.py` (Arcsin, Dagum, Erlang, Frechet, Gamma,
  GammaInverse, Kumaraswamy, Laplace, Logistic, Nakagami, StudentT,
  UniformSum), each requiring a distinct, correct, closed-form CDF derived
  from the distribution's own math (via Wikipedia references in the issue).
- Base commit `7b127bdf71a3...`, plain venv (no Docker — an explicit,
  stated scope reduction under time pressure, unlike the Docker-verified
  pytest-7220 runs).
- `iteration_budget=100000`, `max_wall_clock_seconds=25200` (7h),
  `memory_policy="bounded-structured"`, `requires_code_changes=True`,
  test files excluded from `forbidden_paths` (agent never saw `test_patch`).
- Task text was the issue's own `problem_statement`, unmodified.

## Reliability result: the infra held

Unlike the first launch attempt (which died at iteration 4 to an uncaught
`ollama._types.ResponseError` XML-parsing error), this run hit the same
transient error 3 times and recovered via retry every time, then ran for the
**entire 7-hour wall-clock budget** — 3,379 iterations — before terminating
via `wall_clock_exhausted`, not a crash. This is the first fully-stable
unattended overnight run in this whole investigation. `finish_task` was never
called — the model never declared itself done, which is the honest outcome
given the task was genuinely incomplete (see below).

## Result: 1 of 12 required fixes landed, and it's correct

Across 3,379 iterations, exactly two `patch_file` calls succeeded:

1. Added a closed-form `_cdf` to `ArcsinDistribution` (iteration ~2565).
2. Added the `asin, acos, asec` imports it needed (iteration ~2845).

Independently re-verified just now (not trusting the model's own claim),
directly against the checkout's own sympy:

```
CDF: Piecewise((0, x <= 0), (asin(2*x/3 - 1)/pi + 1/2, x < 3), (1, True))
diff(cdf) simplified == PDF: True
cdf(1) = -asin(1/3)/pi + 1/2 = 0.3918265520306073  (finite, closed-form)
```

Correct. But 11 of the 12 required distributions were never touched. This is
not a resolved instance — it's a genuine, verified partial result.

## The core finding: this is NOT the same failure mode as pytest-7220

pytest-7220's problem was **turn-level** analysis paralysis: correct
diagnosis by turn ~20, but a delay before converting it into one `patch_file`
call. Every mechanism built for it (watchdog, repetition detector, confidence
checkpoint) targets that specific gap, and on this harder instance, they all
fired exactly as designed:

- **Repetition detector**: fired at turns 117, 333, 469, 840, and 2399 —
  `'_cdf'` repeatedly flagged as the named cause across separate turns,
  each time forcing an explicit-action message.
- **Watchdog** (`turns_since_last_write`): fired **3,286 times out of 3,379
  iterations — 97% of the entire run**. The model was in an overdue-for-a-
  write state for almost the whole 7 hours.
- **Subgoal grace-period auto-close**: fired 7 times (`sg-01` through
  `sg-06`, then `sg-08`), each correctly detecting "real progress happened
  (reads/greps) but the subgoal never got a real completion or a written
  fix," and closing it rather than letting it hang open forever — exactly
  the behavior this mechanism was built for earlier this week.

All three mechanisms worked. The task still didn't get done, because the
actual failure mode here is one turn-level fixes don't touch:

**The model recreated the *identical* subgoal 8 separate times** (`sg-01`
through `sg-08`, all with the same goal text — "add `_cdf` to Arcsin, Dagum,
Erlang, Frechet, Gamma, GammaInverse, Kumaraswamy, Laplace, Logistic,
Nakagami, StudentT, UniformSum"), and **each time re-investigated from
scratch** — re-reading `crv_types.py`, re-grepping for the same class
locations it had already found multiple times before — rather than picking
up where a prior attempt left off. Auto-close's own generated episode
summaries make this explicit:

> [sg-03]: "...confirmed via `grep` that `_cdf` methods already exist in
> these files (indicating the premise of the subgoal may be flawed)..."

> [sg-06]: "...identified the locations of 12 specific distribution
> classes... located existing `_cdf` method implementations... read the
> entire `crv_types.py` source... only performed investigation and reading
> actions; it did not actually implement or write any new code
> modifications."

This is a distinct failure mode from turn-level analysis paralysis: call it
**subgoal-level amnesia** — the model isn't failing to act on a correct
diagnosis in the moment, it's failing to *remember it already scoped this
exact plan several times before* and ratchet toward execution instead of
re-scoping. The clearest single data point: at iteration 3378, the model's
own stated intent was

> "I need to stop investigating and start writing code immediately. Let me
> find the exact line numbers for each distribution class and then patch
> them all."

— immediately followed by two more `grep_dir`/`search_file` calls, not a
`patch_file` call. This is the exact "structural compliance without
behavioral follow-through" gap `SWEBENCH_ANALYSIS_PARALYSIS_FINDINGS.md`'s
run 10 also observed — but there, the mechanisms eventually broke the loop
before budget ran out. Here, across 8 cycles and the full 7-hour budget, they
never did.

## Why this is a harder problem to fix than turn-level paralysis

The existing mechanisms are turn-scoped (repetition detector looks at the
last few turns' language; the watchdog counts turns since a write). None of
them carry information *across* a subgoal's full lifecycle — there's nothing
today that would let subgoal `sg-08`'s prompt context say "you already
created and abandoned this exact plan 7 times; here is specifically what was
already confirmed (class locations, existing patterns) — do not re-derive
it, execute it." The subgoal ledger records completion/auto-close, but
recreating a subgoal with the same goal text isn't currently detected or
discouraged at creation time — it's treated as a fresh plan every time.

## Honest scope caveats

- **n=1.** One instance, no repeats, no ablation of which single mechanism
  (if any) would have helped — unlike pytest-7220's 10-run series, time
  pressure meant this ran once.
- **No Docker/official grading** — correctness of the one landed fix was
  verified by hand (differentiation + numeric check against the checkout's
  own sympy), not the official SWE-bench harness. PASS_TO_PASS regression
  risk from the two changed lines was not run against the full test suite.
- **Task difficulty is confounded with task *shape*** — this instance is
  harder both because the math is harder per-distribution *and* because it
  requires 12 independent sustained fixes instead of 1. This report can't
  cleanly separate "harder math" from "amnesia across a longer task,"
  though the repeated identical re-scoping (not repeated *wrong* math
  attempts) points at the latter as the dominant effect.
- **The forbidden_paths / no-test-patch setup matches established
  protocol**, but without Docker grading, the *other* 11 distributions'
  correctness (had they been written) is unverified by construction — only
  the one that got made can be checked at all.

## Recommendation for next session

Before building a fix, get one more real data point: does subgoal-level
amnesia reproduce on a *different* multi-part instance, or is this specific
to how unusually large this instance's fan-out is (12 sub-fixes)? If it
reproduces, the concrete next mechanism to prototype is a creation-time
check in `subgoal_create` — flag (not block) when the new goal text is a
near-duplicate of a `closed`/`auto-closed` subgoal, and inject that prior
subgoal's own episode summary directly into context as "already known, do
not re-derive."
