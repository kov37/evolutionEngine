"""Task-derived, model-independent validation contracts.

The contract is intentionally a lightweight parser, not a second model.  It
turns acceptance language already present in an ad-hoc task into executable
evidence requirements and produces a failure-specific next-action suggestion.
It must remain useful when the actor, provider, model, and tool names change.
"""

from dataclasses import dataclass
import re


_CRITERION_RE = re.compile(
    r"(?:^|\n|[.;])\s*(?:[-*]\s*)?(?:(?:the\s+)?(?:app|program|agent|implementation)\s+)?"
    r"(?:must|should|shall|needs? to|support(?:s)?|return(?:s)?|expose(?:s)?|include(?:s)?|verify(?:\s+that)?|test(?:s)?)\b"
    r"[^\n.;]{4,180}", re.IGNORECASE,
)
_ENDPOINT_RE = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD)\s+(/[A-Za-z0-9_./:{}?=&%-]+)", re.IGNORECASE)
_PATH_RE = re.compile(r"(?<!\w)(/[A-Za-z][A-Za-z0-9_./{}?=&%-]*)")
_CLAUSE_RE = re.compile(
    r"\b(?:must|should|support|expose|serve|return|validate|include|test|start)\b"
    r"[^,.;\n]{4,160}", re.IGNORECASE,
)
_FIELD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:string|number|integer|boolean|bool)\b", re.I)
_STATUS_RE = re.compile(r"\bstatus\s*[=:]\s*([A-Za-z0-9_-]+)", re.I)


@dataclass(frozen=True)
class ValidationContract:
    task: str
    criteria: tuple
    categories: frozenset
    endpoints: tuple
    fields: tuple = ()

    @property
    def has_criteria(self):
        return bool(self.criteria or self.endpoints)

    def render(self):
        if not self.has_criteria:
            return (
                "## Validation contract\n"
                "Derive a focused executable check from the task. A file listing, diff, import, "
                "or process startup alone is not behavioral validation."
            )
        lines = [
            "## Validation contract",
            "Every acceptance criterion below needs executable evidence before finish_task.",
        ]
        for criterion in self.criteria[:8]:
            lines.append(f"- {criterion}")
        if self.endpoints:
            lines.append("Representative interfaces to exercise: " + ", ".join(self.endpoints[:8]))
        if self.fields:
            lines.append("Response fields to assert where applicable: " + ", ".join(self.fields))
        lines.extend([
            "Evidence rules: tests must assert behavior, API checks must make a request and assert status/body structure, "
            "and CLI checks must assert exit status/output. Do not count startup, compilation, diff, or file listing.",
            "If a check fails, use its output to make the smallest targeted repair, then rerun the relevant check.",
        ])
        return "\n".join(lines)

    def assess(self, tool_name, arguments, result_content):
        """Return (accepted, reason, suggestion, evidence_key, hits)."""
        text = str(result_content or "")
        if text.startswith(("ERROR:", "REJECTED:")):
            return False, "the validation tool failed to execute", "fix the command or test invocation and rerun it", None, ()
        command = (arguments or {}).get("command", "")
        if isinstance(command, list):
            command = " ".join(str(x) for x in command)
        probe = f"{command} {text}".lower()
        if tool_name == "run_tests":
            passed = text.startswith("(True,")
        elif tool_name in {"run_command", "run_shell"}:
            passed = "Exit code: 0" in text
        else:
            passed = False
        if not passed:
            return False, "the executable check did not pass", "read the failure output, repair the implementation, and rerun the focused check", None, ()
        if tool_name != "run_tests" and not re.search(r"\b(assert|check|verify|test|pytest|unittest|curl|wget|http|urllib|health|status)\b", probe, re.I):
            return False, "the command passed but does not show an assertion or behavioral probe", "replace the smoke command with a focused test or request that asserts the requested behavior", None, ()
        hits = tuple(c for c in self.criteria if any(word.lower() in probe for word in _meaningful_words(c)))
        endpoint_hits = tuple(p for p in self.endpoints if p.lower() in probe)
        if self.endpoints and not endpoint_hits and self.categories.intersection({"api", "web"}):
            return False, "the passing check did not exercise a task interface", "send a representative request to one of the required interfaces and assert its status and response structure", None, ()
        def field_is_asserted(field):
            # Do not mistake a request payload (e.g. {"title": ...}) for
            # an assertion about the response. Accept common language/runtime
            # access forms without tying the engine to one test framework.
            name = re.escape(field)
            return bool(re.search(
                rf"(?:get\s*\(\s*['\"]{name}['\"]|\[['\"]{name}['\"]\]|"
                rf"['\"]{name}['\"]\s+in|assert[^\n;]*{name})",
                str(command) + " " + text, re.I,
            ))
        missing_fields = tuple(f for f in self.fields if not field_is_asserted(f))
        field_relevant = any("api" in p.lower() for p in endpoint_hits)
        if missing_fields and field_relevant and self.categories.intersection({"api", "web"}) and tool_name != "run_tests":
            return False, "the passing API check does not mention required response fields: " + ", ".join(missing_fields), "assert the response JSON has the required fields and types, then rerun the request", None, ()
        key = f"{tool_name}|{command}|{text}"
        return True, "behavioral evidence accepted", "", key, hits + endpoint_hits + self.fields

    def failure_packet(self, tool_name, arguments, result_content):
        """Render compact, actionable evidence for the mandatory repair turn."""
        text = str(result_content or "")
        command = (arguments or {}).get("command", "")
        if isinstance(command, list):
            command = " ".join(str(x) for x in command)
        joined = f"{command} {text}"
        endpoint = next((p for p in self.endpoints if p.lower() in joined.lower()), None)
        expected = []
        if endpoint:
            expected.append(f"exercise {endpoint}")
        if self.fields:
            expected.append("assert response field(s): " + ", ".join(self.fields))
        matching = [c for c in self.criteria if endpoint is None or endpoint.lower() in c.lower()]
        expected.extend(matching[:2])
        if not expected:
            expected.append("satisfy the task's acceptance criteria with an executable assertion")
        observed = text.strip().replace("\x00", "")
        if len(observed) > 900:
            observed = observed[-900:]
        if not observed:
            observed = "(no tool output)"
        if endpoint and self.fields:
            next_action = f"inspect the handler for {endpoint}; return a JSON object and assert its fields before rechecking"
        elif endpoint:
            next_action = f"inspect the handler for {endpoint} and repair the behavior shown by the failure"
        else:
            next_action = "inspect the implementation named by the failure and make one minimal targeted repair"
        return (
            "## Repair packet\n"
            f"Failed probe: {str(command).strip() or tool_name}\n"
            "Expected:\n- " + "\n- ".join(expected) + "\n"
            "Observed failure:\n" + observed + "\n"
            "Next repair focus:\n" + next_action + "\n"
            "Constraint: make one concrete mutation now; do not rerun the same check unchanged."
        )


def _meaningful_words(text):
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text) if w.lower() not in {
        "must", "should", "shall", "return", "include", "support", "expose", "verify", "that", "with", "from", "into"
    }][:10]


def from_task(task, task_type="code_change"):
    text = str(task or "")
    criteria = []
    for match in _CRITERION_RE.findall(text) + _CLAUSE_RE.findall(text):
        item = " ".join(match.strip().split())
        if item not in criteria:
            criteria.append(item)
    endpoints = []
    for match in _ENDPOINT_RE.findall(text) + _PATH_RE.findall(text):
        if match not in endpoints:
            endpoints.append(match)
    lower = text.lower()
    categories = set()
    if endpoints or any(w in lower for w in ("api", "http", "json", "endpoint", "server")):
        categories.update(("api", "web"))
    if any(w in lower for w in ("command line", "cli", "command", "exit code", "stdout")):
        categories.add("cli")
    if any(w in lower for w in ("function", "class", "library", "pytest", "regression test")):
        categories.add("library")
    if any(w in lower for w in ("file", "artifact", "app", "application", "gui")):
        categories.add("artifact")
    fields = []
    for field in _FIELD_RE.findall(text):
        if field not in fields:
            fields.append(field)
    return ValidationContract(text, tuple(criteria), frozenset(categories), tuple(endpoints), tuple(fields))


def _self_test():
    c = from_task("Create an app. It must expose GET /health and return JSON. Test the endpoint.")
    assert c.has_criteria and "/health" in c.endpoints and "api" in c.categories
    ok = c.assess("run_shell", {"command": "curl http://localhost/health and assert status"}, "Exit code: 0\nOK")[0]
    assert ok
    bad = c.assess("run_shell", {"command": "python server.py"}, "Exit code: 0\nstarted")[0]
    assert not bad
    api = from_task("Support POST /api/tasks with JSON {title: string} returning JSON.")
    assert not api.assess("run_shell", {"command": "curl /api/tasks -d '{title: x}'"}, "Exit code: 0\nOK")[0]
    assert api.assess("run_shell", {"command": "curl /api/tasks; assert response.get('title') == 'x'"}, "Exit code: 0\nOK")[0]
    assert "Repair packet" in api.failure_packet("run_shell", {"command": "curl /api/tasks"}, "Exit code: 1\nAssertionError")


if __name__ == "__main__":
    _self_test()
    print("OK validation_contract")
