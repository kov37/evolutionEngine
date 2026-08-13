"""Piece 3 of the progress-governor system: task contracts.

Defines what "done" and "required" mean for a given run, so the governor
(action_governor.py's evidence ledger, plus the escalation logic built on
top of it) never assumes every task needs a MUTATE — a real, explicit gap
in tonight's earlier FORCE_EDIT_AFTER_ITERATIONS mechanism, which forced a
patch attempt unconditionally, regardless of whether the task actually
called for one. A pure diagnosis/analysis task should be pushed toward
DELIVER, never MUTATE, unless explicitly authorized.

expected_phases is an ORDER-INSENSITIVE set of capability classes the task
is expected to exercise before completion, not a strict sequence — real
runs interleave OBSERVE/MUTATE/VALIDATE freely (read, patch, test, patch
again), and demanding a strict phase order would be a worse fit to reality
than the fixed-iteration-count mechanisms this whole system replaces.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TaskContract:
    task_type: str
    completion_conditions: tuple  # human-readable strings, informational
    expected_phases: frozenset  # capability classes the task should exercise
    forbidden_without_authorization: frozenset = field(default_factory=frozenset)

    def requires(self, capability: str) -> bool:
        """Whether this contract genuinely expects `capability` to occur at
        least once before the task can be considered complete."""
        return capability in self.expected_phases and capability not in self.forbidden_without_authorization


CODE_CHANGE_CONTRACT = TaskContract(
    task_type="code_change",
    completion_conditions=(
        "A relevant workspace mutation exists",
        "Requested behavior is implemented",
        "Validation has been attempted",
    ),
    expected_phases=frozenset({"OBSERVE", "MUTATE", "VALIDATE", "DELIVER"}),
)

DIAGNOSIS_CONTRACT = TaskContract(
    task_type="diagnosis",
    completion_conditions=(
        "Cause is identified with evidence",
        "Findings are delivered",
    ),
    expected_phases=frozenset({"OBSERVE", "DELIVER"}),
    forbidden_without_authorization=frozenset({"MUTATE"}),
)

CONTRACTS_BY_TYPE = {
    "code_change": CODE_CHANGE_CONTRACT,
    "diagnosis": DIAGNOSIS_CONTRACT,
}


def missing_required_capabilities(contract: TaskContract, capabilities_seen: set) -> list:
    """Which of the contract's genuinely-required capabilities (accounting
    for forbidden_without_authorization) haven't occurred yet, in
    expected_phases order where that's meaningful (OBSERVE before MUTATE
    before VALIDATE before DELIVER, the natural progression even though
    it's not strictly enforced) — used to tell the model exactly what's
    still missing, not just that something is."""
    order = ["OBSERVE", "PLAN", "MUTATE", "VALIDATE", "DELIVER"]
    return [
        cap for cap in order
        if contract.requires(cap) and cap not in capabilities_seen
    ]


def _self_test() -> bool:
    # A code_change contract must require MUTATE.
    assert CODE_CHANGE_CONTRACT.requires("MUTATE") is True
    assert CODE_CHANGE_CONTRACT.requires("OBSERVE") is True
    assert CODE_CHANGE_CONTRACT.requires("PLAN") is False  # not in expected_phases at all

    # A diagnosis contract must NOT require MUTATE, even though nothing
    # stops the model from doing one if it chooses to — "forbidden without
    # authorization" means the governor won't PUSH toward it, not that the
    # tool itself is blocked.
    assert DIAGNOSIS_CONTRACT.requires("MUTATE") is False
    assert DIAGNOSIS_CONTRACT.requires("OBSERVE") is True
    assert DIAGNOSIS_CONTRACT.requires("DELIVER") is True

    # missing_required_capabilities: code_change with only OBSERVE seen so
    # far must report MUTATE/VALIDATE/DELIVER still missing, in that order.
    missing = missing_required_capabilities(CODE_CHANGE_CONTRACT, {"OBSERVE"})
    assert missing == ["MUTATE", "VALIDATE", "DELIVER"], missing

    # Once MUTATE has happened too, only VALIDATE/DELIVER remain.
    missing2 = missing_required_capabilities(CODE_CHANGE_CONTRACT, {"OBSERVE", "MUTATE"})
    assert missing2 == ["VALIDATE", "DELIVER"], missing2

    # A fully-satisfied contract reports nothing missing.
    missing3 = missing_required_capabilities(CODE_CHANGE_CONTRACT, {"OBSERVE", "MUTATE", "VALIDATE", "DELIVER"})
    assert missing3 == [], missing3

    # diagnosis contract with only OBSERVE seen must NOT list MUTATE as
    # missing (it isn't required at all) — only DELIVER.
    missing4 = missing_required_capabilities(DIAGNOSIS_CONTRACT, {"OBSERVE"})
    assert missing4 == ["DELIVER"], missing4

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   task_contract (TaskContract + missing_required_capabilities)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
