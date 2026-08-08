"""Evidence-gated controller — Phase 4 of AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md.

Every mechanism here follows the doc's "Enforceable evidence versus
semantic claims" distinction: it can check that memory/reducers.py's
reduce_state() shows something changed, never that a model's claim is
true. Naming follows the doc's vocabulary — evidence_linked,
prediction_observed/prediction_disconfirmed, mechanically_verified — on
purpose, never "confirmed"/"proven".
"""
