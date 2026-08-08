"""Lossless event/artifact store — Phase 0/1 of AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md.

Records every model call and tool call agent.py makes, in full, before
dispatch.py's existing truncation ever happens — so the discarded portion
of a large tool result is no longer gone forever, just outside the prompt.
Deliberately does NOT change what the model sees on any given turn: this
phase is instrumentation only, not a new context or controller policy.
"""
