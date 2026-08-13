"""Kernel tier: control-flow signaling for agent.py.

Separate from kernel/io_tools.py's RUN_STATE on purpose. RUN_STATE is what
harness.py's curriculum loop checks (did the file run clean); TASK_STATE is
what agent.py's loop checks (did the model declare the task done). They're
different orchestrators with different stop conditions and shouldn't share
a flag — harness.py doesn't check this, and never gets it as a tool.
"""

TASK_STATE = {"done": False, "requested": False, "summary": None}


def finish_task(summary: str) -> str:
    """Call this when — and only when — the task is fully complete. This is
    the sole way the agent loop knows to stop; returning plain text without
    calling this is treated as still in progress.

    Args:
      summary: A short account of what was done and the end result.
    """
    TASK_STATE["requested"] = True
    TASK_STATE["summary"] = summary
    return "Completion requested; the agent will verify the work before stopping."


def approve_task() -> None:
    """Internal orchestrator hook; never exposed as a model tool."""
    TASK_STATE["done"] = True
