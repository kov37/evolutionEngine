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


def dispatch_tool_calls(tool_calls, tool_map, recorder=None):
    """Execute every tool call in one model turn. Returns a list of
    {"role": "tool", ...} messages ready to append to the conversation.
    Never raises — tool errors become an ERROR/REJECTED string in content.

    recorder, if given, is called as recorder(tool_name, arguments, full_result)
    once per call, with the FULL untruncated result — before the truncation
    below ever happens — and its return value is used, if it looks like an
    event record (has an "event_id"), to tell the model exactly how to get
    the truncated content back (memory_expand(ref=...)) instead of leaving
    it to remember a generic prompt mention. A live run surfaced this isn't
    hypothetical: the model correctly noticed a file kept getting truncated
    and got stuck repeatedly re-fetching it via run_shell instead of using
    memory_expand, which exists for exactly this. Optional and defaults to
    None so harness.py's call site (which has no run store, and no
    memory_expand tool) is completely unaffected; only agent.py passes one.
    See memory/store.py's RunStore.record_tool_call."""
    messages = []
    for call in tool_calls:
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
            result = str(result)

        print(result)

        recorded_event = None
        if recorder is not None:
            recorded_event = recorder(call.function.name, call.function.arguments, result)

        content = result
        if len(content) > MAX_MESSAGE_CONTENT_CHARS:
            expand_hint = ""
            if isinstance(recorded_event, dict) and recorded_event.get("event_id"):
                expand_hint = f" Use memory_expand(ref='{recorded_event['event_id']}') to get the rest if you need it."
            content = (
                content[:MAX_MESSAGE_CONTENT_CHARS]
                + f"\n...[truncated, {len(content) - MAX_MESSAGE_CONTENT_CHARS} more chars — "
                  f"printed in full above, but not kept in context to avoid unbounded growth.{expand_hint}]"
            )

        messages.append({"role": "tool", "tool_name": call.function.name, "content": content})
    return messages
