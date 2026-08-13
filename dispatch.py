"""Shared tool-call dispatch logic for harness.py and agent.py.

Extracted after the same bug needed identical fixes in both files: a
graduated tool returning a non-string result (list[tuple], tuple[bool,str],
etc. — legitimate return types for a plain Python caller) crashed the next
chat() call, since Ollama's Message.content must be a str. Protocol-level
concerns like "how does a tool's return value become conversation content"
belong in one place, not duplicated per orchestrator.

MAX_MESSAGE_CONTENT_CHARS caps what actually accumulates in the
conversation sent back to the model on every subsequent call — a large
web_search/fetch result kept in full, forever, is real, measured context
growth (this is what filled the model's 32768-token window and caused
Ollama to start evicting it mid-generation in one run). Console output is
unaffected — only what's replayed to the model on every future turn is
capped.
"""

MAX_MESSAGE_CONTENT_CHARS = 4000


def _format_result(result) -> str:
    """Make common structured results compact and model-readable."""
    if isinstance(result, list):
        lines = []
        for item in result[:200]:
            if isinstance(item, (tuple, list)):
                lines.append("\t".join(str(part).rstrip("\n") for part in item))
            else:
                lines.append(str(item))
        if len(result) > 200:
            lines.append(f"...[truncated {len(result) - 200} additional entries]")
        return "\n".join(lines) if lines else "(no results)"
    return str(result)


def dispatch_tool_calls(tool_calls, tool_map, allowed_names=None):
    """Execute every tool call in one model turn. Returns a list of
    {"role": "tool", ...} messages ready to append to the conversation.
    Never raises — tool errors become an ERROR/REJECTED string in content.

    allowed_names: optional set restricting which tools may actually run
    this turn, independent of what's in tool_map. Needed because removing a
    tool from the `tools=` list offered to chat() only stops the model from
    seeing its schema — it can still emit a syntactically valid call to a
    tool NAME it remembers from earlier in the same conversation's history
    (its own prior tool_calls are still visible messages), and tool_map
    itself is always the full, unrestricted registry. Confirmed live:
    agent.py's forced-edit gating reduced the offered tool list correctly
    (prompt tokens dropped as expected) but the model still called list_dir
    anyway, and it executed — offering fewer tools is not the same as
    disallowing them without this check."""
    messages = []
    for call in tool_calls:
        if allowed_names is not None and call.function.name not in allowed_names:
            result = (
                f"ERROR: '{call.function.name}' is unavailable this turn — only {sorted(allowed_names)} "
                f"are allowed right now. Use one of those instead."
            )
            print(f"🚫 blocked {call.function.name}({call.function.arguments}) — not in allowed set")
            messages.append({"role": "tool", "tool_name": call.function.name, "content": result})
            continue

        fn = tool_map.get(call.function.name)
        if fn is None:
            result = f"ERROR: unknown tool '{call.function.name}'."
        else:
            print(f"🔧 {call.function.name}({call.function.arguments})")
            try:
                result = fn(**call.function.arguments)
            except TypeError as e:
                result = f"ERROR: bad arguments for {call.function.name}: {e}"
            except ValueError as e:
                result = f"REJECTED: {e}"
            except Exception as e:
                # A graduated tool can raise anything (list_dir_tool.py raising
                # FileNotFoundError on a hallucinated path is what surfaced this in
                # practice) — the docstring above already promised "never raises,"
                # but only TypeError/ValueError were actually caught. Same fix two
                # independent self-edit generations found in evolve/'s isolated
                # experiments earlier; applying it here for real.
                result = f"ERROR: unexpected exception in {call.function.name}: {type(e).__name__}: {e}"

        if not isinstance(result, str):
            result = _format_result(result)

        print(result)

        content = result
        if len(content) > MAX_MESSAGE_CONTENT_CHARS:
            content = (
                content[:MAX_MESSAGE_CONTENT_CHARS]
                + f"\n...[truncated, {len(content) - MAX_MESSAGE_CONTENT_CHARS} more chars — "
                  f"printed in full above, but not kept in context to avoid unbounded growth]"
            )

        messages.append({"role": "tool", "tool_name": call.function.name, "content": content})
    return messages
