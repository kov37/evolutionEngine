"""Model-facing memory operations — the subset of the plan's memory_*
tool list actually needed for Phase 5's acceptance tests: retrieval
under a bounded budget, exact source expansion, quick orientation.
AgeMem-style write operations (memory_add/update/delete/filter) aren't
built — nothing yet needs the model to curate memory beyond what the
subgoal/hypothesis tools (Phase 4) already do.
"""

import os

from controller.hypotheses import hypothesis_ledger
from controller.phases import derive_phase
from controller.subgoals import subgoal_ledger
from memory.artifacts import load as load_artifact
from memory.events import read_events
from memory.reducers import reduce_state
from memory.retrieval import retrieve


def make_memory_tools(run_store):
    """Returns (memory_status, memory_recall, memory_expand), closures
    bound to one run — same factory pattern as controller/subgoals.py's
    make_subgoal_tools."""

    def memory_status() -> str:
        """Quick orientation: current phase, open/completed subgoals, open
        hypotheses, and the most recent failure. Call this if you're not
        sure what's already been established, instead of re-reading files
        to reconstruct it from scratch.
        """
        state = reduce_state(run_store.run_dir)
        lines = [f"Phase: {derive_phase(state)}"]

        subgoals = subgoal_ledger(run_store.run_dir)
        if subgoals:
            lines.append("Subgoals:")
            for sid, sg in subgoals.items():
                lines.append(f"  - {sid} [{sg['status']}]: {sg['goal']}")

        hypotheses = hypothesis_ledger(run_store.run_dir)
        if hypotheses:
            lines.append("Hypotheses:")
            for hid, hyp in hypotheses.items():
                lines.append(f"  - {hid} [{hyp['status']}]: {hyp['claim']}")

        if state.get("failures"):
            last = state["failures"][-1]
            lines.append(f"Most recent failure: [{last['taxonomy']}] {last['detail'][:150]}")

        return "\n".join(lines)

    def memory_recall(query: str, max_tokens: int = 500) -> str:
        """Search prior subgoal summaries, files touched, and failures for
        anything relevant to `query`. Use this instead of re-reading a
        file you suspect you've already looked at.

        Args:
          query: Keywords to search for, e.g. "annotation parsing utils.py".
          max_tokens: Rough budget for how much to return.
        """
        results = retrieve(run_store.run_dir, query, max_tokens=max_tokens)
        if not results:
            return "No relevant recorded evidence found for that query."
        return "\n".join(f"[{r['kind']}, ref={r['ref']}] {r['text']}" for r in results)

    def memory_expand(ref: str, offset: int = 0, length: int = None) -> str:
        """Retrieve the full, untruncated content behind a ref= you saw in
        memory_recall or the current-state summary — e.g. a full past
        read_file result that was only shown as a preview.

        Args:
          ref: Either an event_id (e.g. "evt-000012", from a ref= you saw)
            or a literal artifact_id ("sha256:...").
          offset: Character offset to start from.
          length: Max characters to return (omit for the rest of the artifact).
        """
        artifact_id = ref
        if ref.startswith("evt-"):
            record = next((r for r in read_events(run_store.run_dir) if r.get("event_id") == ref), None)
            if record is None or not record.get("artifact_id"):
                return f"ERROR: event '{ref}' not found or has no artifact."
            artifact_id = record["artifact_id"]
        try:
            return load_artifact(os.path.join(run_store.run_dir, "artifacts"), artifact_id, offset, length)
        except OSError:
            return f"ERROR: no artifact found for '{ref}'."

    return memory_status, memory_recall, memory_expand
