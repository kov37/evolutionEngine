"""Compacts the orchestrator's own turn-by-turn reasoning once it crosses a
token ceiling — a real, measured problem the existing tool-result pruning
(agent.py/kernel/memory.py) doesn't touch: `messages.append(msg)` in
agent.py's loop is unconditional, so the model's own `content` text (its
"thinking" prose, ~76 tokens/iteration measured live across a real run)
accumulates forever across the full iteration budget with nothing bounding
it, unlike tool results.

COMPACTION_TOKEN_CEILING (6000) comes from tonight's own measurements, not a
guess: system+task overhead runs ~2,500 tokens, the structured-state block
~1,500, and tool-result pruning's "5 recent" window can transiently spike to
~17,000 right after a full-file read (see kernel/io_tools.py's read_file
guidance). NUM_CTX is 32,768 — confirmed the actual throughput sweet spot in
this project's own context-ceiling test (REFACTORING_LEARNINGS.md), not
something to raise instead. 2,500 + 1,500 + 17,000 = 21,000, leaving ~11,700
tokens of real headroom; 6,000 keeps assistant chatter at roughly half that,
with margin for the other components to spike at the same time, while still
allowing several compaction cycles across a 200-iteration run rather than
compacting every few turns.

Reuses qwen3.5:4b (structured_state.py's STATUS_MODEL), not worker.py's 9b —
running a second resident model alongside the 35b orchestrator was the exact
cause of a real, measured memory-pressure collapse earlier tonight (33.6GB
of 36GB total resident, 9.4GB/10GB swap used, ~79MB free pages); reusing
that model for a second purpose here would undo that fix rather than risk
reintroducing it via a different code path.

Design constraint: Ollama's message format requires an assistant message
carrying tool_calls to be immediately followed by its paired tool-result
message(s) — removing or reordering messages risks breaking that pairing.
So compaction never splices `messages` (no index shifting, no structural
risk of misaligning a tool_call with its result). It only overwrites old
assistant messages' `content` field in place — the exact same safety shape
as agent.py's existing tool-result pruning — leaving tool_calls and every
tool-result message completely untouched. Like that pruning, this costs one
cache-invalidating call right after a compaction event (content changed
mid-prefix), then `messages` is stable again until the next one.
"""

from ollama import chat

COMPACTION_MODEL = "qwen3.5:4b"
COMPACTION_NUM_CTX = 8192
COMPACTION_TOKEN_CEILING = 6000
KEEP_RECENT_ITERATIONS = 5  # mirrors agent.py's KEEP_RECENT_RAW_RESULTS
COMPACTION_RETRIES = 2  # mirrors structured_state.py's STATUS_RETRIES

COMPACTION_PROMPT = """Summarize what the agent was doing/thinking across these earlier turns of an \
ongoing coding task, in ONE short dense paragraph. This is scratch reasoning, not the final record — \
deterministic facts, files explored, and edit outcomes are tracked separately elsewhere and don't need \
repeating here. Focus only on what a future turn would actually benefit from knowing: the approach being \
tried, any dead ends hit, anything genuinely useful that isn't just "read some files."

Task: {task}

Turns to summarize:
{turns_text}

Summary (one paragraph):"""


def compact_if_needed(messages, iteration_assistant_idx, iteration_tokens, compacted_iterations, task):
    """Check whether uncompacted assistant-turn tokens exceed the ceiling,
    and if so, compact every uncompacted turn except the most recent
    KEEP_RECENT_ITERATIONS. Mutates `messages` and `compacted_iterations` in
    place. Returns the sorted list of newly-compacted turn numbers (for the
    caller's own visibility print — NOT the cumulative set, since
    `compacted_iterations` accumulates across the whole run), or None if
    nothing was compacted this call.
    """
    uncompacted = sorted(i for i in iteration_tokens if i not in compacted_iterations)
    if len(uncompacted) <= KEEP_RECENT_ITERATIONS:
        return None
    compactable = uncompacted[:-KEEP_RECENT_ITERATIONS]
    total_tokens = sum(iteration_tokens[i] for i in compactable)
    if total_tokens <= COMPACTION_TOKEN_CEILING:
        return None

    turns_text = "\n".join(
        f"Turn {i}: {messages[iteration_assistant_idx[i]]['content']}"
        for i in compactable
        if messages[iteration_assistant_idx[i]]["content"]
    )
    if not turns_text.strip():
        # No prose in these turns, just tool calls — nothing to summarize,
        # no model call needed.
        summary = "(no reasoning text in these turns — see tool calls and structured state for what happened)"
    else:
        prompt = COMPACTION_PROMPT.format(task=task, turns_text=turns_text)
        summary = None
        for _ in range(COMPACTION_RETRIES):
            try:
                response = chat(
                    model=COMPACTION_MODEL, messages=[{"role": "user", "content": prompt}],
                    think=False, options={"num_ctx": COMPACTION_NUM_CTX},
                )
                text = (response.message.content or "").strip()
                if text:
                    summary = text
                    break
            except Exception:
                pass
        if not summary:
            # Leave these turns as uncompacted (don't touch compacted_iterations)
            # so the next call retries them instead of permanently exempting
            # them at full size — a real gap caught in review: marking them
            # "compacted" here without ever shrinking their content would
            # silently defeat the ceiling for exactly the turns that failed.
            return None

    first = compactable[0]
    messages[iteration_assistant_idx[first]]["content"] = (
        f"[Compacted summary of turns {first}-{compactable[-1]}]: {summary}"
    )
    for i in compactable[1:]:
        messages[iteration_assistant_idx[i]]["content"] = (
            f"(reasoning compacted — see summary at turn {first} above)"
        )
    compacted_iterations.update(compactable)
    return compactable


def _self_test() -> bool:
    # No live model call — checks the splice-free in-place-edit shape and
    # the ceiling/keep-recent math only. Live summarization behavior is
    # verified separately, against the real model.
    messages = []
    iteration_assistant_idx = {}
    iteration_tokens = {}
    for i in range(1, 11):
        messages.append({"role": "assistant", "content": f"thinking about turn {i}" * 20})
        iteration_assistant_idx[i] = len(messages) - 1
        iteration_tokens[i] = 1500  # 5 compactable turns (10 - 5 kept recent) * 1500 = 7500 > 6000 ceiling
        messages.append({"role": "tool", "content": f"result {i}"})

    compacted_iterations = set()
    original_message_count = len(messages)

    # Below the keep-recent floor: nothing compactable yet even though the
    # raw token math alone would trigger (10 - 5 = 5 iterations available,
    # equal to KEEP_RECENT_ITERATIONS, so still nothing compactable).
    changed = compact_if_needed(messages[:2], {1: 0}, {1: 700}, set(), "task")
    assert changed is None

    def fake_chat_summary(*args, **kwargs):
        class R:
            class message:
                content = "fake summary"
        return R()

    # Patch the module's own global `chat` name directly — importing this
    # module by name while running it as __main__ would create a SECOND,
    # separate module object, and patching that one wouldn't affect the
    # `chat` compact_if_needed actually resolves (a real bug caught by this
    # test itself on first run: silently exercised the REAL network call
    # path instead of the fake, only surfacing as a missing-summary assert).
    global chat
    real_chat = chat
    chat = fake_chat_summary
    try:
        changed = compact_if_needed(messages, iteration_assistant_idx, iteration_tokens, compacted_iterations, "task")
    finally:
        chat = real_chat

    assert changed == [1, 2, 3, 4, 5], "must return the newly-compacted range, not the cumulative set"
    assert len(messages) == original_message_count, "compaction must never change message COUNT, only content"
    assert "[Compacted summary of turns 1-5]" in messages[iteration_assistant_idx[1]]["content"]
    assert "fake summary" in messages[iteration_assistant_idx[1]]["content"]
    for i in range(2, 6):
        assert "reasoning compacted" in messages[iteration_assistant_idx[i]]["content"]
    for i in range(6, 11):
        assert "reasoning compacted" not in messages[iteration_assistant_idx[i]]["content"]
    assert compacted_iterations == {1, 2, 3, 4, 5}

    # Idempotent: calling again immediately with the same state must not
    # recompact or error (nothing new crosses the ceiling).
    changed_again = compact_if_needed(messages, iteration_assistant_idx, iteration_tokens, compacted_iterations, "task")
    assert changed_again is None

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   message_compaction.compact_if_needed (in-place, no message-count changes)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
