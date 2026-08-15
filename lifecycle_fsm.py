"""Deterministic lifecycle state machine for the agent loop.

The FSM owns phase transitions only. Tool parsing, risk checks, and task
contracts remain separate policies. Keeping those concerns separate makes the
engine easier to test without coupling it to a model or benchmark.
"""

from enum import Enum


class LifecycleState(str, Enum):
    ORIENT = "orient"
    ACT = "act"
    VALIDATE = "validate"
    REPAIR = "repair"
    RECOVER = "recover"
    COMPLETE = "complete"
    FAILED = "failed"


class InvalidTransition(RuntimeError):
    """Raised when the orchestrator attempts an impossible lifecycle move."""


_TRANSITIONS = {
    (LifecycleState.ORIENT, "turn"): LifecycleState.ACT,
    (LifecycleState.ACT, "turn"): LifecycleState.ACT,
    (LifecycleState.VALIDATE, "turn"): LifecycleState.VALIDATE,
    (LifecycleState.REPAIR, "turn"): LifecycleState.REPAIR,
    (LifecycleState.RECOVER, "turn"): LifecycleState.RECOVER,
    (LifecycleState.ACT, "mutation"): LifecycleState.VALIDATE,
    (LifecycleState.REPAIR, "mutation"): LifecycleState.VALIDATE,
    (LifecycleState.RECOVER, "mutation"): LifecycleState.VALIDATE,
    (LifecycleState.VALIDATE, "validation_failed"): LifecycleState.REPAIR,
    (LifecycleState.REPAIR, "validation_failed"): LifecycleState.REPAIR,
    (LifecycleState.RECOVER, "validation_failed"): LifecycleState.RECOVER,
    (LifecycleState.REPAIR, "recovery_budget_exhausted"): LifecycleState.RECOVER,
    (LifecycleState.VALIDATE, "validation_partial"): LifecycleState.VALIDATE,
    (LifecycleState.VALIDATE, "validation_passed"): LifecycleState.COMPLETE,
    (LifecycleState.RECOVER, "validation_passed"): LifecycleState.COMPLETE,
    (LifecycleState.ACT, "provider_error"): LifecycleState.FAILED,
    (LifecycleState.VALIDATE, "provider_error"): LifecycleState.FAILED,
    (LifecycleState.REPAIR, "provider_error"): LifecycleState.FAILED,
    (LifecycleState.RECOVER, "provider_error"): LifecycleState.FAILED,
}


class LifecycleFSM:
    """Small strict FSM with an auditable transition history."""

    def __init__(self):
        self.state = LifecycleState.ORIENT
        self.history = []

    def transition(self, event):
        key = (self.state, str(event))
        next_state = _TRANSITIONS.get(key)
        if next_state is None:
            raise InvalidTransition(
                f"invalid lifecycle event {event!r} from {self.state.value!r}"
            )
        previous = self.state
        self.state = next_state
        self.history.append({
            "from": previous.value,
            "event": str(event),
            "to": next_state.value,
        })
        return self.state

    def metrics(self):
        return {
            "state": self.state.value,
            "transitions": len(self.history),
            "last_transition": self.history[-1] if self.history else None,
        }
