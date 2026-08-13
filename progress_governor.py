"""Piece 4 of the progress-governor system: combines action_governor.py's
EvidenceLedger with task_contract.py's TaskContract into the two real
signals the escalation logic (piece 5/6) acts on:

- stagnating: the recent window of actions produced no new evidence and no
  MUTATE/DELIVER (EvidenceLedger.recent_progress already computes this).
- avoiding_required_phase: the task's contract genuinely requires a
  capability (e.g. MUTATE for a code_change task — NOT for a diagnosis
  task, see task_contract.py) that hasn't happened yet, AND there's already
  enough OBSERVE/VALIDATE evidence that further observation is unlikely to
  be the bottleneck.

Deliberately NOT iteration-count-based — "enough evidence" is measured by
new_evidence_count() (distinct, non-duplicate OBSERVE/VALIDATE calls), not
raw turns elapsed. A task needing genuinely more files/context legitimately
gets more room; one where the model is just re-reading the same content
does not, regardless of how many turns have passed either way.
"""

from dataclasses import dataclass

import adaptive_budget
import task_contract as tc

# recent_progress()'s window — how many of the most recent actions must all
# be non-new-evidence, non-MUTATE/DELIVER before calling it stagnation.
STAGNATION_WINDOW = 5


@dataclass
class ProgressSignal:
    stagnating: bool
    avoiding_required_phase: bool
    missing_capabilities: list
    new_evidence_count: int
    capabilities_seen: set

    def needs_intervention(self) -> bool:
        return self.stagnating or self.avoiding_required_phase


def evaluate(ledger, contract: tc.TaskContract, task: str = "", repo_size_hint: int = 0) -> ProgressSignal:
    capabilities_seen = ledger.capabilities_seen()
    missing = tc.missing_required_capabilities(contract, capabilities_seen)
    evidence_count = ledger.new_evidence_count()
    budget = adaptive_budget.compute_budget(
        task, repo_size_hint=repo_size_hint, failed_action_count=ledger.failed_action_count()
    )

    avoiding = "MUTATE" in missing and evidence_count >= budget
    stagnating = not ledger.recent_progress(window=STAGNATION_WINDOW)

    return ProgressSignal(
        stagnating=stagnating,
        avoiding_required_phase=avoiding,
        missing_capabilities=missing,
        new_evidence_count=evidence_count,
        capabilities_seen=capabilities_seen,
    )


def _self_test() -> bool:
    import action_governor as gov

    # --- Case 1: early in a code_change task, still gathering evidence —
    # NOT avoidance, even though MUTATE hasn't happened, because evidence
    # count is below the threshold.
    ledger = gov.EvidenceLedger()
    for i in range(3):
        ledger.record(i, "read_file", {"path": f"f{i}.py"}, f"content {i}")  # 3 distinct pieces of evidence
    signal = evaluate(ledger, tc.CODE_CHANGE_CONTRACT)
    assert signal.avoiding_required_phase is False, "too little evidence yet — not avoidance"
    assert "MUTATE" in signal.missing_capabilities

    # --- Case 2: plenty of distinct evidence, still zero MUTATE — this IS
    # avoidance for a code_change task.
    ledger2 = gov.EvidenceLedger()
    for i in range(10):
        ledger2.record(i, "read_file", {"path": f"f{i}.py"}, f"distinct content {i}")
    signal2 = evaluate(ledger2, tc.CODE_CHANGE_CONTRACT)
    assert signal2.avoiding_required_phase is True, "10 distinct observations with zero MUTATE is avoidance"

    # --- Case 3: same evidence pattern, but a DIAGNOSIS task — must NOT be
    # flagged as avoiding anything, since MUTATE isn't required for it.
    ledger3 = gov.EvidenceLedger()
    for i in range(10):
        ledger3.record(i, "read_file", {"path": f"f{i}.py"}, f"distinct content {i}")
    signal3 = evaluate(ledger3, tc.DIAGNOSIS_CONTRACT)
    assert signal3.avoiding_required_phase is False, "diagnosis tasks must never be flagged for missing MUTATE"

    # --- Case 4: real run-10 pattern — many duplicate reads, not enough
    # DISTINCT evidence despite a high raw call count. Must not falsely
    # treat "many calls" as "enough evidence."
    ledger4 = gov.EvidenceLedger()
    for i in range(20):
        ledger4.record(i, "read_file", {"path": "same.py"}, "always identical content")  # 1 real piece of evidence, 19 duplicates
    signal4 = evaluate(ledger4, tc.CODE_CHANGE_CONTRACT)
    assert signal4.new_evidence_count == 1, f"expected exactly 1 distinct evidence entry, got {signal4.new_evidence_count}"
    assert signal4.avoiding_required_phase is False, "20 duplicate reads is not 20 pieces of evidence"
    assert signal4.stagnating is True, "20 identical reads in a row is genuine stagnation"

    # --- Case 5: stagnating should be False once real progress interrupts
    # the duplicate-read streak, even close to the end of the window.
    ledger5 = gov.EvidenceLedger()
    for i in range(4):
        ledger5.record(i, "read_file", {"path": "same.py"}, "always identical content")
    ledger5.record(4, "patch_file", {"path": "same.py"}, "Wrote 'same.py'.")
    signal5 = evaluate(ledger5, tc.CODE_CHANGE_CONTRACT)
    assert signal5.stagnating is False, "a MUTATE in the recent window means not stagnating"

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   progress_governor (evaluate: stagnation + avoidance signals)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
