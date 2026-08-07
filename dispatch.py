"""Shared tool-call dispatch logic for harness.py and agent.py.

Extracted after the same bug needed identical fixes in both files: a
graduated tool returning a non-string result (list[tuple], tuple[bool,str],
etc. — legitimate return types for a plain Python caller) crashed the next
chat() call, since Ollama's Message.content must be a str. Protocol-level
concerns like "how does a tool's return value become conversation content"
belong in one place, not duplicated per orchestrator.
"""


def dispatch_tool_calls(tool_calls, tool_map):
    """Execute every tool call in one model turn. Returns a list of
    {"role": "tool", ...} messages ready to append to the conversation.
    Never raises — tool errors become an ERROR/REJECTED string in content."""
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

        if not isinstance(result, str):
            result = str(result)

        print(result)
        messages.append({"role": "tool", "tool_name": call.function.name, "content": result})
    return messages
