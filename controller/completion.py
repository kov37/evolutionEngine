"""Mechanical completion gate for finish_task — the generic (pre-Phase-6)
implementation of AGENTIC_MEMORY_IMPLEMENTATION_PLAN.md's 8-predicate
gate. finish_task becomes a request, not authority (design principle 7):
this decides whether to actually honor a completion claim, using ONLY
reducer-visible facts — never the model's own claim that it's finished.

Three of the doc's eight predicates need SWE instance metadata that
doesn't exist until Phase 6's swe/ adapter (base-commit lineage against a
KNOWN commit, forbidden-file policy from KNOWN instance metadata, an
external SWE verifier). Generic substitutes stand in for two of them here
(git-HEAD-unchanged, an optional forbidden_paths param); the third has no
substitute. Passing every generic predicate here produces
outcome="unverified", never "resolved" — "resolved" is reserved for
Phase 6, when a real external verifier exists.
"""

import fnmatch
import subprocess

from memory.events import event_seq, read_events
from memory.reducers import reduce_state

DIFF_REVIEW_TOOLS = {"git_diff", "diff_files"}


def current_git_head(project_root: str):
    """None if project_root isn't a git repo (or git isn't available) —
    callers must treat that as 'lineage check not applicable', not as a
    failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def evaluate_completion_gate(run_dir: str, project_root: str = None, initial_git_head: str = None,
                              forbidden_paths=None) -> dict:
    """Returns {"allowed": bool, "outcome": "unverified"|"rejected", "reasons": [...]}.
    "rejected" means finish_task must NOT be honored yet — reasons explain
    what's missing, in the same ERROR/REJECTED convention as patch_file, so
    the message can be fed straight back to the model. "unverified" means
    every generic predicate passed — allowed to actually stop the loop, but
    honestly labeled: no external verifier confirmed it."""
    state = reduce_state(run_dir)
    changed = state.get("changed_entities", [])

    if not changed:
        return {"allowed": False, "outcome": "rejected",
                "reasons": ["no file has been successfully changed yet (changed_entities is empty)"]}

    last_write_seq = max(event_seq(e["changed_at_event_id"]) for e in changed)

    verification_after_write = sorted(
        (r for r in (state.get("test_runs", []) + state.get("shell_runs", []))
         if event_seq(r["event_id"]) > last_write_seq),
        key=lambda r: event_seq(r["event_id"]),
    )
    if not verification_after_write:
        return {"allowed": False, "outcome": "rejected",
                "reasons": ["no test or command was run after the most recent change — "
                            "run one before calling finish_task"]}

    # Only the MOST RECENT verification event matters, not "did anything
    # ever fail since the last write" — a live run surfaced exactly why:
    # the model tried `python` (not found), then correctly switched to
    # `python3` and passed. Blocking on the earlier, since-superseded
    # failure would make the gate reject forever after any self-corrected
    # command typo — the "bounded best-effort" paralysis trap the plan
    # doc explicitly warns against.
    latest_verification_event_id = verification_after_write[-1]["event_id"]
    latest_failure = next(
        (f for f in state.get("failures", []) if f["event_id"] == latest_verification_event_id), None,
    )
    if latest_failure is not None:
        return {"allowed": False, "outcome": "rejected",
                "reasons": [f"the most recent verification after your last change failed "
                            f"({latest_failure['taxonomy']}) — fix it before calling finish_task"]}

    # "Reviewed" means a SUCCESSFUL diff/re-read, not merely attempted — a
    # live run surfaced this too: git_diff was called against a directory
    # that isn't a git repo, failed outright, and the (unfixed) version of
    # this check still counted it as review performed. git_diff/diff_files
    # are the primary path; re-reading every changed file after its write
    # is accepted as an equivalent substitute, since diff_files needs a
    # second file to compare against (nothing to diff a fresh write
    # against) and git_diff is unusable outside a git repo — without a
    # substitute, this predicate would be impossible to satisfy for a
    # plain (non-git) project, exactly the paralysis trap the plan doc
    # warns against.
    diff_tool_succeeded = any(
        r.get("event_type") == "tool_call" and r["payload"]["tool_name"] in DIFF_REVIEW_TOOLS
        and event_seq(r["event_id"]) > last_write_seq
        and not r["payload"].get("result_preview", "").startswith(("ERROR", "REJECTED"))
        for r in read_events(run_dir)
    )
    inspected_by_path = {e["path"]: e for e in state.get("inspected_entities", [])}
    all_changes_reread = all(
        e["path"] in inspected_by_path
        and event_seq(inspected_by_path[e["path"]]["observed_at_event_id"]) > event_seq(e["changed_at_event_id"])
        for e in changed
    )
    if not (diff_tool_succeeded or all_changes_reread):
        return {"allowed": False, "outcome": "rejected",
                "reasons": ["the final diff hasn't been reviewed — call git_diff/diff_files successfully, "
                            "or read_file every file you changed, after your last change"]}

    reasons = []

    if forbidden_paths:
        violations = [e["path"] for e in changed if any(fnmatch.fnmatch(e["path"], pat) for pat in forbidden_paths)]
        if violations:
            return {"allowed": False, "outcome": "rejected",
                    "reasons": [f"forbidden path(s) changed: {', '.join(violations)}"]}
        reasons.append("forbidden-file policy: passed")
    else:
        reasons.append("forbidden-file policy: not configured (skipped)")

    if project_root and initial_git_head:
        head_now = current_git_head(project_root)
        if head_now is not None and head_now != initial_git_head:
            return {"allowed": False, "outcome": "rejected",
                    "reasons": [f"git HEAD changed since the run started ({initial_git_head[:12]} -> "
                                f"{head_now[:12]}) — checkout lineage can no longer be trusted"]}
        reasons.append("git HEAD lineage: unchanged" if head_now else "git HEAD lineage: not applicable (not a git repo)")
    else:
        reasons.append("git HEAD lineage: not checked (no initial_git_head recorded)")

    reasons.append("external SWE verifier: not available (Phase 6) — outcome is unverified, not resolved")
    return {"allowed": True, "outcome": "unverified", "reasons": reasons}
