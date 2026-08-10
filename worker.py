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

Runs locally (this Mac), confirmed live: both models fit resident
simultaneously (~21GB + ~5.7GB, well under the 36GB ceiling), with no
meaningful compute contention once Ollama's own daemon is healthy.
Briefly moved to legion.local (a separate physical machine, RTX 4070) to
free local headroom during a long run where the orchestrator's context
had grown to 30GB+ — reverted after finding the network round-trip made a
single compression call take 5+ minutes in practice (vs. ~1-2s local),
which made the whole pipeline impractically slow. Local latency is the
worse tradeoff to accept than that.

Never blocks or crashes the orchestrator: any worker failure (timeout,
malformed response, model not loaded) falls back to sidecar.py's
mechanical summary, so a worker hiccup degrades gracefully to Arm A's
original behavior instead of stalling the run.
"""

from ollama import chat

import fact_extraction
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

# Different shape from COMPRESSION_PROMPT: that one compresses ONE call in
# isolation, appended to a growing list (sidecar.render_sidecar). This one
# re-synthesizes a SINGLE holistic summary each time — given the previous
# summary plus what just happened, produce an updated summary that REPLACES
# it, not a new entry alongside it. Lets the worker deduplicate/reorganize
# across calls (e.g. collapse five separate "explored crv_types.py" notes
# into one), which a pure append-only list can never do.
CONTEXT_SUMMARY_PROMPT = """You are maintaining a running summary of an ongoing coding task, \
updated after each tool call. Given the previous summary and what just happened, produce an \
UPDATED summary that replaces it entirely.

Be concise — a short dense paragraph, not a list. Keep facts that are still relevant (files/ \
classes/functions found, what's done, what's still needed); drop or merge anything superseded \
or no longer useful. Do not just append the new information — actually integrate it.
{grounding_block}
Previous summary:
{previous_summary}

What just happened (tool: {tool_name}, args: {arguments}):
{result_content}

Updated summary:"""

# Appended into CONTEXT_SUMMARY_PROMPT only when fact_extraction found real,
# verified names in this call's content — grounds the model's paraphrase
# against confirmed facts instead of letting it free-recall class/function
# names from memory, the exact failure mode behind two unverified
# "discrepancy" claims the worker made live in earlier testing (a PDF
# normalization-coefficient claim, a docstring-mismatch claim — neither
# ever independently checked against the real source).
GROUNDING_BLOCK_TEMPLATE = """
VERIFIED facts in this result (extracted mechanically, not by you — these names are guaranteed \
real, use them exactly as given rather than recalling similar-sounding names from memory): \
{facts}
"""


def _call_worker(prompt: str):
    """Shared retry loop for a single worker prompt. Returns the stripped
    response text, or None (with the last error printed to stderr) if every
    attempt failed — callers decide their own fallback, since "keep the old
    summary" and "fall back to a mechanical per-call note" are different
    recoveries for different callers."""
    last_error = None
    for attempt in range(1, WORKER_TIMEOUT_RETRIES + 1):
        try:
            response = chat(
                model=WORKER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                think=False,
                options={"num_ctx": WORKER_NUM_CTX},
            )
            text = (response.message.content or "").strip()
            if text:
                return text, None
            last_error = "worker returned empty content"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
    return None, last_error


def compress_tool_result(tool_name: str, arguments: dict, result_content: str) -> str:
    """Ask the worker model for a one-sentence semantic summary of a tool
    call's result. Falls back to sidecar.summarize_call's mechanical
    summary on any worker failure."""
    prompt = COMPRESSION_PROMPT.format(
        tool_name=tool_name, arguments=arguments, result_content=result_content
    )
    summary, error = _call_worker(prompt)
    if summary is not None:
        return summary

    fallback = sidecar.summarize_call(tool_name, arguments, result_content)
    return f"{fallback} [worker compression failed: {error}]"


def summarize_context(previous_summary: str, tool_name: str, arguments: dict, result_content: str) -> str:
    """Ask the worker model to produce an UPDATED holistic summary given the
    previous one plus what just happened — replaces the whole thing, rather
    than compress_tool_result's one-entry-per-call approach. On worker
    failure, keeps the previous summary unchanged (the only sensible
    fallback here — there's no mechanical equivalent for "re-synthesize
    everything," unlike compress_tool_result's per-call fallback).

    Grounds the re-synthesis against fact_extraction's mechanically-verified
    class/function names when the result looks like code, instead of
    letting the model paraphrase from memory alone — see
    GROUNDING_BLOCK_TEMPLATE for why."""
    facts = fact_extraction.extract_code_facts(result_content)
    if facts:
        print(f"📌 [grounding] {facts}")
    grounding_block = GROUNDING_BLOCK_TEMPLATE.format(facts=facts) if facts else ""
    prompt = CONTEXT_SUMMARY_PROMPT.format(
        previous_summary=previous_summary or "(nothing yet — this is the first update)",
        tool_name=tool_name, arguments=arguments, result_content=result_content,
        grounding_block=grounding_block,
    )
    summary, error = _call_worker(prompt)
    if summary is not None:
        return summary
    return previous_summary + f"\n[note: summary update failed this turn — {error}]"


def _self_test() -> bool:
    # No live model call here — this only checks prompt construction and the
    # fallback path shape, kept fast and dependency-free for CI-style runs.
    # Live worker behavior is verified separately, against the real model.
    prompt = COMPRESSION_PROMPT.format(
        tool_name="read_file", arguments={"path": "a.py"}, result_content="def f(): pass"
    )
    assert "read_file" in prompt
    assert "def f(): pass" in prompt

    ctx_prompt_first = CONTEXT_SUMMARY_PROMPT.format(
        previous_summary="(nothing yet — this is the first update)",
        tool_name="read_file", arguments={"path": "a.py"}, result_content="def f(): pass",
        grounding_block="",
    )
    assert "nothing yet" in ctx_prompt_first
    assert "def f(): pass" in ctx_prompt_first

    ctx_prompt_update = CONTEXT_SUMMARY_PROMPT.format(
        previous_summary="Explored a.py, found function f().",
        tool_name="read_file", arguments={"path": "b.py"}, result_content="def g(): pass",
        grounding_block="",
    )
    assert "Explored a.py, found function f()." in ctx_prompt_update
    assert "def g(): pass" in ctx_prompt_update

    # Grounding block: real facts must actually appear in the built prompt,
    # not just be extracted and discarded.
    real_facts = fact_extraction.extract_code_facts("class DagumDistribution(SingleContinuousDistribution):\n    def pdf(self, x):\n        pass\n")
    grounded_prompt = CONTEXT_SUMMARY_PROMPT.format(
        previous_summary="", tool_name="read_file", arguments={"path": "c.py"},
        result_content="class DagumDistribution(SingleContinuousDistribution):\n    def pdf(self, x):\n        pass\n",
        grounding_block=GROUNDING_BLOCK_TEMPLATE.format(facts=real_facts),
    )
    assert "VERIFIED facts" in grounded_prompt
    assert "DagumDistribution" in grounded_prompt

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   worker.compress_tool_result/summarize_context (prompt construction only)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
