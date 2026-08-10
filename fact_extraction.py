"""Deterministic fact extraction, grounding worker.summarize_context's
LLM-based re-synthesis against real, verified names instead of letting it
free-recall them from memory.

Directly reuses a proven pattern from this project's own history
(controller/extraction.py + plan_validation.py on main, "Isolation Planning
Evaluation"): ungrounded LLM extraction hallucinated real file/symbol names
in 0 of 6 real test attempts; injecting a REAL, VERIFIED listing into the
prompt instead of asking the model to recall from scratch fixed it
completely (6 of 6). That evaluation also found temperature=0 does NOT make
this local model reproduce identical output on repeat calls — so
"deterministic" here means grounded-and-verified, not just a sampling
setting; the LLM's job is prose around confirmed facts, not fact recall.

For code content specifically, `ast` gives real class/function names with
zero LLM involvement and zero hallucination risk by construction. Falls
back to a regex line-scan when the content isn't valid standalone Python —
the common case for read_file's windowed output, which is usually a
fragment (starts mid-class, mid-function) that ast.parse() will reject with
a SyntaxError even though it's real code.
"""

import ast
import re

CLASS_LINE_RE = re.compile(r"^\s*class\s+(\w+)")
DEF_LINE_RE = re.compile(r"^\s*def\s+(\w+)")
MAX_FACTS = 20  # keep the grounding block short — a long fact list defeats compression


def extract_code_facts(content: str) -> str:
    """Return a compact, deterministic string of class/function names found
    in `content`, or "" if none are found (e.g. non-code content — the
    caller should skip the grounding block entirely in that case, not
    claim "no code found" as if that were itself a verified fact).
    """
    facts = _extract_via_ast(content)
    if facts is None:
        facts = _extract_via_regex(content)
    if not facts:
        return ""
    facts = facts[:MAX_FACTS]
    return "; ".join(facts)


def _extract_via_ast(content: str):
    """Full AST parse — most reliable when it works, but read_file's
    windowed output is usually a syntactically incomplete fragment (starts
    mid-class/mid-function), so this fails often and callers must fall
    back, not treat a SyntaxError as "no facts"."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    facts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(_base_name(b) for b in node.bases)
            facts.append(f"class {node.name}({bases})" if bases else f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            facts.append(f"def {node.name}")
    return facts


def _base_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return "?"


def _extract_via_regex(content: str) -> list:
    """Line-by-line pattern match — works on incomplete fragments, since it
    doesn't require the content to parse as a standalone module. Less
    rigorous than AST (no base-class info, could theoretically false-match
    inside a string, though rare for real source), but robust to the
    dominant real case: a windowed excerpt of a much larger file."""
    facts = []
    for line in content.splitlines():
        m = CLASS_LINE_RE.match(line)
        if m:
            facts.append(f"class {m.group(1)}")
            continue
        m = DEF_LINE_RE.match(line)
        if m:
            facts.append(f"def {m.group(1)}")
    return facts


def _self_test() -> bool:
    # Complete, valid module — AST path.
    valid_module = "class Foo(Base):\n    def bar(self):\n        pass\n\ndef baz():\n    pass\n"
    facts = extract_code_facts(valid_module)
    assert "class Foo(Base)" in facts, facts
    assert "def bar" in facts, facts
    assert "def baz" in facts, facts

    # Incomplete fragment (real read_file shape) — AST fails, regex must catch it.
    fragment = "        return self.x\n\nclass DagumDistribution(SingleContinuousDistribution):\n    def pdf(self, x):\n"
    frag_facts = extract_code_facts(fragment)
    assert "class DagumDistribution" in frag_facts, frag_facts
    assert "def pdf" in frag_facts, frag_facts

    # Non-code content — must return "", not fabricate anything.
    assert extract_code_facts("Exit code: 0\nSTDOUT:\nhello\n") == ""

    # Cap respected on a large fragment.
    many = "\n".join(f"def f{i}():\n    pass" for i in range(50))
    capped = extract_code_facts(many)
    assert capped.count("def f") <= MAX_FACTS, capped

    return True


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   fact_extraction.extract_code_facts" if ok else "FAILED")
    sys.exit(0 if ok else 1)
