"""Render memory/reducers.py's reduce_state() output into a stable,
compact text block for the model-facing prompt. Kept separate from
policies.py so wording/format can change without touching selection or
budget logic.

Deliberately excludes redundant raw tool output (plan requirement) — a
file that was read shows up as a path + turn + event_id reference the
model can act on (re-read it, or later call a memory_expand tool once
Phase 5 exists), never its full previously-seen content inline. Stale
entities (memory/reducers.py's stale-evidence invalidation) are flagged
rather than dropped, and sorted after fresh ones when trimming to a
limit — the plan's "penalize stale evidence" instruction, applied.
"""


def render_structured_state(state: dict, max_entities: int = 8, max_failures: int = 6, episodes=None) -> str:
    lines = ["## Current state (derived automatically from tool calls so far — not model self-report)"]

    if episodes:
        lines.append("\nCompleted subgoals:")
        for ep in episodes:
            tags = []
            if not ep.get("fidelity_ok", True):
                tags.append("fidelity check failed — summary may be unreliable")
            if ep.get("auto_closed"):
                tags.append("auto-closed by runtime, not explicitly confirmed by you")
            tag_text = f" [{'; '.join(tags)}]" if tags else ""
            lines.append(f"  - {ep['subgoal_id']}: {ep['summary']}{tag_text}")

    by_path = {}
    for e in state.get("inspected_entities", []):
        by_path[e["path"]] = e
    for e in state.get("changed_entities", []):
        by_path[e["path"]] = {**e, "changed": True}

    if by_path:
        ordered = sorted(by_path.values(), key=lambda e: (e.get("stale", False), -(e.get("iteration") or 0)))
        lines.append("\nFiles touched:")
        for e in ordered[:max_entities]:
            status = "CHANGED" if e.get("changed") else "read"
            stale_tag = " [STALE — re-read before trusting]" if e.get("stale") else ""
            event_ref = e.get("changed_at_event_id") or e.get("observed_at_event_id")
            lines.append(f"  - {e['path']} ({status}, turn {e.get('iteration')}, ref={event_ref}){stale_tag}")

    if state.get("test_runs"):
        latest = sorted(state["test_runs"], key=lambda t: -(t.get("iteration") or 0))[:3]
        lines.append("\nRecent test runs:")
        for t in latest:
            parsed = t.get("parsed")
            summary = f"{parsed['passed']}/{parsed['total']} passed" if parsed else "unparsed result"
            lines.append(f"  - turn {t.get('iteration')}: {summary} (ref={t['event_id']})")

    if state.get("failures"):
        recent = sorted(state["failures"], key=lambda f: -(f.get("iteration") or 0))[:max_failures]
        lines.append("\nRecent failures:")
        for f in recent:
            target = f.get("path") or f.get("command") or ""
            lines.append(f"  - [{f['taxonomy']}] turn {f.get('iteration')}: {target} — "
                          f"{f['detail'][:150]} (ref={f['event_id']})")

    if len(lines) == 1:
        return "## Current state\n(nothing recorded yet)"
    return "\n".join(lines)
