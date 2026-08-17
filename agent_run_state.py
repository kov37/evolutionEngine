"""Container for run_agent's cross-turn subsystem handles.

Before this, `wm`, `state`, `ws`, `ledger`, `contract`, `escalation`,
`risk`, `transaction`, `novelty_context`, and `lifecycle` were ten
independent loop-local variables in `agent.py::run_agent`, each built by
its own multi-line conditional at setup and threaded individually through
the rest of the ~2000-line function body. Each object is already a clean,
well-encapsulated subsystem (see each module's own docstring) — the
problem was never those objects, it was that nothing held them together,
so a helper that needed four of them needed four separate parameters
instead of one.

`RepairState` (repair_state.py) and `RecoveryState` (recovery_state.py)
deliberately stay OUT of this container: they already achieved the same
consolidation independently and are used pervasively throughout
`run_agent`, so folding them in here would only add an `.agent_state.`
prefix to every call site those two earlier steps already rewired, with
no reduction in complexity.

See ``~/.claude/plans/agile-forging-salamander.md`` (Step E).
"""

from __future__ import annotations

from dataclasses import dataclass

import action_governor
import escalation_governor
import risk_layer
import structured_state
import task_contract
import transaction_buffer
import working_state
from kernel.sandbox import get_root
from lifecycle_fsm import LifecycleFSM
from novelty_context import NoveltyContext
from working_memory import WorkingMemory, build_working_memory


@dataclass
class AgentRunState:
    """The ten cross-turn subsystem handles for one run_agent() call."""

    working_memory: WorkingMemory | None
    structured_state: structured_state.StructuredState | None
    working_state: working_state.WorkingState | None
    ledger: action_governor.EvidenceLedger | None
    contract: task_contract.TaskContract
    escalation: escalation_governor.EscalationState | None
    risk: risk_layer.RiskLayer | None
    transaction: transaction_buffer.TransactionBuffer | None
    novelty: NoveltyContext | None
    lifecycle: LifecycleFSM

    @classmethod
    def create(
        cls,
        *,
        task: str,
        task_type: str,
        working_memory_enabled: bool,
        structured_summary_enabled: bool,
        working_state_enabled: bool,
        novelty_context_enabled: bool,
        novelty_worker_model: str,
        governed: bool,
    ) -> "AgentRunState":
        """Build every subsystem exactly as run_agent's setup block did,
        including the off-by-default gating each one already had."""
        working_memory_obj = build_working_memory(task) if working_memory_enabled else None
        structured_state_obj = (
            structured_state.StructuredState(task) if structured_summary_enabled else None
        )
        working_state_obj = (
            working_state.WorkingState(
                objective=(task if len(task) <= 300 else task[:300] + "...(truncated)"),
                task_type=task_type,
            )
            if working_state_enabled else None
        )
        ledger = action_governor.EvidenceLedger() if governed else None
        contract = task_contract.CONTRACTS_BY_TYPE.get(task_type, task_contract.CODE_CHANGE_CONTRACT)
        escalation = escalation_governor.EscalationState() if governed else None
        risk = risk_layer.RiskLayer() if governed else None
        transaction = (
            transaction_buffer.TransactionBuffer(get_root(), followup_turns=1)
            if governed else None
        )
        if risk is not None:
            risk.protect_existing_tests(get_root())
        novelty = NoveltyContext(worker_model=novelty_worker_model) if novelty_context_enabled else None
        return cls(
            working_memory=working_memory_obj,
            structured_state=structured_state_obj,
            working_state=working_state_obj,
            ledger=ledger,
            contract=contract,
            escalation=escalation,
            risk=risk,
            transaction=transaction,
            novelty=novelty,
            lifecycle=LifecycleFSM(),
        )
