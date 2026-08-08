"""Bounded context compiler — Phase 3 of AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md.

Replaces "replay the entire messages list" with "rebuild a bounded prompt
fresh each turn." Does not itself add subgoal/hypothesis structure — that's
Phase 4's controller. This only changes HOW context reaches the model, not
what decisions get made about it.
"""
