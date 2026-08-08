"""Phase state machine — derived, never declared.

derive_phase() is a pure function of memory/reducers.py's reduce_state()
output, exactly like reduce_state() is a pure function of the event log.
No new tool call, no model self-report: the model cannot advance the
phase by saying it has, only by producing a reducer-visible fact that
implies it. This is what makes it unlike the old prose-pattern watchdog.

orient -> reproduce -> localize -> patch -> verify -> review, the plan's
own SWE-shaped names. "finish" is deliberately not derived here — agent.py
already knows synchronously whether finish_task fired (TASK_STATE); this
module has nothing to add there.
"""

PHASES = ("orient", "reproduce", "localize", "patch", "verify", "review")


def derive_phase(state: dict) -> str:
    inspected = state.get("inspected_entities", [])
    changed = state.get("changed_entities", [])
    test_runs = state.get("test_runs", [])
    shell_runs = state.get("shell_runs", [])
    failures = state.get("failures", [])

    if not inspected and not changed and not test_runs and not shell_runs:
        return "orient"

    if not changed:
        return "reproduce" if (test_runs or shell_runs) else "localize"

    last_write_iteration = max((e.get("iteration") or 0) for e in changed)
    verification_after_write = [
        r for r in (test_runs + shell_runs) if (r.get("iteration") or 0) > last_write_iteration
    ]
    if not verification_after_write:
        return "patch"

    failures_after_write = [f for f in failures if (f.get("iteration") or 0) > last_write_iteration]
    return "verify" if failures_after_write else "review"
