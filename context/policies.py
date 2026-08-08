"""Context-assembly policies. Each takes messages[0:2] (system + task —
the immutable contract, kept byte-identical, per the plan's explicit
requirement) and messages[2:] (the turn-by-turn tail) and returns the
list to actually send to the model this turn. Never mutates the input —
agent.py's own `messages` list stays the durable, complete record
regardless of which policy is active; what a policy returns is a
disposable view built fresh every turn.
"""

from context.budget import TOKEN_BUDGETS, estimate_tokens
from context.render import render_structured_state


def _role(m) -> str:
    return m["role"] if isinstance(m, dict) else m.role


def group_into_turns(tail):
    """Group the tail into per-iteration blocks: one assistant message
    plus everything that followed it, up to (not including) the next
    assistant message. A tool-role message is only ever valid paired with
    the assistant message whose tool_calls it answers — dropping from the
    middle of a block would send the API an invalid request, so every
    policy below keeps or drops WHOLE blocks only."""
    blocks = []
    for m in tail:
        if _role(m) == "assistant" or not blocks:
            blocks.append([m])
        else:
            blocks[-1].append(m)
    return blocks


def policy_append_all(system_and_task, tail, **kwargs):
    """The unmodified current behavior — mathematically identical to what
    agent.py sent before this phase existed. The permanent baseline every
    other policy gets compared against (design principle 10: every policy
    must be ablatable, starting from this one)."""
    return system_and_task + tail


def policy_sliding_window(system_and_task, tail, window_turns=6, **kwargs):
    """Keep only the last N full turns. No structured state, no
    retrieval — a deliberately dumb baseline between append-all and
    bounded-structured, per the plan's own comparison matrix."""
    blocks = group_into_turns(tail)
    kept = blocks[-window_turns:]
    return system_and_task + [m for block in kept for m in block]


def policy_bounded_structured(system_and_task, tail, run_dir=None, recent_turns=4, **kwargs):
    """Task contract + memory/reducers.py's materialized state (current
    entities/tests/failures, NOT raw tool output — see context/render.py)
    + a short recent tail. reduce_state() folds the FULL event log every
    call, so a test failure or file change from turn 3 still appears in
    the rendered state at turn 200 even though it long ago fell out of
    the recent tail — state doesn't decay with turn count the way a pure
    sliding window does; only raw play-by-play detail does."""
    from memory.episodes import list_episodes
    from memory.reducers import reduce_state

    blocks = group_into_turns(tail)
    recent_blocks = blocks[-recent_turns:]

    state_text = ""
    if run_dir:
        state_text = render_structured_state(reduce_state(run_dir), episodes=list_episodes(run_dir))

    budget = TOKEN_BUDGETS["structured_state"] + TOKEN_BUDGETS["recent_tail"]
    while recent_blocks and estimate_tokens(state_text) + estimate_tokens(recent_blocks) > budget and len(recent_blocks) > 1:
        recent_blocks = recent_blocks[1:]  # drop oldest recent-turn block first — never truncate the state block itself

    recent_messages = [m for block in recent_blocks for m in block]
    state_message = [{"role": "user", "content": state_text}] if state_text else []
    return system_and_task + state_message + recent_messages


POLICIES = {
    "append-all": policy_append_all,
    "sliding-window": policy_sliding_window,
    "bounded-structured": policy_bounded_structured,
}

# flat-summary and hierarchy (Phase 3's remaining two policy names from the
# plan) are NOT implemented yet — both need an LLM-generated summary keyed
# to subgoal boundaries, and subgoals are Phase 4's controller, which
# doesn't exist. Building a summarization policy with nothing to summarize
# BY would mean inventing arbitrary boundaries now and redoing it once
# Phase 4 provides real ones.
