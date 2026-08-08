"""Subgoal tools (subgoal_create/subgoal_complete) — the mechanism
actually targeting "plan instability" (pylint-4551: the model never
progressed past re-litigating one line across a 100-turn budget on a
4-file fix). Creation is cheap to allow — it's the model stating its own
plan, and HiAgent's own ablation found that's where most of the benefit
comes from. Completion is evidence-gated: subgoal_complete only succeeds
if something reducer-visible (memory/reducers.py's reduce_state — a
new/changed entity, a test run, a resolved hypothesis) happened AFTER the
subgoal was created. This does NOT verify the model's claim is
semantically true (AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md's "Enforceable
evidence versus semantic claims") — only that real work happened, not
just re-stated diagnosis.

Stateless by design, like memory/reducers.py: every call re-derives the
subgoal ledger from the run's own event log rather than holding private
in-process bookkeeping that could drift from the durable record.
"""

from memory.episodes import create_episode
from memory.events import event_seq, read_events

BOOKKEEPING_TOOLS = {"subgoal_create", "subgoal_complete", "subgoal_auto_close"}


CLOSE_TOOLS = {"subgoal_complete", "subgoal_auto_close"}


def subgoal_ledger(run_dir: str) -> dict:
    """{subgoal_id: {"goal", "success_condition", "created_event_id", "status", "auto_closed"}}.
    IDs are assigned positionally (the Nth subgoal_create call is always
    sg-0N) so no id needs to be parsed back out of a result string.
    subgoal_auto_close is a synthetic tool_call agent.py records directly
    (never model-invoked) when the runtime, not the model, closes a
    subgoal at a transition point — see auto_close_open_subgoals()."""
    ledger = {}
    counter = 0
    for record in read_events(run_dir):
        if record.get("event_type") != "tool_call":
            continue
        name = record["payload"]["tool_name"]
        args = record["payload"].get("arguments", {})
        if name == "subgoal_create":
            counter += 1
            ledger[f"sg-{counter:02d}"] = {
                "goal": args.get("goal"), "success_condition": args.get("success_condition"),
                "created_event_id": record["event_id"], "status": "open", "auto_closed": False,
            }
        elif name in CLOSE_TOOLS:
            # A live run surfaced this: a subgoal_complete call with a bad
            # extra argument fails with a TypeError before the function
            # body even runs (dispatch.py's own error handling), but the
            # arguments the model TRIED to pass — including subgoal_id —
            # are still what's recorded on the event. Without this check,
            # that failed call still marked the subgoal completed, and the
            # model's real, valid completion attempt one turn later was
            # then rejected as "already marked complete" — silently
            # losing a legitimate completion (and its episode) to a typo.
            if record["payload"].get("result_preview", "").startswith(("ERROR", "REJECTED")):
                continue
            entry = ledger.get(args.get("subgoal_id"))
            if entry is not None:
                entry["status"] = "completed"
                entry["auto_closed"] = (name == "subgoal_auto_close")
    return ledger


def most_recent_progress_event_id(run_dir: str, since_event_id: str, exclude_tools=frozenset()):
    """The event_id of the LATEST successful (non-ERROR/REJECTED) tool call
    after since_event_id, excluding bookkeeping calls — or None. Used both
    as a boolean progress check and (Phase 5) as the natural upper bound
    for an episode's raw event range: subgoal_complete's own call hasn't
    been recorded yet at the point it needs this, so the last REAL progress
    event is the right "end" for what actually happened, not a later
    bookkeeping call with nothing new in it."""
    since_seq = event_seq(since_event_id)
    latest = None
    for record in read_events(run_dir):
        if record.get("event_type") != "tool_call":
            continue
        if event_seq(record["event_id"]) <= since_seq:
            continue
        name = record["payload"]["tool_name"]
        if name in exclude_tools:
            continue
        if record["payload"].get("result_preview", "").startswith(("ERROR", "REJECTED")):
            continue
        latest = record["event_id"]
    return latest


def has_real_progress_since(run_dir: str, since_event_id: str, exclude_tools=frozenset()) -> bool:
    """Shared by subgoal completion gating and (Phase 4 item 5) the
    cross-subgoal stagnation detector."""
    return most_recent_progress_event_id(run_dir, since_event_id, exclude_tools) is not None


def open_subgoals_with_progress(run_dir: str) -> dict:
    """Open subgoals that already have real progress recorded since
    creation — candidates for either an explicit subgoal_complete (the
    enforced-grammar nudge in agent.py prompts for this) or an eventual
    auto-close at the next transition point. Open subgoals with NO
    progress yet are excluded — nothing to nudge about or close."""
    ledger = subgoal_ledger(run_dir)
    return {
        sid: entry for sid, entry in ledger.items()
        if entry["status"] == "open"
        and most_recent_progress_event_id(run_dir, entry["created_event_id"], exclude_tools=BOOKKEEPING_TOOLS)
    }


def _create_episode_safely(run_store, model, subgoal_id, entry, conclusion, to_event_id, auto_closed=False):
    """Episode creation is an enrichment, not a correctness gate — the
    real evidence check already passed by the time this is called. A
    summarization call failing (model/network hiccup) must not turn a
    legitimately earned completion into a rejection."""
    try:
        create_episode(run_store.run_dir, subgoal_id, entry["goal"], entry["success_condition"], conclusion,
                        entry["created_event_id"], to_event_id, model, auto_closed=auto_closed)
    except Exception as e:
        print(f"⚠️  Episode summary for '{subgoal_id}' failed ({type(e).__name__}: {e}) — "
              f"completion still stands, just without a summary.")


def auto_close_open_subgoals(run_store, model: str, iteration: int, reason: str) -> list:
    """Called by agent.py's loop at a subgoal transition (a new
    subgoal_create while an earlier one is still open, or finish_task
    called with subgoals still open) — separation of concerns per the
    user's own framing: state tracking belongs to the deterministic
    runtime, not to whether the model remembers to call subgoal_complete.

    Deliberately does NOT try to detect that a subgoal's free-text
    success_condition was semantically satisfied — the runtime can't
    evaluate that any more than it can verify any other model claim (see
    AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md's "Enforceable evidence versus
    semantic claims"). It closes on the same weak-but-real signal
    subgoal_complete's own gate already uses: real progress happened.
    "Moving on" (creating the next subgoal, or calling finish_task) is
    itself a reducer-visible transition, not a semantic judgment — that's
    what's mechanically detected here, not goal satisfaction.

    An open subgoal with NO real progress is left open, not force-closed
    — auto-closing an empty subgoal would create a garbage episode.
    Returns the list of subgoal_ids that were auto-closed."""
    closed = []
    for subgoal_id, entry in open_subgoals_with_progress(run_store.run_dir).items():
        to_event_id = most_recent_progress_event_id(run_store.run_dir, entry["created_event_id"],
                                                     exclude_tools=BOOKKEEPING_TOOLS)
        conclusion = f"Auto-closed by runtime ({reason}): moved on before an explicit subgoal_complete call."
        run_store.record_tool_call(
            iteration=iteration, tool_name="subgoal_auto_close",
            arguments={"subgoal_id": subgoal_id, "reason": reason}, result_text=conclusion,
        )
        _create_episode_safely(run_store, model, subgoal_id, entry, conclusion, to_event_id, auto_closed=True)
        closed.append(subgoal_id)
    return closed


def make_subgoal_tools(run_store, model: str):
    """Returns (subgoal_create, subgoal_complete), closures bound to one
    run — mirrors docker_verify_tools.make_run_shell_in_container's
    factory pattern. Both are ordinary tool functions: dispatch_tool_calls
    records them exactly like any other tool call, so the ledger above can
    always be rebuilt from the same event log everything else already
    goes through.

    model is threaded in (not imported from agent.py) to avoid a circular
    import — it's only used for the Phase 5 episode summary generated on
    a successful subgoal_complete."""

    def subgoal_create(goal: str, success_condition: str) -> str:
        """Declare a subgoal: a concrete, checkable piece of the overall
        task you're about to work on. Use this to break a multi-file or
        multi-step fix into pieces BEFORE diving into the first one — state
        your whole plan, not just the next single action.

        Args:
          goal: What this subgoal accomplishes, e.g. "add get_annotation()
            to utils.py".
          success_condition: What observable outcome would mean this
            subgoal is actually done, e.g. "utils.py contains
            get_annotation and test_get_annotation_annassign passes". Be
            concrete — subgoal_complete on this subgoal is rejected unless
            real work happened after this call, not just more reasoning.
        """
        ledger = subgoal_ledger(run_store.run_dir)
        sid = f"sg-{len(ledger) + 1:02d}"
        return f"Created {sid}: {goal} (success condition: {success_condition})"

    def subgoal_complete(subgoal_id: str, conclusion: str, evidence_ids: str = "") -> str:
        """Mark a subgoal complete. REJECTED unless something real (a file
        read/changed, a test run, a resolved hypothesis) has been recorded
        since it was created — restating your reasoning again does not
        satisfy this. If nothing has actually happened yet, go do that
        first, then call this.

        Args:
          subgoal_id: The id returned by subgoal_create, e.g. "sg-01".
          conclusion: What you found or did for this subgoal.
          evidence_ids: Optional comma-separated event_id references
            supporting the conclusion.
        """
        ledger = subgoal_ledger(run_store.run_dir)
        entry = ledger.get(subgoal_id)
        if entry is None:
            return f"ERROR: unknown subgoal_id '{subgoal_id}' — call subgoal_create first."
        if entry["status"] == "completed":
            return f"ERROR: '{subgoal_id}' was already marked complete."

        to_event_id = most_recent_progress_event_id(run_store.run_dir, entry["created_event_id"],
                                                     exclude_tools=BOOKKEEPING_TOOLS)
        if to_event_id is None:
            return (
                f"REJECTED: no new evidence (file read/changed, test run, hypothesis resolved) has been "
                f"recorded since '{subgoal_id}' was created. Restating your conclusion is not enough — "
                f"go do something that produces a real, tool-recorded observation, then call this again."
            )

        _create_episode_safely(run_store, model, subgoal_id, entry, conclusion, to_event_id)
        return f"'{subgoal_id}' marked complete: {conclusion}"

    return subgoal_create, subgoal_complete
