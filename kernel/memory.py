"""Kernel tier: a plain archive of full tool-result text, keyed by entry
number. Lets context_summary_enabled mode (agent.py) safely prune old raw
tool results from the live conversation without permanently losing exact
detail — worker.summarize_context() gives the orchestrator the gist; recall()
gets it the verbatim original when the gist isn't precise enough (e.g.
before a patch_file call that needs exact text to match).

Deliberately NOT semantic/vector search — a plain dict lookup by entry
number. No LLM call involved, so it's nearly free, and no ambiguity about
what gets returned.
"""

ARCHIVE: dict = {}


def reset_archive() -> None:
    ARCHIVE.clear()


def record(entry_number: int, content: str) -> None:
    ARCHIVE[entry_number] = content


def recall(entry_number: int) -> str:
    """Retrieve the full, verbatim original tool output for a given entry
    number — use this when the running summary's paraphrase isn't precise
    enough (e.g. right before a patch_file call that needs exact text to
    match). The running summary states the current valid entry range.

    Args:
      entry_number: The entry number to retrieve.
    """
    if entry_number not in ARCHIVE:
        valid_range = f"1-{len(ARCHIVE)}" if ARCHIVE else "(none yet)"
        return f"ERROR: no archived entry #{entry_number}. Valid range: {valid_range}."
    return ARCHIVE[entry_number]


def _self_test() -> bool:
    reset_archive()
    assert recall(1).startswith("ERROR")

    record(1, "full raw content of call 1")
    record(2, "full raw content of call 2")
    assert recall(1) == "full raw content of call 1"
    assert recall(2) == "full raw content of call 2"

    missing = recall(5)
    assert missing.startswith("ERROR") and "1-2" in missing, missing

    reset_archive()
    assert len(ARCHIVE) == 0

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   kernel.memory.record/recall" if ok else "FAILED")
    sys.exit(0 if ok else 1)
