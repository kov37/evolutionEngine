"""Hypothesis ledger tools (hypothesis_record/hypothesis_resolve).

Statuses are prediction_observed / prediction_disconfirmed — never
"confirmed"/"rejected". That distinction is the whole point (see
AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md's "Enforceable evidence versus
semantic claims"): this only records that a narrow, predicted observation
did or didn't occur, never that the broader claim is proven true.
hypothesis_resolve requires citing a real event_id that postdates the
hypothesis — the same weak-but-real citation gate as controller/subgoals.py,
applied to claims instead of subgoal completion.
"""

from memory.events import event_seq, read_events

VALID_STATUSES = ("prediction_observed", "prediction_disconfirmed")


def _hypothesis_ledger(run_dir: str) -> dict:
    ledger = {}
    counter = 0
    for record in read_events(run_dir):
        if record.get("event_type") != "tool_call":
            continue
        name = record["payload"]["tool_name"]
        args = record["payload"].get("arguments", {})
        if name == "hypothesis_record":
            counter += 1
            ledger[f"hyp-{counter:02d}"] = {
                "claim": args.get("claim"), "prediction": args.get("prediction"),
                "falsifier": args.get("falsifier"), "created_event_id": record["event_id"],
                "status": "untested",
            }
        elif name == "hypothesis_resolve":
            entry = ledger.get(args.get("hypothesis_id"))
            if entry is not None and args.get("status") in VALID_STATUSES:
                entry["status"] = args["status"]
                entry["evidence_id"] = args.get("evidence_id")
    return ledger


def make_hypothesis_tools(run_store):
    """Returns (hypothesis_record, hypothesis_resolve), closures bound to
    one run — same factory pattern as controller/subgoals.py's
    make_subgoal_tools."""

    def hypothesis_record(claim: str, prediction: str, falsifier: str) -> str:
        """Record a hypothesis with a concrete, testable prediction —
        before you test it, not after.

        Args:
          claim: What you believe, e.g. "the wrong path is built because
            display_path uses os.getcwd() instead of the repo root".
          prediction: The specific observation that would occur if the
            claim is true, e.g. "changing os.getcwd() to repo_root and
            rerunning the failing test will make it pass".
          falsifier: The specific observation that would mean the claim is
            WRONG, e.g. "the test still fails the same way after that
            change".
        """
        ledger = _hypothesis_ledger(run_store.run_dir)
        hid = f"hyp-{len(ledger) + 1:02d}"
        return f"Recorded {hid}: {claim} (predicts: {prediction})"

    def hypothesis_resolve(hypothesis_id: str, status: str, evidence_id: str) -> str:
        """Resolve a hypothesis's prediction against a real, already-recorded
        event — never mark one resolved from reasoning alone.

        Args:
          hypothesis_id: The id returned by hypothesis_record, e.g. "hyp-01".
          status: Exactly "prediction_observed" (the predicted observation
            occurred) or "prediction_disconfirmed" (it didn't, or the
            falsifier fired). Never "confirmed"/"rejected" — this only
            records what was observed, not that the claim is proven true.
          evidence_id: The event_id (e.g. from a tool result) of the real
            observation this resolution is based on. Must be an event that
            happened AFTER this hypothesis was recorded.
        """
        if status not in VALID_STATUSES:
            return f"ERROR: status must be one of {VALID_STATUSES}, got '{status}'."

        ledger = _hypothesis_ledger(run_store.run_dir)
        entry = ledger.get(hypothesis_id)
        if entry is None:
            return f"ERROR: unknown hypothesis_id '{hypothesis_id}' — call hypothesis_record first."

        real_events = {r["event_id"] for r in read_events(run_store.run_dir) if r.get("event_type") != "corrupt_event"}
        if evidence_id not in real_events:
            return f"ERROR: evidence_id '{evidence_id}' does not match any real event in this run."
        if event_seq(evidence_id) <= event_seq(entry["created_event_id"]):
            return (f"ERROR: evidence_id '{evidence_id}' predates '{hypothesis_id}' — cite something that "
                     f"happened AFTER you recorded it, not before.")

        return f"'{hypothesis_id}' resolved: {status} (evidence: {evidence_id})"

    return hypothesis_record, hypothesis_resolve
