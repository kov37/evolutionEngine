"""Structured, code-maintained state — an alternative to worker.py's
free-prose summarize_context. A genuine quality improvement (stronger
grounding, less hallucination surface), NOT a latency fix — that's already
solved by agent.py's trailing-injection placement (validated separately:
prompt-eval stays flat regardless of how much a trailing block's wording
changes turn to turn).

Two of three fields are maintained ENTIRELY by code, zero LLM involvement:
FilesExplored (any path-like tool argument seen) and FactsFound
(fact_extraction.py's ast/regex-extracted class/def names, accumulated and
deduplicated across the WHOLE run via set semantics) — neither can drift,
paraphrase, or hallucinate, because no model ever generates them. Only
Status ("what's done, what's left") requires genuine judgment and stays a
short, LLM-generated line — kept deliberately small and constrained, per
this project's own prior finding (controller/plan_validation.py on main):
a rich multi-field schema mostly wasn't trustworthy; a small schema of only
the fields actually proven reliable was. Everything here that CAN be
deterministic, is.
"""

from ollama import chat

import fact_extraction

STATUS_MODEL = "qwen3.5:9b"
STATUS_NUM_CTX = 32768
STATUS_RETRIES = 2

STATUS_PROMPT = """Given the facts below about an ongoing coding task, state in ONE short phrase \
(5-15 words) what is confirmed complete and what is still needed. No restating the facts, no prose, \
just the judgment call.

Task: {task}
Files explored so far: {files}
Facts confirmed so far: {facts}
What just happened: {tool_name}({arguments}) -> {result_summary}

Status (one short phrase):"""


class StructuredState:
    def __init__(self, task: str):
        self.task = task
        self.files_explored = set()
        self.facts_accumulated = set()
        self.status = "starting"

    def update(self, tool_name: str, arguments: dict, result_content: str) -> None:
        # --- Deterministic, zero-LLM: no drift possible on these two. ---
        for key in ("path", "path_a", "path_b"):
            value = arguments.get(key) if arguments else None
            if isinstance(value, str):
                self.files_explored.add(value)

        facts = fact_extraction.extract_code_facts(result_content)
        if facts:
            self.facts_accumulated.update(facts.split("; "))

        # --- The one field needing genuine judgment. ---
        prompt = STATUS_PROMPT.format(
            task=self.task,
            files=", ".join(sorted(self.files_explored)) or "(none yet)",
            facts="; ".join(sorted(self.facts_accumulated)) or "(none yet)",
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
        return (
            "## Structured state (FilesExplored/FactsFound are maintained by code — "
            "guaranteed accurate, never model-generated; Status is the only judged field)\n"
            f"ActiveTask: {self.task}\n"
            f"FilesExplored ({len(self.files_explored)}): {files_block}\n"
            f"FactsFound ({len(self.facts_accumulated)}): {facts_block}\n"
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
