"""LLM-generated episode summaries, with a fidelity check against the raw
event range they claim to summarize — deferred from Phase 3
(context/policies.py's flat-summary/hierarchy policies were explicitly
NOT built there) until subgoals (Phase 4) existed to define real
boundaries, rather than inventing arbitrary ones.

The summary is a MODEL ASSERTION, not a reducer-visible fact
(AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md's "Enforceable evidence versus
semantic claims") — useful for compact retrieval and context, never
treated as ground truth. check_fidelity() is a best-effort heuristic
cross-check (do the paths the summary mentions actually appear in the
real event range it's summarizing), not a guarantee of correctness.
"""

import re

from ollama import chat

from memory.events import event_seq, read_events

MAX_RAW_CHARS_FOR_SUMMARY = 6000  # keeps the summarization call itself bounded


def _gather_raw_text(run_dir: str, from_event_id: str, to_event_id: str) -> str:
    """Renders tool_call/model_call events between from_event_id and
    to_event_id (inclusive) as plain text — previews only, not full
    artifacts, so a subgoal spanning several large reads doesn't blow the
    summarization call's own context."""
    from_seq, to_seq = event_seq(from_event_id), event_seq(to_event_id)
    lines = []
    for record in read_events(run_dir):
        if record.get("event_type") == "corrupt_event":
            continue
        seq = event_seq(record["event_id"])
        if seq < from_seq or seq > to_seq:
            continue
        if record["event_type"] == "tool_call":
            p = record["payload"]
            lines.append(f"[{record['event_id']}] {p['tool_name']}({p.get('arguments')}) "
                         f"-> {p.get('result_preview', '')[:300]}")
        elif record["event_type"] == "model_call":
            preview = record["payload"].get("response_preview", "")
            if preview:
                lines.append(f"[{record['event_id']}] (reasoning) {preview[:300]}")
    return "\n".join(lines)[:MAX_RAW_CHARS_FOR_SUMMARY]


def generate_summary(run_dir: str, from_event_id: str, to_event_id: str, goal: str, conclusion: str, model: str):
    """Returns (summary_text, raw_text_used). A single, non-tool-calling
    chat() call — runs outside the agent's own tool-calling loop, so it
    can't itself call subgoal_create/etc."""
    raw_text = _gather_raw_text(run_dir, from_event_id, to_event_id)
    prompt = (
        f"Summarize what actually happened while working on this subgoal, in 2-4 sentences. "
        f"Be concrete: name the specific files/functions touched and what was confirmed, not just intent.\n\n"
        f"Subgoal: {goal}\n"
        f"Claimed conclusion: {conclusion}\n\n"
        f"Raw trace:\n{raw_text}\n\n"
        f"Summary:"
    )
    response = chat(model=model, messages=[{"role": "user", "content": prompt}], think=False)
    return (response.message.content or "").strip(), raw_text


_PATHLIKE_RE = re.compile(r"\b[\w\-/]+\.\w+\b")


def check_fidelity(summary_text: str, raw_text: str) -> bool:
    """Heuristic, not a guarantee: every file-path-shaped token mentioned
    in the summary must also appear in the raw trace it was generated
    from. Catches a summary that hallucinates a path never actually
    touched; does not verify the summary is otherwise accurate."""
    mentioned_paths = set(_PATHLIKE_RE.findall(summary_text))
    if not mentioned_paths:
        return True  # nothing checkable mentioned — not a failure, just no claim to verify
    return all(path in raw_text for path in mentioned_paths)
