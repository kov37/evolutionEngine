"""Automated write-only sidecar: a system-maintained running log of tool-call
activity, pinned to the top of the system prompt every turn.

Arm A of the context-management experiment (see REFACTORING_LEARNINGS.md
findings #22-26, the sympy-13878 baseline where ~40 of the first 65
iterations were redundant re-reads of the same file). Deliberately NOT a
tool — the model never calls anything to produce or read this. It's built
automatically from what dispatch_tool_calls already returns, then spliced
into messages[0]'s content before every chat() call. This avoids the
executive-function overhead a scratchpad TOOL would impose (deciding when to
write, what to write, getting the tool-call syntax right) — overhead this
model has already shown it struggles with (hallucinated kwargs, malformed
flags). It also guarantees delivery: since the harness does the injection,
the model can't forget to read it or skip an update, and it sits at the very
start of the prompt — the position transformers attend to most reliably,
rather than buried in the middle of a long tool-output history.

Kept deliberately dumb for this first pass: a chronological log of compact
one-line call summaries, not deduplicated or range-merged (that's the
state-aware-tooling arm, tested separately later). This only adds a passive
breadcrumb trail; it does not prune or alter the existing raw message
history at all, so a run with this enabled is a clean, single-variable A/B
against the already-recorded baseline.
"""

MAX_ARG_REPR_CHARS = 60

# Matches dispatch.py's MAX_MESSAGE_CONTENT_CHARS convention. Without a cap,
# sidecar_log grows linearly with total tool calls for the life of the run —
# over a long run this becomes exactly the kind of unbounded context growth
# the sidecar exists to prevent, and would confound any A/B comparison
# against a no-sidecar baseline (a "sidecar hurt" result could just mean
# "the log itself got huge," not "summaries don't help").
MAX_SIDECAR_CHARS = 4000


def _arg_repr(value) -> str:
    """Compact representation of one tool-call argument. Long values
    (patch_file's search/replace blocks, write_file's content) would
    dominate/bloat the log if shown in full, so anything over the threshold
    collapses to a length marker instead."""
    s = repr(value)
    if len(s) <= MAX_ARG_REPR_CHARS:
        return s
    return f"<{len(str(value))} chars omitted>"


def summarize_call(tool_name: str, arguments: dict, result_content: str) -> str:
    """One compact line describing a single tool call and its outcome."""
    arg_str = ", ".join(f"{k}={_arg_repr(v)}" for k, v in (arguments or {}).items())
    first_line = (result_content or "").split("\n", 1)[0]
    if first_line.startswith("ERROR") or first_line.startswith("REJECTED"):
        outcome = first_line[:100]
    else:
        outcome = f"{len(result_content)} chars returned"
    return f"{tool_name}({arg_str}) -> {outcome}"


def render_sidecar(log_lines: list) -> str:
    """Render the full sidecar section to append to the system prompt. An
    empty log renders as "" (no section header at all) so a run with no
    tool calls yet doesn't show an empty, confusing block.

    Bounded to MAX_SIDECAR_CHARS: keeps the most recent entries that fit,
    dropping the oldest first. Call numbers are preserved from the original
    (untruncated) log even when early entries are dropped, so "[47]" always
    means "the 47th tool call this run" — not "the 1st line still visible."
    """
    if not log_lines:
        return ""

    numbered = list(enumerate(log_lines, start=1))
    kept = []
    total_chars = 0
    omitted_count = 0
    for i, line in reversed(numbered):
        entry = f"[{i}] {line}"
        if total_chars + len(entry) + 1 > MAX_SIDECAR_CHARS:
            omitted_count = i
            break
        kept.append(entry)
        total_chars += len(entry) + 1
    kept.reverse()

    header = (
        "\n\n## Automated activity log (system-maintained — a record of what "
        "you've already called, not something you wrote)"
    )
    if omitted_count:
        header += f"\n[...{omitted_count} earlier call(s) omitted to keep this section bounded...]"
    return header + "\n" + "\n".join(kept)


def _self_test() -> bool:
    assert render_sidecar([]) == "", "empty log must render to nothing"

    line = summarize_call(
        "read_file", {"path": "a.py", "offset": 1, "limit": 50}, "some content here"
    )
    assert line == "read_file(path='a.py', offset=1, limit=50) -> 17 chars returned", line

    long_value = "x" * 200
    line2 = summarize_call("patch_file", {"path": "a.py", "search": long_value}, "Wrote 'a.py'")
    assert "<200 chars omitted>" in line2, line2
    assert long_value not in line2, "long arg values must not appear verbatim in the sidecar"

    err_line = summarize_call("grep_dir", {"path": "x"}, "ERROR: 'x' does not exist.\nmore text")
    assert err_line.endswith("ERROR: 'x' does not exist."), err_line

    rendered = render_sidecar([line, err_line])
    assert rendered.startswith("\n\n## Automated activity log"), rendered
    assert "[1] " + line in rendered
    assert "[2] " + err_line in rendered

    # Unbounded growth: a huge log must truncate to the most recent entries,
    # stay under the cap, and preserve original call numbering.
    huge_log = [f"read_file(path='f{n}.py') -> {n} chars returned" for n in range(1, 501)]
    huge_rendered = render_sidecar(huge_log)
    assert len(huge_rendered) < MAX_SIDECAR_CHARS + 500, (
        f"rendered sidecar must stay roughly bounded, got {len(huge_rendered)} chars"
    )
    assert "[500] " in huge_rendered, "most recent entry must survive truncation"
    assert "[1] " not in huge_rendered, "oldest entry must be dropped once over budget"
    assert "earlier call(s) omitted" in huge_rendered, "must note that truncation happened"

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   sidecar.summarize_call/render_sidecar" if ok else "FAILED")
    sys.exit(0 if ok else 1)
