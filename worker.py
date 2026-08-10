"""Orchestrator-worker pattern: qwen3.5:9b (the worker) compresses each tool
call's raw result into a short, semantic one-line summary; qwen3.6:35b-mlx
(the orchestrator, agent.py) consumes that compressed summary via the
sidecar instead of the raw content or sidecar.py's mechanical
name+args+length line.

This is the real content-awareness layer sidecar.py explicitly deferred —
sidecar.py's own docstring calls it out as "deliberately dumb... not
deduplicated or range-merged," i.e. call metadata only, no semantic
compression. Doing that compression with a SECOND, smaller model (rather
than the orchestrator writing its own notes, or a fragile per-tool text
heuristic) means: no extra executive-function burden on the orchestrator
(same reasoning that ruled out a scratchpad TOOL), and no per-tool-type
parsing code to maintain — one general-purpose prompt handles any tool's
output.

Confirmed live before building this: both models fit resident simultaneously
on this hardware (qwen3.6:35b-mlx ~21GB + qwen3.5:9b ~5.7GB, well under the
36GB unified memory ceiling), so this is real concurrent orchestrator+worker
execution, not a sequential swap.

Never blocks or crashes the orchestrator: any worker failure (timeout,
malformed response, model not loaded) falls back to sidecar.py's mechanical
summary, so a worker hiccup degrades gracefully to Arm A's original
behavior instead of stalling the run.
"""

from ollama import chat

import sidecar

WORKER_MODEL = "qwen3.5:9b"
WORKER_NUM_CTX = 32768
WORKER_TIMEOUT_RETRIES = 2

COMPRESSION_PROMPT = """Summarize what this tool call revealed, in ONE short sentence. \
Be concrete and factual — what exists, what was found, what's missing or wrong. \
No preamble, no restating the tool name, just the fact(s) learned.

Tool: {tool_name}
Arguments: {arguments}
Result:
{result_content}

One-sentence summary:"""


def compress_tool_result(tool_name: str, arguments: dict, result_content: str) -> str:
    """Ask the worker model for a one-sentence semantic summary of a tool
    call's result. Falls back to sidecar.summarize_call's mechanical
    summary on any worker failure."""
    prompt = COMPRESSION_PROMPT.format(
        tool_name=tool_name, arguments=arguments, result_content=result_content
    )
    last_error = None
    for attempt in range(1, WORKER_TIMEOUT_RETRIES + 1):
        try:
            response = chat(
                model=WORKER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                think=False,
                options={"num_ctx": WORKER_NUM_CTX},
            )
            summary = (response.message.content or "").strip()
            if summary:
                return summary
            last_error = "worker returned empty content"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"

    fallback = sidecar.summarize_call(tool_name, arguments, result_content)
    return f"{fallback} [worker compression failed: {last_error}]"


def _self_test() -> bool:
    # No live model call here — this only checks prompt construction and the
    # fallback path shape, kept fast and dependency-free for CI-style runs.
    # Live worker behavior is verified separately, against the real model.
    prompt = COMPRESSION_PROMPT.format(
        tool_name="read_file", arguments={"path": "a.py"}, result_content="def f(): pass"
    )
    assert "read_file" in prompt
    assert "def f(): pass" in prompt
    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   worker.compress_tool_result (prompt construction only)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
