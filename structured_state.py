"""Structured, code-maintained state — an alternative to worker.py's
free-prose summarize_context. A genuine quality improvement (stronger
grounding, less hallucination surface), NOT a latency fix — that's already
solved by agent.py's trailing-injection placement (validated separately:
prompt-eval stays flat regardless of how much a trailing block's wording
changes turn to turn).

Three of four fields are maintained ENTIRELY by code, zero LLM involvement:
FilesExplored (any path-like tool argument seen), FactsFound
(fact_extraction.py's ast/regex-extracted class/def names, accumulated and
deduplicated across the WHOLE run via set semantics), and EditsSucceeded/
EditsFailed (patch_file/write_file outcomes, classified by kernel/io_tools.py's
own return-string convention: "ERROR:"/"REJECTED" prefixes mean nothing was
written, anything else means it was) — none of these can drift, paraphrase,
or hallucinate, because no model ever generates them. Only Status ("what's
done, what's left") requires genuine judgment and stays a short, LLM-generated
line — kept deliberately small and constrained, per this project's own prior
finding (controller/plan_validation.py on main): a rich multi-field schema
mostly wasn't trustworthy; a small schema of only the fields actually proven
reliable was. Everything here that CAN be deterministic, is.

EditsSucceeded/EditsFailed exists because of a real failure mode caught live:
STATUS_PROMPT only ever sees the MOST RECENT tool result (no memory of
earlier turns, no previous-status carry-forward), so a patch_file call that
failed at iteration 27 was invisible by iteration 37 — the Status model
confidently claimed "Arcsin fix complete" while the orchestrator had made
exactly one edit attempt in 37 iterations, and it had failed. Feeding a
deterministic, never-forgotten edit ledger into every Status call closes
that gap without needing STATUS_PROMPT to carry full history.

STATUS_MODEL is qwen3.5:4b, not the heavier 9b used elsewhere (worker.py) —
running this alongside the resident 35b orchestrator on a 36GB Mac left
only ~2.4GB free with the 9b (27GB + 6.6GB resident), which live testing
confirmed drives the system into heavy swapping (9.4GB/10GB swap used,
~79MB free pages) as KV cache grows over a long run — the real cause behind
generation slowing from ~0.2s/tok to ~3s/tok over a handful of iterations,
previously misattributed to vague "Ollama daemon degradation." qwen3.5:4b
(3.4GB) leaves meaningfully more headroom for the same one-sentence
judgment call, which doesn't need a bigger model's capacity.
"""

from ollama import chat

import fact_extraction

STATUS_MODEL = "qwen3.5:4b"
# STATUS_PROMPT's real content (task summary + bounded files/facts/edits +
# a 200-char result snippet) never approaches 8192 tokens in practice — a
# live run's actual prompt sizes stayed under ~1,500 tokens. Left at 32768
# (agent.py's orchestrator value, copied without re-checking) this call
# reserves 4x the KV cache it needs on every single tool result.
STATUS_NUM_CTX = 8192
STATUS_RETRIES = 2

# STATUS_PROMPT re-embeds {task} on EVERY call even though the task text
# never changes after iteration 1 — for a real SWE-bench issue that's ~750
# tokens of dead weight resent every single tool result. Only a short
# summary is needed for a one-line "what's done/what's left" judgment, so
# StructuredState truncates once at construction (see _task_for_status)
# instead of resending the full original issue text each time.
STATUS_TASK_CHARS = 300

EDIT_TOOLS = {"patch_file", "write_file"}


def _edit_succeeded(result_content: str) -> bool:
    return not (result_content.startswith("ERROR:") or result_content.startswith("REJECTED"))


STATUS_PROMPT = """Given the facts below about an ongoing coding task, state in ONE short phrase \
(5-15 words) what is confirmed complete and what is still needed. Trust EditsSucceeded/EditsFailed \
over your own impression of progress — they are ground truth, verified by code, not a claim. Do not \
say something is "complete" or "fixed" unless it appears in EditsSucceeded. No restating the facts, \
no prose, just the judgment call.

Task: {task}
Files explored so far: {files}
Facts confirmed so far: {facts}
Edits that actually succeeded (real writes to disk): {edits_succeeded}
Edits that failed (nothing written): {edits_failed}
What just happened: {tool_name}({arguments}) -> {result_summary}

Status (one short phrase):"""


class StructuredState:
    def __init__(self, task: str):
        self.task = task
        # Truncated once, not resent-and-truncated per call — render() still
        # uses the full self.task for the orchestrator's own trailing block,
        # which legitimately needs the complete issue text.
        self._task_for_status = (
            task if len(task) <= STATUS_TASK_CHARS else task[:STATUS_TASK_CHARS] + "...(truncated)"
        )
        self.files_explored = set()
        self.facts_accumulated = set()
        self.edits_succeeded = []  # list of paths actually written, in order — duplicates kept (re-editing is real signal)
        self.edits_failed = []  # list of paths a patch/write attempt failed on
        self.status = "starting"

    def update(self, tool_name: str, arguments: dict, result_content: str) -> None:
        # --- Deterministic, zero-LLM: no drift possible on any of these three. ---
        for key in ("path", "path_a", "path_b"):
            value = arguments.get(key) if arguments else None
            if isinstance(value, str):
                self.files_explored.add(value)

        facts = fact_extraction.extract_code_facts(result_content)
        if facts:
            self.facts_accumulated.update(facts.split("; "))

        if tool_name in EDIT_TOOLS:
            path = (arguments.get("path") if arguments else None) or "(unknown path)"
            target = self.edits_succeeded if _edit_succeeded(result_content) else self.edits_failed
            target.append(path)

        # --- The one field needing genuine judgment. ---
        prompt = STATUS_PROMPT.format(
            task=self._task_for_status,
            files=", ".join(sorted(self.files_explored)) or "(none yet)",
            facts="; ".join(sorted(self.facts_accumulated)) or "(none yet)",
            edits_succeeded=", ".join(self.edits_succeeded) or "(none yet)",
            edits_failed=", ".join(self.edits_failed) or "(none yet)",
            tool_name=tool_name, arguments=arguments, result_summary=result_content[:200],
        )
        for _ in range(STATUS_RETRIES):
            try:
                response = chat(
                    model=STATUS_MODEL, messages=[{"role": "user", "content": prompt}],
                    think=False, options={"num_ctx": STATUS_NUM_CTX},
                )
                text = (response.message.content or "").strip()
                if text:
                    self.status = text
                    return
            except Exception:
                pass
        # Keep the previous status unchanged on failure — same recovery
        # pattern as worker.summarize_context's fallback.

    def render(self) -> str:
        files_block = ", ".join(sorted(self.files_explored)) or "(none yet)"
        facts_block = "; ".join(sorted(self.facts_accumulated)) or "(none yet)"
        succeeded_block = ", ".join(self.edits_succeeded) or "(none yet)"
        failed_block = ", ".join(self.edits_failed) or "(none yet)"
        return (
            "## Structured state (FilesExplored/FactsFound/EditsSucceeded/EditsFailed are maintained "
            "by code — guaranteed accurate, never model-generated; Status is the only judged field, "
            "and can be wrong if it disagrees with EditsSucceeded/EditsFailed)\n"
            f"ActiveTask: {self.task}\n"
            f"FilesExplored ({len(self.files_explored)}): {files_block}\n"
            f"FactsFound ({len(self.facts_accumulated)}): {facts_block}\n"
            f"EditsSucceeded ({len(self.edits_succeeded)}): {succeeded_block}\n"
            f"EditsFailed ({len(self.edits_failed)}): {failed_block}\n"
            f"Status: {self.status}"
        )


def _self_test() -> bool:
    state = StructuredState("Fix the bug")

    # Deterministic accumulation must work with ZERO live model calls —
    # test this in isolation from .update()'s LLM-dependent status field.
    state.files_explored.add("crv_types.py")
    state.facts_accumulated.update(["class ArcsinDistribution(SingleContinuousDistribution)", "def pdf"])
    state.files_explored.add("crv.py")
    state.facts_accumulated.update(["class DagumDistribution(SingleContinuousDistribution)"])

    rendered = state.render()
    assert "ActiveTask: Fix the bug" in rendered
    assert "crv.py" in rendered and "crv_types.py" in rendered
    assert "ArcsinDistribution" in rendered and "DagumDistribution" in rendered
    assert "FilesExplored (2)" in rendered
    assert "FactsFound (3)" in rendered

    # Alphabetical, deterministic ordering — same input, same output, always.
    assert rendered == state.render()

    # Deduplication via set semantics — adding the same fact twice must not
    # double-count.
    state.facts_accumulated.add("def pdf")
    assert "FactsFound (3)" in state.render(), "duplicate fact must not inflate the count"

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   structured_state.StructuredState (deterministic fields only)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
