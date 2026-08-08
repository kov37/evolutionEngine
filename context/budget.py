"""Token budget allocation and rough estimation for the context compiler.

No real tokenizer is vendored — estimation is chars/4, a common rough
approximation, good enough to decide what to trim BEFORE the real call
happens. Actual usage is still measured precisely elsewhere via Ollama's
own prompt_eval_count (memory/store.py's record_model_call) — that stays
the source of truth; this estimator only drives assembly-time decisions.

Only budgets for components this compiler actually produces are listed.
The plan's own initial-allocation table also has "retrieved evidence"
(5,000) and a separate "action/verification instructions" (1,000) line —
both skipped here: there's no retrieval yet (Phase 5), and action/
verification instructions are already part of agent.py's system prompt,
not a separate compiled section. Values are the plan's own proposed
32K-window numbers — provisional, not measured: Phase 0 found num_ctx is
not actually set for qwen3.6:35b-mlx, so the true effective window this
project has been running against was never confirmed. Revisit these once
that's resolved.
"""

TOKEN_BUDGETS = {
    "system_policy": 3000,
    "task_contract": 2000,
    "structured_state": 3000,
    "recent_tail": 3000,
}

CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(text) -> int:
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)
