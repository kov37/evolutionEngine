"""Task-derived, model-independent validation contracts.

The contract is intentionally a lightweight parser, not a second model.  It
turns acceptance language already present in an ad-hoc task into executable
evidence requirements and produces a failure-specific next-action suggestion.
It must remain useful when the actor, provider, model, and tool names change.
"""

from dataclasses import dataclass
import re
import shlex

from lifecycle_policy import is_inspection_command, is_output_only_command


def _failure_diagnostic(text: str) -> str:
    """Extract compact exception and assertion-diff evidence."""
    raw = str(text or "")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    useful = [line for line in lines if re.search(
        r"(?:SyntaxError|TypeError|ValueError|KeyError|ImportError|ModuleNotFoundError):", line
    )]
    minus = [line[1:].strip() for line in lines if line.startswith("-") and not line.startswith("---")]
    plus = [line[1:].strip() for line in lines if line.startswith("+") and not line.startswith("+++")]
    if minus and plus:
        useful.append("actual vs expected (unittest '-' is actual, '+' is expected): "
                      + " | ".join(minus[:4]) + " => " + " | ".join(plus[:4]))
    return "\n".join(dict.fromkeys(useful))[:1200]


_CRITERION_RE = re.compile(
    r"(?:^|\n|[.;])\s*(?:[-*]\s*)?(?:(?:the\s+)?(?:app|program|agent|implementation)\s+)?"
    r"(?:must|should|shall|needs? to|support(?:s)?|return(?:s)?|expose(?:s)?|include(?:s)?|verify(?:\s+that)?|test(?:s)?)\b"
    r"[^\n.;]{4,180}", re.IGNORECASE,
)
_ENDPOINT_RE = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD)\s+(/[A-Za-z0-9_./:{}?=&%-]+)", re.IGNORECASE)
# Do not treat the host portion of a URL (the second slash in
# ``http://localhost``) as an application endpoint. Real path tokens must not
# be preceded by a word character or another slash.
_PATH_RE = re.compile(r"(?<![/\w])(/[A-Za-z][A-Za-z0-9_./{}?=&%-]*)")
_CLAUSE_RE = re.compile(
    r"\b(?:must|should|support|expose|serve|return|validate|include|test|start)\b"
    r"[^,.;\n]{4,160}", re.IGNORECASE,
)
_FIELD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:string|number|integer|boolean|bool)\b", re.I)
_STATUS_RE = re.compile(r"\bstatus\s*[=:]\s*([A-Za-z0-9_-]+)", re.I)


def assertion_driven_tool_contract(tool_name, arguments, result_content):
    """Classify raw tool output before applying task-specific assertions.

    A zero exit code is only execution success. It is not automatically
    behavioral evidence. This small shared contract keeps setup, failure, and
    evidence distinct for every task and model.
    """
    text = str(result_content or "")
    if text.startswith(("ERROR:", "REJECTED:")):
        return {"success": False, "evidence": False, "setup_only": False,
                "plane": "non_evidence", "reason": "tool execution failed"}
    args = arguments or {}
    if tool_name == "run_tests":
        passed = text.startswith("(True,")
        return {"success": passed, "evidence": passed, "setup_only": False,
                "plane": "verification" if passed else "non_evidence",
                "reason": "test runner result" if passed else "test runner failed"}
    if tool_name in {"run_command", "run_shell"}:
        if args.get("background") is True or text.startswith("Started background process."):
            return {"success": True, "evidence": False, "setup_only": True,
                    "plane": "setup", "reason": "background process setup is not behavioral evidence"}
        if "Exit code: 0" not in text:
            return {"success": False, "evidence": False, "setup_only": False,
                    "plane": "non_evidence", "reason": "command did not exit successfully"}
        raw_command = args.get("command", "")
        command = raw_command
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        # Keep argv boundaries for inline-interpreter checks. Flattening a
        # quoted ``print('assert passed')`` command makes fake evidence look
        # like a real assertion.
        if is_output_only_command(raw_command):
            return {"success": True, "evidence": False, "setup_only": True,
                    "plane": "setup", "reason": "output-only command is not executable evidence"}
        evidence = bool(re.search(
            r"\b(assert(?:ion)?|received|connected|response|message|pong|websocket|"
            r"handshake|round[- ]?trip|passed|success(?:ful)?)\b",
            f"{command} {text}", re.I,
        ) or re.search(r"\b(?:curl|wget|urllib)\b\s+[^\n]*[/]", str(command), re.I))
        return {"success": True, "evidence": evidence, "setup_only": False,
                "plane": "verification" if evidence else "setup",
                "reason": "behavioral evidence present" if evidence else
                          "zero exit code without a behavioral assertion"}
    if tool_name == "process_status":
        running = "RUNNING" in text
        return {"success": running, "evidence": False, "setup_only": True,
                "plane": "setup",
                "reason": "process readiness is setup, not behavioral evidence" if running else
                          "process is not running"}
    return {"success": False, "evidence": False, "setup_only": False,
            "plane": "non_evidence",
            "reason": "tool does not provide executable behavioral evidence"}


def is_tool_plane_failure(tool_name, result_content):
    """Return true when a tool call failed before the product was exercised.

    The distinction matters during repair.  An unavailable tool, malformed
    tool argument, or dispatch allow-list rejection is a command-plane
    problem; it must reopen a usable validation surface, not trigger a patch
    to the product.  Keep this classifier deliberately narrow so real process
    failures and assertion failures still enter product/setup diagnosis.
    """
    if tool_name not in {"run_command", "run_shell", "run_tests", "process_status", "stop_process"}:
        return False
    lower = str(result_content or "").lower()
    return any(marker in lower for marker in (
        "is unavailable this turn",
        "only [",
        "bad arguments for ",
        "command arguments must be single-line",
        "command must be a non-empty list",
        "invalid command options",
        "unknown tool",
        "invalid tool call",
    )) or (
        # ``python -c`` reports syntax errors against ``<string>``. That is
        # a malformed validation probe, not evidence that the product file is
        # broken. A traceback naming the product file remains a real product
        # failure and intentionally does not match this rule.
        "syntaxerror" in lower and "<string>" in lower
    )


def is_probe_quality_failure(reason):
    """Return true when the implementation was not tested strongly enough.

    A probe can execute successfully yet omit a required assertion, response
    shape, or interface. That is a verification-plan defect, not evidence that
    product code needs mutation.
    """
    lower = str(reason or "").lower()
    return any(marker in lower for marker in (
        "does not assert response shape",
        "does not show an assertion",
        "not executable evidence",
        "not behavioral evidence",
        "only inspected files",
        "zero exit code without a behavioral",
        "output-only command",
        "process readiness is setup",
        "no interaction evidence",
    ))


def is_dependency_setup_command(command) -> bool:
    """Identify normal dependency installation, not a behavioral check."""
    if isinstance(command, (list, tuple)):
        argv = [str(part) for part in command]
    elif isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
    else:
        return False
    if not argv:
        return False
    head = argv[0].rsplit("/", 1)[-1].lower()
    tokens = [token.lower() for token in argv[1:]]
    if head in {"npm", "pnpm", "yarn"}:
        return bool(tokens and tokens[0] in {"install", "ci", "add"})
    if head in {"pip", "pip3"}:
        return bool(tokens and tokens[0] == "install")
    if head in {"python", "python3"}:
        return len(tokens) >= 2 and tokens[:2] == ["-m", "pip"] and "install" in tokens[2:]
    return False


def _inferred_response_requirements(text):
    """Infer only response facts that are explicit in common task wording.

    This is deliberately conservative: the contract should improve the
    actor's probe without inventing a schema from a benchmark name.  A
    resource-creation clause such as ``POST /api/tasks with JSON {title:
    string} returning the created task as JSON`` says two useful things that
    the old field parser missed: the response is an object, and it should
    identify the created resource.  A collection clause says the response is
    a JSON list of objects.  The same wording works for users, jobs, records,
    and other resources.
    """
    lower = text.lower()
    fields = []
    shapes = []

    # Input typed fields are also normally part of a returned resource when
    # the task explicitly says it returns the created resource.
    if re.search(r"return(?:s|ing)?\s+(?:the\s+)?created\s+[^.\n,;]*\bjson", lower):
        fields.extend(_FIELD_RE.findall(text))
        if re.search(r"\b(?:id|identifier)\b", lower) or re.search(
                r"created\s+(?:task|item|record|resource|object|entry|entity)", lower):
            fields.append("id")
        shapes.append("object")

    if re.search(r"return(?:s|ing)?\s+(?:all|a list of|the list of)\s+[^.\n,;]*\bjson", lower):
        shapes.append("list")
        shapes.append("object items")

    # Preserve order while removing duplicates.
    return tuple(dict.fromkeys(fields)), tuple(dict.fromkeys(shapes))


@dataclass(frozen=True)
class ValidationContract:
    task: str
    criteria: tuple
    categories: frozenset
    endpoints: tuple
    fields: tuple = ()
    response_shapes: tuple = ()
    creation_endpoints: tuple = ()
    collection_endpoints: tuple = ()
    operations: tuple = ()

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
            lines.append("Do not stop after one interface: cover every listed interface before finish_task.")
        if self.operations:
            lines.append("Required HTTP operations (method matters): " + ", ".join(self.operations[:8]))
        if self.fields:
            lines.append("Response fields to assert where applicable: " + ", ".join(self.fields))
        if self.response_shapes:
            lines.append("Response shapes to assert where applicable: " + ", ".join(self.response_shapes))
        lines.extend([
            "Evidence rules: tests must assert behavior, API checks must make a request and assert status/body structure, "
            "and CLI checks must assert exit status/output. Do not count startup, compilation, diff, or file listing.",
            "If a check fails, use its output to make the smallest targeted repair, then rerun the relevant check.",
        ])
        return "\n".join(lines)

    def uncovered_endpoints(self, covered_hits):
        """Return required interfaces not represented in accepted evidence."""
        covered = {str(item).lower() for item in (covered_hits or ())}
        if self.operations:
            return tuple(operation for operation in self.operations if operation.lower() not in covered)
        return tuple(endpoint for endpoint in self.endpoints if endpoint.lower() not in covered)

    def assess(self, tool_name, arguments, result_content):
        """Return (accepted, reason, suggestion, evidence_key, hits)."""
        text = str(result_content or "")
        base_contract = assertion_driven_tool_contract(tool_name, arguments, text)
        if not base_contract["success"]:
            return False, "the validation tool failed to execute", "fix the command or test invocation and rerun it", None, ()
        if base_contract["setup_only"]:
            return False, base_contract["reason"], "complete the setup, then run a focused behavioral check", None, ()
        raw_command = (arguments or {}).get("command", "")
        if (tool_name in {"run_command", "run_shell"}
                and is_dependency_setup_command(raw_command)
                and "Exit code: 0" in text):
            return (
                False,
                "dependency setup completed; this is not behavioral evidence",
                "run the focused behavioral smoke test now",
                None,
                (),
            )
        command = raw_command
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
        # A pytest-style module can exit zero when run as a plain script while
        # executing zero test functions. Never treat that silent command as
        # behavioral evidence; require a runner or an explicit test call.
        direct_test_file = re.search(
            r"\bpython3?\s+(?:[^\s]+/)?test[_-][^\s/]+\.py\b", str(command), re.I
        )
        stdout = text.split("STDOUT:", 1)[1].split("STDERR:", 1)[0].strip() if "STDOUT:" in text else text
        stderr = text.split("STDERR:", 1)[1].strip() if "STDERR:" in text else ""
        # A checker can catch its own assertion and still exit zero. Preserve
        # the distinction between a real behavioral failure and a weak probe:
        # explicit failure summaries are product evidence, so they must enter
        # repair rather than being treated as an uninformative readback.
        behavioral_failure = re.search(
            r"(?:\bassert(?:ion)?error\b|\btests?\s+failed\b|\bfailed\s*[:=]|"
            r"\b\d+\s+failed\b|\bchecks?\s*:\s*\d+\s+failed\b)",
            stdout + "\n" + stderr,
            re.IGNORECASE,
        )
        if behavioral_failure:
            return (
                False,
                "the executable behavioral check reported a failure",
                "inspect the failed check output and repair the implicated behavior, then rerun the check",
                None,
                (),
            )
        # Only classify a successful readback as weak evidence after checking
        # for an explicit checker failure. A urllib/curl probe can read an
        # artifact while also reporting failed behavioral assertions; the
        # failure must win so the actor receives a repair packet.
        if tool_name in {"run_command", "run_shell"} and is_inspection_command(raw_command):
            return (
                False,
                "the command only inspected files or reported environment metadata",
                "run an executable behavioral assertion or client exchange instead of listing or printing source",
                None,
                (),
            )
        if direct_test_file and not stdout and not stderr and "-m" not in str(command):
            return (
                False,
                "the test module ran as a script but produced no test evidence",
                "invoke an installed test runner or explicitly call the provided test function, then rerun it",
                None,
                (),
            )
        runner_command = bool(re.search(r"\b(?:pytest|unittest)\b", str(command), re.I))
        no_tests_reported = bool(re.search(
            r"\b(?:ran\s+0\s+tests?|no\s+tests?(?:\s+were)?\s+(?:discovered|found|ran)|"
            r"collected\s+0\s+items?)\b",
            probe,
            re.I,
        ))
        if runner_command and no_tests_reported:
            return (
                False,
                "the test runner exited successfully but discovered zero tests",
                "run a focused test module or a runner command that reports at least one executed test",
                None,
                (),
            )
        endpoint_hits = tuple(p for p in self.endpoints if p.lower() in probe)
        operation_hits = []
        for operation in self.operations:
            method, path = operation.split(None, 1)
            method_present = re.search(rf"\b{re.escape(method)}\b", probe, re.I)
            if method == "GET" and not re.search(
                r"\b(?:post|put|patch|delete|head)\b|(?:-X|--request)\s+\w+", probe, re.I
            ):
                method_present = True
            if len(self.operations) == 1 and path.lower() in probe:
                # A task with one required operation may use a framework
                # default method and omit the verb in its probe.
                method_present = True
            if method in {"POST", "PUT", "PATCH"} and re.search(r"(?:-d|--data|--request)", probe, re.I):
                method_present = True
            if method_present and path.lower() in probe:
                operation_hits.append(operation)
        health_evidence = bool(
            endpoint_hits and any(p.lower().endswith("/health") for p in endpoint_hits)
            and re.search(r"[\"']?status[\"']?\s*[=:]\s*[\"']?ok\b", probe, re.I)
        )
        if tool_name != "run_tests" and not health_evidence and not re.search(
            r"\b(assert(?:ion)?|check|verify|test(?:ing|ed)?|pytest|unittest|curl|wget|http|urllib|health|status|"
            r"received|connected|response|message|pong|websocket|passed)\b|"
            r"\btest[_-][A-Za-z0-9_-]+",
            probe,
            re.I,
        ):
            return False, "the command passed but does not show an assertion or behavioral probe", "replace the smoke command with a focused test or request that asserts the requested behavior", None, ()
        # A command name is not evidence.  In particular, a successful curl
        # can still return a protocol error page (for example, a WebSocket
        # endpoint replying ``Upgrade Required``).  For web tasks without a
        # conventional HTTP endpoint, require the command's actual output to
        # describe an observed interaction or assertion.  This stays
        # provider/model agnostic while preventing premature completion on a
        # clean process exit alone.
        if (tool_name != "run_tests" and "web" in self.categories and not self.endpoints
                and not re.search(
                    r"\b(assert(?:ion)?|verif(?:y|ied)|pass(?:ed)?|success(?:ful)?|"
                    r"received|connected|response|message|pong|websocket|"
                    r"handshake|round[- ]?trip|sent)\b",
                    stdout + " " + stderr,
                    re.I,
                )):
            return False, "the web command exited cleanly but produced no interaction evidence", "run a real client or focused browser check and report the observed response or assertion", None, ()
        hits = tuple(c for c in self.criteria if any(word.lower() in probe for word in _meaningful_words(c)))
        if self.operations and not operation_hits and self.categories.intersection({"api", "web"}):
            return False, "the passing check did not exercise a required HTTP operation", "send a request using the required HTTP method and endpoint, then assert its response", None, ()
        if not self.operations and self.endpoints and not endpoint_hits and self.categories.intersection({"api", "web"}):
            return False, "the passing check did not exercise a task interface", "send a representative request to one of the required interfaces and assert its status and response structure", None, ()
        def field_is_asserted(field):
            # Do not mistake a request payload (e.g. {"title": ...}) for
            # an assertion about the response. Accept common language/runtime
            # access forms without tying the engine to one test framework.
            name = re.escape(field)
            return bool(re.search(
                rf"(?:get\s*\(\s*['\"]{name}['\"]|\[['\"]{name}['\"]\]|"
                rf"['\"]{name}['\"]\s+in|assert[^\n;]*{name}|"
                rf"(?:check|verify|test|pass|returns?|response|✓)[^\n;]{{0,140}}\b{name}\b|"
                rf"[^\n;]{{0,140}}['\"]{name}['\"]\s*:)",
                str(command) + " " + text, re.I,
            ))
        missing_fields = tuple(f for f in self.fields if not field_is_asserted(f))
        write_probe = bool(re.search(r"\b(?:post|put|patch)\b|(?:-d|--data|--request)", probe, re.I))
        creation_probe = any(
            re.search(rf"\b{method}\b", probe, re.I) and path.lower() in probe
            for method, path in (("POST", p) for p in self.creation_endpoints)
        ) or (write_probe and (
            any(p.lower() in self.creation_endpoints for p in endpoint_hits)
            or (not self.creation_endpoints and any("api" in p.lower() for p in endpoint_hits))
        ))
        collection_probe = any(
            re.search(rf"\bGET\b", probe, re.I) and path.lower() in probe
            for path in self.collection_endpoints
        ) or (not write_probe and any(p.lower() in self.collection_endpoints for p in endpoint_hits))
        field_relevant = creation_probe and any("api" in p.lower() for p in endpoint_hits)
        if missing_fields and field_relevant and self.categories.intersection({"api", "web"}) and tool_name != "run_tests":
            return False, "the passing API check does not mention required response fields: " + ", ".join(missing_fields), "assert the response JSON has the required fields and types, then rerun the request", None, ()
        shape_relevant = (
            (creation_probe and "object" in self.response_shapes)
            or (collection_probe and "list" in self.response_shapes)
        )
        if self.response_shapes and shape_relevant and self.categories.intersection({"api", "web"}) and tool_name != "run_tests":
            shape_words = {
                "object": (r"\b(?:dict|object|mapping|json\s*object)\b",),
                "list": (r"\b(?:list|array|sequence)\b",),
                "object items": (r"\b(?:item|record|task|entry)s?\b", r"\b(?:dict|object|isinstance|get|index|iterat|any)\b"),
            }
            missing_shapes = []
            expected_shapes = []
            if creation_probe and "object" in self.response_shapes:
                expected_shapes.append("object")
            if collection_probe:
                expected_shapes.extend(shape for shape in ("list", "object items") if shape in self.response_shapes)
            for shape in expected_shapes:
                if shape not in self.response_shapes:
                    continue
                patterns = shape_words.get(shape, (re.escape(shape),))
                evidence = str(command) + " " + text
                concrete_json_shape = {
                    "object": bool(re.search(r"(?:response|json|/(?:api|health)[^\n:]{0,80})\s*:\s*\{|(?:^|\n)\s*\{", evidence, re.I)),
                    "list": bool(re.search(r"(?:response|json|/(?:api|health)[^\n:]{0,80})\s*:\s*\[|(?:^|\n)\s*\[", evidence, re.I)),
                    "object items": bool(re.search(r"(?:response|json|/(?:api|health)[^\n:]{0,80})\s*:\s*\[\s*\{|(?:^|\n)\s*\[\s*\{", evidence, re.I)),
                }
                if not concrete_json_shape.get(shape, False) and not all(
                    re.search(pattern, evidence, re.I) for pattern in patterns
                ):
                    missing_shapes.append(shape)
            if missing_shapes:
                return False, "the passing API check does not assert response shape: " + ", ".join(missing_shapes), "assert the response is the required JSON object/list and inspect each returned item, then rerun the request", None, ()
        key = f"{tool_name}|{command}|{text}"
        return True, "behavioral evidence accepted", "", key, hits + endpoint_hits + tuple(operation_hits) + self.fields

    @staticmethod
    def is_lifecycle_setup(tool_name, arguments, result_content):
        """Return true for process setup/cleanup, not behavior evidence."""
        text = str(result_content or "")
        if tool_name in {"run_command", "run_shell"} and (arguments or {}).get("background") is True:
            return text.startswith("Started background process.")
        if (tool_name in {"run_command", "run_shell"}
                and is_dependency_setup_command((arguments or {}).get("command", ""))):
            return "Exit code: 0" in text
        if tool_name == "process_status":
            return "RUNNING" in text
        if tool_name == "stop_process":
            return text.startswith("Stopped process")
        return False

    def synthesize_failure_feedback(self, tool_name, arguments, result_content):
        """Return bounded next-action feedback for a failed tool result."""
        return self.failure_packet(tool_name, arguments, result_content)[:2200]

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
        write_probe = bool(re.search(r"\b(?:post|put|patch)\b|(?:-d|--data|--request)", joined, re.I))
        field_relevant = write_probe and endpoint and endpoint.lower() in self.creation_endpoints
        collection_relevant = (not write_probe and endpoint and endpoint.lower() in self.collection_endpoints)
        if self.fields and field_relevant:
            expected.append("assert response field(s): " + ", ".join(self.fields))
        if self.response_shapes and field_relevant:
            expected.append("assert response shape: object")
        elif self.response_shapes and collection_relevant:
            expected.append("assert response shape: list and object items")
        matching = [c for c in self.criteria if endpoint is None or endpoint.lower() in c.lower()]
        expected.extend(matching[:2])
        if not expected:
            expected.append("satisfy the task's acceptance criteria with an executable assertion")
        observed = text.strip().replace("\x00", "")
        if len(observed) > 900:
            observed = observed[-900:]
        if not observed:
            observed = "(no tool output)"
        lower_observed = observed.lower()
        diagnostic = _failure_diagnostic(observed)
        if re.search(r"\b(?:no\s+tests?\s+ran|ran\s+0\s+tests?|collected\s+0\s+items?)\b", lower_observed):
            next_action = (
                "the command did not execute any assertions; inspect the supplied test file and either invoke "
                "its test function directly or use the correct installed test runner, then rerun the focused check"
            )
        elif "modulenotfounderror" in lower_observed or "no module named" in lower_observed:
            next_action = (
                "inspect project declarations and determine whether the missing dependency is required; if required, "
                "install it through the project's normal workflow and record it in the dependency declaration, "
                "otherwise replace an ad hoc probe dependency with a standard-library or existing-project equivalent, "
                "then rerun the check"
            )
        elif ("timeout" in lower_observed or "timed out" in lower_observed) and endpoint and write_probe:
            next_action = (
                f"the service is reachable but {endpoint} blocked during a state-changing request; inspect "
                "that handler for a deadlock, blocking lock, or I/O wait. Do not change a passing health "
                "handler; make one targeted repair, then rerun the request with a short timeout"
            )
        elif "timeout" in lower_observed or "timed out" in lower_observed:
            next_action = (
                "do not use a foreground long-running process as the check; launch the service with a bounded "
                "background lifecycle and issue a focused request or health probe"
            )
        elif endpoint and field_relevant:
            next_action = f"inspect the handler for {endpoint}; return a JSON object and assert its fields before rechecking"
        elif endpoint and collection_relevant:
            next_action = f"inspect the handler for {endpoint}; return a JSON collection and assert each item before rechecking"
        elif endpoint:
            next_action = f"inspect the handler for {endpoint} and repair the behavior shown by the failure"
        else:
            next_action = "inspect the implementation named by the failure and make one minimal targeted repair"
        return (
            "## Repair packet\n"
            f"Failed probe: {str(command).strip() or tool_name}\n"
            "Expected:\n- " + "\n- ".join(expected) + "\n"
            "Observed failure:\n" + observed + "\n"
            + ("Structured diagnosis:\n" + diagnostic + "\n" if diagnostic else "")
            + "Next repair focus:\n" + next_action + "\n"
            "Constraint: make one concrete mutation now; do not rerun the same check unchanged. "
            "If actual and expected values differ, treat that difference as a behavioral contract to explain, "
            "not only as a type or syntax problem."
        )


def _meaningful_words(text):
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text) if w.lower() not in {
        "must", "should", "shall", "return", "include", "support", "expose", "verify", "that", "with", "from", "into"
    }][:10]


def _is_probable_filesystem_path(path: str) -> bool:
    """Reject traceback/workspace paths when extracting app interfaces."""
    normalized = str(path or "").replace("\\", "/").lower()
    return normalized.startswith((
        "/private/", "/var/", "/tmp/", "/users/", "/home/", "/workspace/",
    )) or "/.agentic" in normalized


def from_task(task, task_type="code_change"):
    text = str(task or "")
    criteria = []
    for match in _CRITERION_RE.findall(text) + _CLAUSE_RE.findall(text):
        item = " ".join(match.strip().split())
        if item not in criteria:
            criteria.append(item)
    endpoints = []
    for match in _ENDPOINT_RE.findall(text) + _PATH_RE.findall(text):
        if not _is_probable_filesystem_path(match) and match not in endpoints:
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
    inferred_fields, response_shapes = _inferred_response_requirements(text)
    for field in inferred_fields:
        if field not in fields:
            fields.append(field)
    creation_endpoints = []
    collection_endpoints = []
    operations = []
    endpoint_matches = [
        match for match in _ENDPOINT_RE.finditer(text)
        if not _is_probable_filesystem_path(match.group(1))
    ]
    for index, match in enumerate(endpoint_matches):
        method, path = match.group(0).split(None, 1)
        method = method.upper()
        operations.append(f"{method} {path}")
        clause_end = endpoint_matches[index + 1].start() if index + 1 < len(endpoint_matches) else len(text)
        clause = text[match.start():clause_end]
        if method in {"POST", "PUT", "PATCH"} and "created" in clause.lower():
            creation_endpoints.append(path)
        if method == "GET" and re.search(r"\b(?:all|list|list of)\b", clause, re.I):
            collection_endpoints.append(path)
    if response_shapes and not creation_endpoints:
        creation_endpoints = [p for p in endpoints if "api" in p.lower()]
    if "list" in response_shapes and not collection_endpoints:
        collection_endpoints = [p for p in endpoints if "api" in p.lower()]
    return ValidationContract(text, tuple(criteria), frozenset(categories), tuple(endpoints), tuple(fields), response_shapes,
                              tuple(dict.fromkeys(p.lower() for p in creation_endpoints)),
                              tuple(dict.fromkeys(p.lower() for p in collection_endpoints)),
                              tuple(dict.fromkeys(operations)))


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
    missing = api.failure_packet("run_command", {"command": "python test_server.py"}, "Exit code: 1\nModuleNotFoundError: No module named 'requests'")
    assert "standard-library" in missing
    timeout = api.failure_packet("run_command", {"command": "python server.py"}, "TIMEOUT after 5s — command likely hung")
    assert "foreground long-running process" in timeout
    created = from_task("Support POST /api/tasks with JSON {title: string} returning the created task as JSON.")
    assert created.fields == ("title", "id") and created.response_shapes == ("object",)
    assert created.creation_endpoints == ("/api/tasks",)
    assert not created.assess("run_shell", {"command": "curl -X POST /api/tasks -d '{title: x}'; assert response.get('title') == 'x'"}, "Exit code: 0\nOK")[0]
    script_evidence = (
        "Exit code: 0\n"
        "Testing POST /api/tasks... Response: {\"id\": 1, \"title\": \"Ship it\"}\n"
        "✓ POST /api/tasks returns task with id and title\n"
        "Testing GET /api/tasks... Response: [{\"id\": 1, \"title\": \"Ship it\"}]\n"
        "✓ GET /api/tasks returns list of tasks with id and title"
    )
    assert created.assess("run_shell", {"command": "bash validate.sh"}, script_evidence)[0]
    collection = from_task("Support GET /api/tasks returning all tasks as JSON.")
    assert collection.response_shapes == ("list", "object items")
    assert collection.assess("run_shell", {"command": "curl /api/tasks"}, "Exit code: 0\n[{\"id\": 1, \"title\": \"Ship it\"}]")[0]
    multi = from_task("Expose GET /health and POST /api/tasks with JSON {title: string} returning the created task as JSON.")
    assert multi.uncovered_endpoints({"GET /health"}) == ("POST /api/tasks",)
    assert multi.uncovered_endpoints({"POST /api/tasks"}) == ("GET /health",)
    multi_collection = from_task("Expose GET /health, POST /api/tasks returning the created task, and GET /api/tasks returning all tasks as JSON.")
    assert multi_collection.uncovered_endpoints({"GET /health", "POST /api/tasks"}) == ("GET /api/tasks",)
    combined_bad = "Exit code: 0\nPOST /api/tasks: {\"id\": 1, \"title\": \"x\"}\nGET /api/tasks: {\"tasks\": [{\"id\": 1, \"title\": \"x\"}]}"
    assert not multi_collection.assess("run_shell", {"command": "curl POST GET /api/tasks"}, combined_bad)[0]
    assert multi.assess("run_shell", {"command": "curl /health"}, "Exit code: 0\n{\"status\":\"ok\"}")[0]
    assert c.assess("run_shell", {"command": "curl /health"}, "Exit code: 0\n{\"status\": \"ok\"}")[0]
    assert api.is_lifecycle_setup("run_command", {"background": True}, "Started background process.\nHandle: proc-x")
    assert api.is_lifecycle_setup("process_status", {}, "RUNNING\nHandle: proc-x")


if __name__ == "__main__":
    _self_test()
    print("OK validation_contract")
