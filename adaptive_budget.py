"""Piece 5: adaptive discovery budget — replaces a fixed evidence/iteration
threshold (like the removed FORCE_EDIT_AFTER_ITERATIONS=8 constant) with
one that scales with real, measurable task characteristics, per the design:

    budget = baseBudget + repositorySizeFactor + taskAmbiguityFactor
             + failureRecoveryAllowance

Takes explicit primitive inputs (task text, repo_size_hint, failed_action_
count) rather than an EvidenceLedger directly — an earlier version derived
repo_size_factor from ledger.distinct_scopes_touched(), which is a REAL bug
caught by this module's own self-test: that value comes from the SAME
ledger being evaluated for "has enough evidence accumulated," so touching
more files simultaneously inflated both the evidence count AND the
threshold it's compared against, self-referentially delaying the avoidance
signal exactly when it should have fired sooner. repo_size_hint must come
from something independent of the run's own exploration history — e.g. a
file count taken once from the actual project directory, not "how much
has been explored so far."

failed_action_count, by contrast, IS fine to source from the ledger being
evaluated: a real failed MUTATE/VALIDATE is a genuinely independent signal
(the task got objectively harder, not "the model chose to explore more"),
so there's no equivalent circularity.
"""

BASE_BUDGET = 4  # minimum evidence entries expected even for a trivial task

# Credit per file in the project (or per some other external size hint) —
# a task against a larger codebase legitimately needs more OBSERVE calls
# before a MUTATE is reasonable. Capped so an enormous repo doesn't grant
# unbounded budget.
REPO_SIZE_CREDIT_PER_UNIT = 1
MAX_REPO_SIZE_CREDIT = 4

# Credit per ~400 characters of task description — crude proxy for how
# much the task itself demands understanding before acting; a one-line
# task needs less exploration room than a multi-paragraph issue with many
# named requirements (like tonight's real sympy-13878 issue, which lists
# 12 separate distributions).
AMBIGUITY_CREDIT_PER_CHARS = 400
MAX_AMBIGUITY_CREDIT = 6  # cap — task prose must not become an analysis-paralysis budget

# Credit per failed MUTATE/VALIDATE attempt — debugging a real failure
# legitimately needs more re-observation than the baseline case.
FAILURE_RECOVERY_CREDIT = 3


def compute_budget(task: str, repo_size_hint: int = 0, failed_action_count: int = 0) -> int:
    """The number of distinct pieces of new evidence (EvidenceLedger.
    new_evidence_count()) expected before a genuinely-required-but-missing
    MUTATE is treated as avoidance rather than legitimate exploration.

    repo_size_hint: an external measure of task/codebase scale (e.g. file
    count) — deliberately NOT derived from the ledger being evaluated, see
    module docstring for why that would be circular. Defaults to 0 (no
    hint available), contributing zero credit rather than guessing.
    failed_action_count: real failed MUTATE/VALIDATE attempts so far —
    safe to source from the same ledger (see docstring)."""
    repo_size_factor = min(MAX_REPO_SIZE_CREDIT, REPO_SIZE_CREDIT_PER_UNIT * max(0, repo_size_hint))
    ambiguity_factor = min(MAX_AMBIGUITY_CREDIT, len(task or "") // AMBIGUITY_CREDIT_PER_CHARS)
    failure_recovery_allowance = FAILURE_RECOVERY_CREDIT * max(0, failed_action_count)
    return BASE_BUDGET + repo_size_factor + ambiguity_factor + failure_recovery_allowance


def _self_test() -> bool:
    # A trivial task, no repo-size hint, no failures — budget is just the
    # base.
    assert compute_budget("fix a typo") == BASE_BUDGET

    # A larger repo_size_hint raises the budget, up to the cap.
    assert compute_budget("fix a typo", repo_size_hint=3) == BASE_BUDGET + 3
    assert compute_budget("fix a typo", repo_size_hint=100) == BASE_BUDGET + MAX_REPO_SIZE_CREDIT, (
        "repo size credit must be capped, not unbounded"
    )

    # A longer task description raises the budget, capped.
    long_task = "x" * 3000  # 3000 // 400 = 7, capped at 6
    assert compute_budget(long_task) == BASE_BUDGET + MAX_AMBIGUITY_CREDIT
    longer_task = "x" * 10000
    assert compute_budget(longer_task) == compute_budget(long_task), (
        "ambiguity credit must be capped, not unbounded"
    )

    # A failed MUTATE/VALIDATE attempt raises the budget — real recovery
    # credit, matching tonight's own observed pattern (a failed patch
    # legitimately needs a re-read before the retry).
    assert compute_budget("fix a typo", failed_action_count=1) == BASE_BUDGET + FAILURE_RECOVERY_CREDIT

    # All factors combine additively, independently of each other — no
    # circularity between repo_size_hint and evidence count, since both
    # are now explicit primitive inputs, not derived from the same ledger.
    combined = compute_budget(long_task, repo_size_hint=2, failed_action_count=1)
    expected = BASE_BUDGET + 2 * REPO_SIZE_CREDIT_PER_UNIT + MAX_AMBIGUITY_CREDIT + FAILURE_RECOVERY_CREDIT
    assert combined == expected, (combined, expected)

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   adaptive_budget (compute_budget: base + repo-size + ambiguity + failure-recovery, decoupled inputs)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
