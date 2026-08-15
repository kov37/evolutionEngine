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


def _normalize_tool_arguments(tool_name, arguments):
    """Repair common JSON-schema shape drift before dispatching a tool.

    Small local models sometimes emit an argv list as a JSON-encoded string,
    call it ``argv``, or express milliseconds as ``timeout_ms`` even though
    the trusted tool schema uses ``command`` and seconds. Normalize only these
    unambiguous aliases; never reinterpret a command's content.
    """
    import json
    import re
    import shlex

    normalized = dict(arguments or {})
    if tool_name in {"run_command", "run_shell"}:
        if "command" not in normalized and "argv" in normalized:
            normalized["command"] = normalized.pop("argv")
        command = normalized.get("command")
        if isinstance(command, str):
            try:
                decoded = json.loads(command)
            except (TypeError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, list):
                normalized["command"] = decoded
            elif tool_name == "run_command":
                normalized["command"] = shlex.split(command)
        if "timeout" not in normalized and "timeout_ms" in normalized:
            try:
                normalized["timeout"] = max(1, (int(normalized.pop("timeout_ms")) + 999) // 1000)
            except (TypeError, ValueError):
                normalized.pop("timeout_ms", None)
        elif "timeout" in normalized and isinstance(normalized["timeout"], str):
            # Salvage a numeric prefix when a small model leaks punctuation or
            # a fragment of the surrounding call into a scalar argument.
            match = re.match(r"\s*(\d+(?:\.\d+)?)", normalized["timeout"])
            if match:
                normalized["timeout"] = max(1, int(float(match.group(1))))
        cwd = normalized.get("cwd")
        if isinstance(cwd, str):
            stripped = cwd.strip()
            if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
                stripped = stripped[1:-1]
            if stripped in {"", "."}:
                normalized.pop("cwd", None)
            else:
                normalized["cwd"] = stripped
    return normalized


def _call_key(tool_name, arguments):
    import json
    return tool_name, json.dumps(arguments or {}, sort_keys=True, default=str, separators=(",", ":"))


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


def dispatch_tool_calls(tool_calls, tool_map, allowed_names=None, blocked_calls=None,
                        blocked_mutation_paths=None):
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
        call_arguments = call.function.arguments or {}
        if (blocked_mutation_paths
                and call.function.name in {"write_file", "patch_file"}
                and call_arguments.get("path") in blocked_mutation_paths):
            result = (
                f"REJECTED: mutation path '{call_arguments.get('path')}' is blocked after a protected-test "
                "edit was rejected. Repair the implementation, then run the validation command."
            )
            print(f"🚫 blocked mutation path {call.function.name}({call_arguments})")
            messages.append({"role": "tool", "tool_name": call.function.name, "content": result})
            continue
        if blocked_calls and _call_key(call.function.name, call.function.arguments) in blocked_calls:
            result = (
                f"REJECTED: repeated failing call {call.function.name} with the same arguments. "
                "Do not retry it. Change strategy: inspect the workspace, use the correct tool schema, "
                "or make a concrete edit based on the evidence already collected."
            )
            print(f"🚫 blocked repeated failure {call.function.name}({call.function.arguments})")
            messages.append({"role": "tool", "tool_name": call.function.name, "content": result})
            continue
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
                normalized_arguments = _normalize_tool_arguments(call.function.name, call.function.arguments)
                result = fn(**normalized_arguments)
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
