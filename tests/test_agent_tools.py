import tempfile
import urllib.error
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from agent import ORIENTATION_TURN_BUDGET, REPAIR_TURN_BUDGET, _TOKENIZE_UNAVAILABLE_BASE_URLS, _authoritative_gate_restrictions, _completion_ready, _consume_worker_gate, _fit_llama_prompt, _force_repair_recovery, _has_orientation_evidence, _intervention_messages, _is_validation_setup_failure, _json_message, _llama_cpp_chat, _terminal_provider_error, _worker_triage_enabled
from dispatch import _format_result, _normalize_tool_arguments, dispatch_tool_calls
import action_governor
from kernel.discovery import find_files
from kernel.exec_tools import run_command
from kernel.io_tools import patch_file, validate_python_syntax
from risk_layer import RiskLayer
from registry import _wrap_with_confinement
from kernel.sandbox import set_root
from novelty_context import NoveltyContext, WorkerJudgment, _parse_judgment
from validation_contract import _failure_diagnostic, assertion_driven_tool_contract, from_task
from lifecycle_fsm import InvalidTransition, LifecycleFSM, LifecycleState
from lifecycle_policy import build_validation_policy, is_inspection_command, orientation_action_tools
from workspace import run_tests_tool
from workspace.run_tests_tool import run_tests
from agentic_benchmark import TASKS, _profile_limits, _run_completed, _scorecard_passed


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeResponse:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class KernelToolTests(unittest.TestCase):
    def test_llama_adapter_sends_explicit_thinking_switch(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {},
                }).encode()

        def fake_urlopen(request, timeout=None):
            captured.update(json.loads(request.data.decode()))
            return Response()

        with patch("agent._fit_llama_prompt", return_value=([], 3, 100)), \
             patch("agent.urllib.request.urlopen", side_effect=fake_urlopen):
            _llama_cpp_chat(
                base_url="http://provider/v1", timeout_seconds=1,
                model="model", messages=[], tools=[], think=False,
            )
        self.assertEqual(captured["chat_template_kwargs"], {"enable_thinking": False})

    def test_lifecycle_fsm_has_deterministic_repair_path(self):
        fsm = LifecycleFSM()
        self.assertEqual(fsm.transition("turn"), LifecycleState.ACT)
        self.assertEqual(fsm.transition("orientation_stalled"), LifecycleState.RECOVER)
        self.assertEqual(fsm.transition("turn"), LifecycleState.RECOVER)
        self.assertEqual(fsm.transition("mutation"), LifecycleState.VALIDATE)
        self.assertEqual(fsm.transition("validation_failed"), LifecycleState.REPAIR)
        self.assertEqual(fsm.transition("recovery_budget_exhausted"), LifecycleState.RECOVER)
        self.assertEqual(fsm.transition("mutation"), LifecycleState.VALIDATE)
        self.assertEqual(fsm.transition("validation_passed"), LifecycleState.COMPLETE)
        self.assertEqual(fsm.metrics()["transitions"], 8)

    def test_lifecycle_fsm_rejects_impossible_transition(self):
        fsm = LifecycleFSM()
        with self.assertRaises(InvalidTransition):
            fsm.transition("validation_passed")

    def test_lifecycle_fsm_completes_from_setup_recovery(self):
        fsm = LifecycleFSM()
        fsm.transition("turn")
        fsm.transition("mutation")
        fsm.transition("validation_failed")
        fsm.transition("recovery_budget_exhausted")
        self.assertEqual(fsm.transition("validation_passed"), LifecycleState.COMPLETE)

    def test_lifecycle_fsm_completes_after_setup_repair_without_product_mutation(self):
        fsm = LifecycleFSM()
        fsm.transition("turn")
        fsm.transition("mutation")
        fsm.transition("validation_failed")
        self.assertEqual(fsm.transition("validation_passed"), LifecycleState.COMPLETE)

    def test_setup_failure_never_forces_product_rewrite(self):
        self.assertFalse(_force_repair_recovery(True, True, True))
        self.assertTrue(_force_repair_recovery(True, True, False))
        self.assertFalse(_force_repair_recovery(False, True, False))

    def test_stale_worker_gate_cannot_remove_behavior_repair_tools(self):
        self.assertEqual(
            _authoritative_gate_restrictions({"patch_file", "write_file", "finish_task"}, False),
            {"finish_task"},
        )
        self.assertEqual(
            _authoritative_gate_restrictions({"patch_file", "write_file"}, True),
            {"patch_file", "write_file"},
        )

    def test_worker_gate_is_opt_in(self):
        class Worker:
            def consume_gate_restrictions(self):
                return {"patch_file"}
        self.assertEqual(_consume_worker_gate(False, Worker()), set())
        self.assertEqual(_consume_worker_gate(True, Worker()), {"patch_file"})
        self.assertFalse(_worker_triage_enabled(False, False))
        self.assertTrue(_worker_triage_enabled(True, False))
        self.assertTrue(_worker_triage_enabled(False, True))

    def test_validation_policy_is_setup_then_command(self):
        first = build_validation_policy(
            validation_required=True, repair_required=True, setup_failure=True,
            repair_inspection_used=False, last_mutation_rejected=False,
            validation_failures=1, protected_edit_recovery_pending=False,
            repair_recovery_mode=False,
        )
        second = build_validation_policy(
            validation_required=True, repair_required=True, setup_failure=True,
            repair_inspection_used=True, last_mutation_rejected=False,
            validation_failures=1, protected_edit_recovery_pending=False,
            repair_recovery_mode=False,
        )
        self.assertNotIn("patch_file", first.tools)
        self.assertNotIn("write_file", first.tools)
        self.assertIn("read_file", first.tools)
        self.assertIn("run_command", first.tools)
        self.assertNotIn("patch_file", second.tools)
        self.assertEqual(second.tools & {"run_tests", "run_command"}, {"run_tests", "run_command"})
        self.assertTrue(second.setup_recovery)

    def test_validation_policy_is_repair_then_patch(self):
        policy = build_validation_policy(
            validation_required=True, repair_required=True, setup_failure=False,
            repair_inspection_used=True, last_mutation_rejected=False,
            validation_failures=1, protected_edit_recovery_pending=False,
            repair_recovery_mode=False,
        )
        self.assertNotIn("read_file", policy.tools)
        self.assertIn("patch_file", policy.tools)
        self.assertTrue(policy.requires_mutation)

    def test_orientation_recovery_keeps_targeted_read_available(self):
        tools = orientation_action_tools()
        self.assertIn("read_file", tools)
        self.assertIn("patch_file", tools)
        self.assertNotIn("list_workspace", tools)

    def test_orientation_recovery_closes_reads_after_evidence_exists(self):
        tools = orientation_action_tools(evidence_available=True)
        self.assertNotIn("read_file", tools)
        self.assertNotIn("search_file", tools)
        self.assertIn("patch_file", tools)
        self.assertIn("run_command", tools)

    def test_orientation_evidence_ignores_empty_and_error_reads(self):
        self.assertFalse(_has_orientation_evidence([
            {"role": "tool", "tool_name": "read_file", "content": ""},
            {"role": "tool", "tool_name": "read_file", "content": "ERROR: missing"},
            {"role": "tool", "tool_name": "read_file", "content": "--- app.py [lines 1-0 of 12] (0 chars) ---"},
        ]))
        self.assertTrue(_has_orientation_evidence([
            {"role": "tool", "tool_name": "read_file", "content": "line 1: implementation"},
        ]))

    def test_orientation_shell_guard_blocks_only_simple_inspection(self):
        self.assertTrue(is_inspection_command(["cat", "server.js"]))
        self.assertTrue(is_inspection_command("sed -n '1,20p' index.html"))
        self.assertTrue(is_inspection_command([
            "node", "-e", "console.log(require('fs').readFileSync('server.js','utf8'))",
        ]))
        self.assertTrue(is_inspection_command([
            "python3", "-c", "print(open('index.html').read())",
        ]))
        self.assertFalse(is_inspection_command(["npm", "install"]))
        self.assertFalse(is_inspection_command(["node", "smoke_test.js"]))
        self.assertFalse(is_inspection_command([
            "node", "-e", "const fs=require('fs'); console.log(fs.readFileSync('x')); assert(true)",
        ]))
        self.assertFalse(is_inspection_command("cat server.js && node smoke_test.js"))

    def test_recovery_guard_is_safe_before_first_validation(self):
        self.assertFalse(_force_repair_recovery(False, False, False))

    def test_setup_classification_survives_compact_failure_summary(self):
        self.assertTrue(_is_validation_setup_failure(
            "the test module produced no test evidence; invoke a test runner"
        ))
        self.assertTrue(_is_validation_setup_failure(
            "the command passed but does not show an assertion or behavioral probe"
        ))

    def test_repair_budget_checkpoint_is_bounded(self):
        self.assertEqual(REPAIR_TURN_BUDGET, 3)
        checkpoint = _intervention_messages(
            [{"role": "system", "content": "foundation"},
             {"role": "user", "content": "task"}] +
            [{"role": "tool", "content": f"evidence-{i}"} for i in range(12)],
            tail=8,
        )
        self.assertEqual(checkpoint[0]["content"], "foundation")
        self.assertEqual(checkpoint[1]["content"], "task")
        self.assertEqual(
            [m["content"] for m in checkpoint[-8:]],
            [f"evidence-{i}" for i in range(4, 12)],
        )
        self.assertEqual(ORIENTATION_TURN_BUDGET, 2)

    def test_tool_call_arguments_are_serialized_at_transport_boundary(self):
        message = type("Message", (), {
            "role": "assistant",
            "content": "",
            "tool_calls": [type("Call", (), {
                "function": type("Function", (), {
                    "name": "read_file",
                    "arguments": {"path": "server.js"},
                })(),
            })()],
        })()
        normalized = _json_message(message)
        self.assertEqual(
            normalized["tool_calls"][0]["function"]["arguments"],
            '{"path": "server.js"}',
        )

    def test_websocket_grader_defines_its_probe_port(self):
        grade = TASKS["websocket_chat"].grade
        compile(grade, ".agentic_grader.py", "exec")
        self.assertIn("port = 18767", grade)
        self.assertIn("env['PORT'] = str(port)", grade)

    def test_smoke_profile_caps_expensive_real_model_runs(self):
        self.assertEqual(_profile_limits("smoke", 20, 90, 600), (8, 45.0, 300.0))
        self.assertEqual(_profile_limits("full", 20, 90, 600), (20, 90, 600))

    def test_interrupted_artifact_is_not_reported_as_completed(self):
        self.assertFalse(_run_completed(False, -15))
        self.assertFalse(_run_completed(True, -15))
        self.assertTrue(_run_completed(False, 0))

    def test_artifact_without_finish_signal_is_not_a_pass(self):
        self.assertFalse(_scorecard_passed(True, False, True))
        self.assertFalse(_scorecard_passed(True, True, False))
        self.assertTrue(_scorecard_passed(True, True, True))


    def test_risk_layer_rolls_back_destructive_repair_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "service.py"
            original = "\n".join(f"def helper_{i}(): return {i}" for i in range(12)) + "\n"
            target.write_text(original, encoding="utf-8")
            layer = RiskLayer()
            layer.checkpoint("service.py", str(target), turn=1)
            target.write_text("def helper_0(): return 999\n", encoding="utf-8")
            reason = layer.reject_destructive_rewrite(
                "service.py", str(target), tool_name="write_file", repair_turn=True,
            )
            self.assertIsNotNone(reason)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_risk_layer_allows_initial_and_surgical_rewrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "service.py"
            target.write_text("\n".join(f"def helper_{i}(): return {i}" for i in range(12)) + "\n", encoding="utf-8")
            layer = RiskLayer()
            layer.checkpoint("service.py", str(target), turn=1)
            target.write_text("\n".join(f"def helper_{i}(): return {999 if i == 0 else i}" for i in range(12)) + "\n", encoding="utf-8")
            self.assertIsNone(layer.reject_destructive_rewrite(
                "service.py", str(target), tool_name="write_file", repair_turn=True,
            ))
            self.assertIsNone(layer.reject_destructive_rewrite(
                "service.py", str(target), tool_name="write_file", repair_turn=False,
            ))

    def test_risk_layer_protects_existing_tests_but_allows_new_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            supplied = root / "test_contract.py"
            supplied.write_text("def test_contract(): pass\n", encoding="utf-8")
            layer = RiskLayer()
            layer.protect_existing_tests(tmp)
            layer.checkpoint("test_contract.py", str(supplied), turn=1)
            supplied.write_text("def test_contract(): assert False\n", encoding="utf-8")
            reason = layer.reject_protected_test_mutation(
                "test_contract.py", str(supplied), repair_turn=True,
            )
            self.assertIsNotNone(reason)
            self.assertEqual(supplied.read_text(encoding="utf-8"), "def test_contract(): pass\n")
            self.assertIsNone(layer.reject_protected_test_mutation(
                "new_test.py", str(root / "new_test.py"), repair_turn=True,
            ))
    def test_run_tests_timeout_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_hang.py").write_text(
                "import time\nimport unittest\n"
                "class Contract(unittest.TestCase):\n"
                "    def test_hangs(self):\n"
                "        time.sleep(0.05)\n",
                encoding="utf-8",
            )
            previous = run_tests_tool.RUN_TESTS_TIMEOUT_SECONDS
            run_tests_tool.RUN_TESTS_TIMEOUT_SECONDS = 0.01
            try:
                success, summary = run_tests(str(root))
            finally:
                run_tests_tool.RUN_TESTS_TIMEOUT_SECONDS = previous
            self.assertFalse(success)
            self.assertIn("timed out", summary)

    def test_dispatch_normalizes_common_command_schema_drift(self):
        self.assertEqual(
            _normalize_tool_arguments(
                "run_command",
                {"argv": '["python3", "-m", "unittest"]', "timeout_ms": 2500},
            ),
            {"command": ["python3", "-m", "unittest"], "timeout": 3},
        )
        self.assertEqual(
            _normalize_tool_arguments("run_command", {"command": "python3 -V"})["command"],
            ["python3", "-V"],
        )
        self.assertNotIn(
            "cwd",
            _normalize_tool_arguments("run_command", {"command": ["python3"], "cwd": '"."'}),
        )

    def test_dispatch_blocks_a_rejected_protected_mutation_path(self):
        class Call:
            class Function:
                name = "patch_file"
                arguments = {"path": "test_contract.py", "search": "x", "replace": "y"}
            function = Function()
        messages = dispatch_tool_calls(
            [Call()], {"patch_file": lambda **_: "should not run"},
            blocked_mutation_paths={"test_contract.py"},
        )
        self.assertTrue(messages[0]["content"].startswith("REJECTED:"))
        self.assertIn("blocked", messages[0]["content"])
        self.assertEqual(
            _normalize_tool_arguments(
                "run_command",
                {"command": ["python3"], "timeout": "30,\nbackground=False]"},
            )["timeout"],
            30,
        )

    def test_web_validation_rejects_clean_protocol_error_without_evidence(self):
        contract = from_task(
            "Build a WebSocket server and run a real local client smoke test."
        )
        accepted, reason, *_ = contract.assess(
            "run_command",
            {"command": ["curl", "http://127.0.0.1:8080"]},
            "Exit code: 0\nSTDOUT:\nUpgrade Required\nSTDERR:\n",
        )
        self.assertFalse(accepted)
        self.assertIn("interaction evidence", reason)

    def test_web_validation_rejects_dependency_setup_output(self):
        contract = from_task(
            "Build a WebSocket server and run a real local client smoke test."
        )
        accepted, reason, *_ = contract.assess(
            "run_command",
            {"command": ["npm", "init", "-y"]},
            "Exit code: 0\nWrote package.json with scripts.test\n",
        )
        self.assertFalse(accepted)
        self.assertIn("interaction evidence", reason)

    def test_validation_rejects_wrapped_file_dump_as_behavior(self):
        contract = from_task("Build a WebSocket server and run a real local client smoke test.")
        accepted, reason, *_ = contract.assess(
            "run_command",
            {"command": ["bash", "-c", "cat index.html"]},
            "Exit code: 0\nSTDOUT:\nnew WebSocket('ws://localhost:8080');\n",
        )
        self.assertFalse(accepted)
        self.assertIn("only inspected files", reason)

    def test_assertion_contract_separates_setup_from_evidence(self):
        setup = assertion_driven_tool_contract(
            "run_command", {"command": ["npm", "install"]},
            "Exit code: 0\nadded 1 package\n",
        )
        self.assertTrue(setup["success"])
        self.assertFalse(setup["evidence"])
        self.assertFalse(setup["setup_only"])
        self.assertEqual(setup["plane"], "setup")
        observed = assertion_driven_tool_contract(
            "run_command", {"command": ["node", "probe.cjs"]},
            "Exit code: 0\nreceived pong; assertion passed\n",
        )
        self.assertTrue(observed["evidence"])
        self.assertEqual(observed["plane"], "verification")

    def test_failure_feedback_is_bounded_and_actionable(self):
        contract = from_task("Repair the application and run its focused test.")
        packet = contract.synthesize_failure_feedback(
            "run_command", {"command": ["node", "probe.cjs"]},
            "Exit code: 1\nTypeError: bad value\n" + ("x" * 5000),
        )
        self.assertLessEqual(len(packet), 2200)
        self.assertIn("Next repair focus", packet)
        self.assertIn("one concrete mutation", packet)

    def test_failure_feedback_replaces_zero_test_runner_with_explicit_assertion(self):
        contract = from_task("Repair the function and run the supplied test.")
        packet = contract.synthesize_failure_feedback(
            "run_command",
            {"command": ["python", "-m", "unittest", "test_metrics"]},
            "Exit code: 5\nRan 0 tests in 0.000s\nNO TESTS RAN",
        )
        self.assertIn("did not execute any assertions", packet)
        self.assertIn("test function directly", packet)

    def test_task_contract_does_not_extract_url_host_as_endpoint(self):
        contract = from_task(
            "Build a WebSocket server at ws://localhost:8080 and run a local client smoke test."
        )
        self.assertEqual(contract.endpoints, ())

    def test_web_validation_accepts_observed_client_exchange(self):
        contract = from_task(
            "Build a WebSocket server and run a real local client smoke test."
        )
        accepted, *_ = contract.assess(
            "run_command",
            {"command": ["node", "probe.cjs"]},
            "Exit code: 0\nreceived pong and message; assertion passed\n",
        )
        self.assertTrue(accepted)

    def test_failed_service_status_is_not_validation_evidence(self):
        contract = from_task("Run a real local WebSocket server/client smoke test.")
        accepted, *_ = contract.assess(
            "process_status", {}, "EXITED code=1\nEADDRINUSE: address already in use"
        )
        self.assertFalse(accepted)

    def test_optional_tool_path_defaults_to_active_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)

            def identify(path="."):
                return path

            wrapped = _wrap_with_confinement(identify, ["path"])
            self.assertEqual(wrapped(), str(Path(tmp).resolve()))

    def test_patch_file_restores_uniform_outer_indentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            path = Path(tmp) / "module.py"
            path.write_text("def run():\n    value = 1\n    return value\n", encoding="utf-8")
            result = patch_file("module.py", "value = 1", "value = 2")
            self.assertIn("successfully", result)
            self.assertIn("    value = 2", path.read_text(encoding="utf-8"))

    def test_python_syntax_rejection_includes_generic_generator_hint(self):
        valid, message = validate_python_syntax(
            "example.py", "values = sorted(x for x in items, key=str.lower)"
        )
        self.assertFalse(valid)
        self.assertIn("wrap the generator expression", message)

    def test_run_tests_reloads_changed_project_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "test_target.py").write_text(
                "import unittest\nimport target\n\n"
                "class Contract(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(target.VALUE, 1)\n",
                encoding="utf-8",
            )

            self.assertTrue(run_tests(tmp)[0])
            (root / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
            (root / "test_target.py").write_text(
                "import unittest\nimport target\n\n"
                "class Contract(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(target.VALUE, 2)\n",
                encoding="utf-8",
            )
            self.assertTrue(run_tests(tmp)[0])

    def test_dispatch_blocks_command_plane_mutation_with_specific_reason(self):
        class Call:
            class Function:
                name = "run_command"
                arguments = {"command": ["bash", "-c", "cat > index.html <<'EOF'\nnew\nEOF"]}
            function = Function()
        key = ("run_command", json.dumps(Call.Function.arguments, sort_keys=True, separators=(",", ":")))
        messages = dispatch_tool_calls(
            [Call()], {"run_command": lambda **_: "should not run"},
            blocked_command_calls={key},
            blocked_command_reasons={key: "REJECTED: validation plane mutation"},
        )
        self.assertEqual(messages[0]["content"], "REJECTED: validation plane mutation")

    def test_command_classifier_catches_shell_write_forms(self):
        self.assertEqual(
            action_governor.classify("run_command", {"command": "bash -c 'cat > app.js'"}),
            "MUTATE",
        )
        self.assertEqual(
            action_governor.classify("run_command", {"command": ["tee", "app.js"]}),
            "MUTATE",
        )
        self.assertEqual(
            action_governor.classify("run_command", {"command": ["python3", "-c", "open('x','w').write('x')"]}),
            "MUTATE",
        )

    def test_run_tests_falls_back_to_function_style_pytest(self):
        """The generic runner must cover pytest-style modules without editing them."""
        if __import__("importlib.util").util.find_spec("pytest") is None:
            self.skipTest("pytest is not installed in this environment")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_function_style.py").write_text(
                "def test_value():\n    assert 2 + 2 == 4\n",
                encoding="utf-8",
            )
            success, summary = run_tests(tmp)
            self.assertTrue(success)
            self.assertIn("pytest passed", summary)

    def test_run_tests_preserves_assertion_diff_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_diff.py").write_text(
                "import unittest\n\n"
                "class Contract(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual({'actual': 3}, {'expected': 33})\n",
                encoding="utf-8",
            )
            success, summary = run_tests(tmp)
            self.assertFalse(success)
            self.assertIn("AssertionError", summary)
            self.assertIn("actual", summary)
            self.assertIn("expected", summary)

    def test_intervention_context_keeps_foundation_and_recent_tail(self):
        messages = [{"role": "system", "content": "foundation"},
                    {"role": "user", "content": "task"}]
        messages.extend({"role": "tool", "content": f"turn-{i}"} for i in range(20))
        compacted = _intervention_messages(messages, tail=4)
        self.assertEqual(compacted[0]["content"], "foundation")
        self.assertEqual(compacted[1]["content"], "task")
        self.assertIn("intervention context reduction", compacted[2]["content"])
        self.assertEqual([m["content"] for m in compacted[-4:]], ["turn-16", "turn-17", "turn-18", "turn-19"])

    def test_context_worker_parses_and_bounds_judgment(self):
        fallback = WorkerJudgment()
        result = _parse_judgment(
            '{"phase":"mutate","new_facts":["x"],"relevant_facts":[],'
            '"duplicate_action":false,"stagnating":false,"recommended_action":"patch_file",'
            '"blocker":"needs edit","target":"a.py","confidence":0.91}', fallback)
        self.assertEqual(result.phase, "mutate")
        self.assertEqual(result.recommended_action, "patch_file")
        self.assertEqual(result.target, "a.py")
        self.assertEqual(result.source, "4b")
        self.assertEqual(_parse_judgment("not json", fallback).source, "fallback")

    def test_context_worker_parses_structured_repair_checkpoint(self):
        result = _parse_judgment(
            '{"phase":"repair","recommended_action":"validate",'
            '"failure_class":"setup","next_action":"run_command",'
            '"diagnosis":"runner unavailable","preserve_files":["app.py"],'
            '"confidence":0.94}', WorkerJudgment()
        )
        self.assertEqual(result.failure_class, "setup")
        self.assertEqual(result.next_action, "run_command")
        self.assertEqual(result.preserve_files, ["app.py"])
        self.assertEqual(result.source, "4b")

    def test_repair_checkpoint_has_deterministic_setup_fallback(self):
        context = NoveltyContext(chat_fn=lambda **kwargs: _FakeResponse("{}"), worker_interval=100)
        context.request_repair_checkpoint(
            3, "repair", "NO TESTS RAN; pytest is unavailable",
            legal_actions=("run_command", "patch_file"), protected_paths=("test_metrics.py",),
        )
        rendered = context.render_for_model(action_critic=True)
        context.close()
        self.assertIn("Failure class: setup", rendered)
        self.assertIn("run_command", rendered)
        self.assertIn("Preserve", rendered)

    def test_synchronous_triage_gate_returns_bounded_restrictions(self):
        def fake_chat(**kwargs):
            return _FakeResponse(
                '{"phase":"repair","recommended_action":"validate",'
                '"failure_class":"setup","next_action":"run_command",'
                '"diagnosis":"runner unavailable","confidence":0.95}'
            )
        context = NoveltyContext(chat_fn=fake_chat, worker_interval=100)
        judgment = context.synchronous_triage(
            4, "repair", "pytest unavailable; no tests discovered",
            legal_actions=("run_command", "patch_file"),
        )
        self.assertEqual(judgment.failure_class, "setup")
        self.assertEqual(context.consume_gate_restrictions(), {"write_file", "patch_file"})
        self.assertEqual(context.consume_gate_restrictions(), set())
        context.close()

    def test_synchronous_triage_cannot_override_deterministic_setup_class(self):
        def hallucinating_chat(**kwargs):
            return _FakeResponse(
                '{"phase":"repair","recommended_action":"patch_file",'
                '"failure_class":"progress","next_action":"patch_file",'
                '"diagnosis":"add a pytest decorator","confidence":0.99}'
            )
        context = NoveltyContext(chat_fn=hallucinating_chat, worker_interval=100)
        judgment = context.synchronous_triage(
            4, "repair", "No module named pytest; no tests discovered",
            legal_actions=("run_command", "patch_file"),
        )
        self.assertEqual(judgment.failure_class, "setup")
        self.assertEqual(context.consume_gate_restrictions(), {"write_file", "patch_file"})
        context.close()

    def test_synchronous_triage_classifies_zero_test_summary_as_setup(self):
        context = NoveltyContext(
            chat_fn=lambda **kwargs: _FakeResponse(
                '{"failure_class":"behavior","next_action":"patch_file","confidence":0.99}'
            ),
            worker_interval=100,
        )
        judgment = context.synchronous_triage(
            4, "repair", "(False, 'Ran 0 tests: no tests discovered')",
            legal_actions=("run_command", "patch_file"),
        )
        self.assertEqual(judgment.failure_class, "setup")
        self.assertEqual(judgment.next_action, "run_command")
        context.close()

    def test_dependency_install_without_behavioral_evidence_is_setup(self):
        context = NoveltyContext(
            chat_fn=lambda **kwargs: _FakeResponse(
                '{"phase":"repair","recommended_action":"patch_file",'
                '"failure_class":"behavior","next_action":"patch_file",'
                '"confidence":0.99}'
            ),
            worker_interval=100,
        )
        judgment = context.synchronous_triage(
            5,
            "repair",
            "Failed probe: npm install\nExit code: 0\n"
            "added 1 package, and audited 2 packages\n"
            "No behavioral assertion was executed.",
            legal_actions=("patch_file", "run_command"),
        )
        self.assertEqual(judgment.failure_class, "setup")
        self.assertEqual(judgment.next_action, "run_command")
        self.assertEqual(context.consume_gate_restrictions(), {"write_file", "patch_file"})
        context.close()

    def test_structured_checkpoint_survives_newer_stale_event(self):
        context = NoveltyContext(chat_fn=lambda **kwargs: _FakeResponse("{}"), worker_interval=100)
        context.last_judgment = WorkerJudgment(
            event_id=1, diagnosis="runner unavailable", failure_class="setup",
            next_action="run_command", confidence=0.9, source="4b",
        )
        context.observe(2, "read_file", {}, "newer ordinary event", validation=False)
        rendered = context.render_for_model(action_critic=True)
        context.close()
        self.assertIn("Failure class: setup", rendered)
        self.assertIn("run_command", rendered)

    def test_context_worker_is_async_and_has_local_fallback(self):
        calls = []

        def fake_chat(**kwargs):
            calls.append(kwargs)
            return _FakeResponse(
                '{"phase":"verify","new_facts":["tests ran"],"relevant_facts":[],'
                '"duplicate_action":false,"stagnating":false,"recommended_action":"validate",'
                '"confidence":0.8}'
            )

        context = NoveltyContext(chat_fn=fake_chat, worker_interval=1)
        context.observe(1, "read_file", {"path": "a.py"}, "def f(): pass")
        judgment = context.collect(wait=True)
        context.observe(2, "patch_file", {"path": "a.py"}, "Wrote a.py", mutation=True)
        context.collect(wait=True)
        metrics = context.metrics()
        context.close()
        self.assertEqual(judgment.source, "4b")
        self.assertEqual(metrics["events"], 2)
        self.assertEqual(metrics["mutations"], 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("num_ctx", calls[0]["options"])

    def test_context_worker_failure_does_not_block(self):
        def failing_chat(**kwargs):
            raise RuntimeError("offline")

        context = NoveltyContext(chat_fn=failing_chat, worker_interval=1)
        context.observe(1, "read_file", {}, "content")
        judgment = context.collect(wait=True)
        metrics = context.metrics()
        context.close()
        self.assertEqual(judgment.source, "fallback")
        self.assertEqual(metrics["events"], 1)
        self.assertEqual(metrics["worker_failures"], 1)

    def test_context_worker_waits_for_repeated_error_signal(self):
        calls = []

        def fake_chat(**kwargs):
            calls.append(kwargs)
            return _FakeResponse('{"phase":"repair","new_facts":[],"relevant_facts":[],'
                                 '"duplicate_action":true,"stagnating":true,'
                                 '"recommended_action":"patch_file","confidence":0.9}')

        context = NoveltyContext(chat_fn=fake_chat, worker_interval=100)
        context.observe(1, "list_dir", {"path": "missing"}, "ERROR: missing")
        context.collect(wait=False)
        self.assertEqual(calls, [])
        context.observe(2, "list_dir", {"path": "missing"}, "ERROR: missing")
        judgment = context.collect(wait=True)
        context.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(judgment.source, "4b")

    def test_stagnation_judgment_becomes_actionable_prompt_signal(self):
        context = NoveltyContext(chat_fn=lambda **kwargs: _FakeResponse("{}"))
        context.last_judgment = WorkerJudgment(
            phase="mutate", stagnating=True, recommended_action="patch_file", source="4b"
        )
        rendered = context.render_for_model()
        context.close()
        self.assertIn("repeated or non-progress actions detected", rendered)
        self.assertIn("patch_file", rendered)

    def test_action_critic_emits_concrete_directive_only_when_enabled(self):
        context = NoveltyContext(chat_fn=lambda **kwargs: _FakeResponse("{}"))
        context.last_judgment = WorkerJudgment(
            phase="mutate", stagnating=True, recommended_action="patch_file",
            blocker="no mutation yet", target="src/a.py", source="4b", confidence=0.9,
        )
        plain = context.render_for_model()
        critic = context.render_for_model(action_critic=True)
        context.close()
        self.assertNotIn("Action critic directive", plain)
        self.assertIn("Action critic directive", critic)
        self.assertIn("patch_file", critic)
        self.assertIn("src/a.py", critic)

    def test_action_critic_has_deterministic_trigger_without_worker_result(self):
        context = NoveltyContext(
            chat_fn=lambda **kwargs: _FakeResponse("{}"),
            worker_interval=100, action_after_events=3,
        )
        for iteration in range(1, 4):
            context.observe(iteration, "read_file", {"path": "src/a.py"}, "same evidence")
        rendered = context.render_for_model(action_critic=True)
        context.close()
        self.assertIn("Action critic directive", rendered)
        self.assertIn("patch_file", rendered)

    def test_repeated_failure_gets_deterministic_recovery_signal(self):
        context = NoveltyContext(chat_fn=lambda **kwargs: _FakeResponse("{}"), worker_interval=100)
        context.observe(1, "list_dir", {"path": "sy"}, "ERROR: missing")
        context.observe(2, "list_dir", {"path": "sy"}, "ERROR: missing")
        rendered = context.render_for_model()
        context.close()
        self.assertIn("Do not repeat that call or argument", rendered)

    def test_repeated_failure_blocks_exact_call_at_dispatch(self):
        class Call:
            class Function:
                name = "list_dir"
                arguments = {"path": "missing"}
            function = Function()

        context = NoveltyContext(chat_fn=lambda **kwargs: _FakeResponse("{}"), worker_interval=100)
        context.observe(1, "list_dir", {"path": "missing"}, "ERROR: missing")
        context.observe(2, "list_dir", {"path": "missing"}, "ERROR: missing")
        messages = dispatch_tool_calls(
            [Call()], {"list_dir": lambda **kwargs: "should not run"},
            blocked_calls=context.blocked_calls(),
        )
        context.close()
        self.assertTrue(messages[0]["content"].startswith("REJECTED: repeated failing call"))
        self.assertIn("Do not retry it", messages[0]["content"])

    def test_action_gate_requires_progress_after_observation_window(self):
        # Recovery reads stay available during the orientation window and
        # close once the action threshold is reached.
        context = NoveltyContext(chat_fn=lambda **kwargs: _FakeResponse("{}"), action_after_events=3)
        for iteration in range(1, 4):
            context.observe(iteration, "read_file", {"path": f"f{iteration}.py"}, "content")
        self.assertTrue(context.requires_progress())
        context.observe(4, "find_files", {"pattern": "*.py"}, "a.py")
        context.observe(5, "find_files", {"pattern": "*.py"}, "a.py")
        self.assertFalse(context.recovery_reads_allowed())
        for iteration in range(6, 12):
            context.observe(iteration, "find_files", {"pattern": "*.py"}, "a.py")
        self.assertFalse(context.recovery_reads_allowed())
        context.observe(12, "patch_file", {"path": "f.py"}, "Wrote f.py", mutation=True)
        self.assertFalse(context.requires_progress())
        context.close()

    def test_structured_results_are_compact(self):
        self.assertEqual(_format_result([("file", "a.py"), ("dir", "src")]), "file\ta.py\ndir\tsrc")
        result = _format_result([(str(i),) for i in range(205)])
        self.assertIn("truncated 5 additional entries", result)

    def test_completion_requires_edit_and_validation(self):
        self.assertFalse(_completion_ready([], "code_change")[0])
        edited = [{"role": "tool", "tool_name": "patch_file", "content": "patched"}]
        self.assertFalse(_completion_ready(edited, "code_change")[0])
        edited.append({"role": "tool", "tool_name": "run_command", "content": "Exit code: 0\nSTDOUT:"})
        self.assertTrue(_completion_ready(edited, "code_change")[0])

    def test_verification_only_task_can_finish_without_mutation(self):
        plan = from_task("Run the existing test suite and verify it passes.", "code_change")
        evidence = [{"role": "tool", "tool_name": "run_command", "content": "Exit code: 0\nOK"}]
        ready, reason = _completion_ready(
            evidence,
            "code_change",
            plan,
            validation_evidence={"run_command|test suite|OK"},
        )
        self.assertTrue(ready, reason)

    def test_find_files_is_bounded_and_skips_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / ".git").mkdir()
            (root / "src" / "a.py").write_text("x")
            (root / ".git" / "hidden.py").write_text("x")
            set_root(tmp)
            self.assertEqual(find_files("*.py"), "src/a.py")

    def test_run_command_uses_argv_and_confined_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            result = run_command(["python3", "-c", "print('ok')"])
            self.assertIn("Exit code: 0", result)
            self.assertIn("ok", result)
        self.assertTrue(run_command(["python3", "-c", "print('ok')"], cwd="..").startswith("ERROR:"))

    def test_run_command_normalizes_missing_python_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            result = run_command(["python", "-c", "print('portable')"])
            self.assertIn("Exit code: 0", result)
            self.assertIn("portable", result)

    def test_validation_rejects_silent_direct_test_module(self):
        contract = from_task("Run test_metrics.py and verify the implementation.", "code_change")
        accepted, reason, suggestion, *_ = contract.assess(
            "run_command",
            {"command": ["python3", "test_metrics.py"]},
            "Exit code: 0\nSTDOUT:\n\nSTDERR:\n",
        )
        self.assertFalse(accepted)
        self.assertIn("no test evidence", reason)
        self.assertIn("test function", suggestion)

    def test_silent_test_evidence_is_a_setup_failure(self):
        self.assertTrue(_is_validation_setup_failure(
            "ERROR: test module 'test_metrics.py' produced no test evidence"
        ))
        self.assertTrue(_is_validation_setup_failure(
            "the command passed but does not show an assertion or behavioral probe"
        ))
        self.assertFalse(_is_validation_setup_failure("AssertionError: wrong total"))

    def test_validation_rejects_zero_test_runner_success(self):
        contract = from_task("Run the existing test suite and verify it passes.", "code_change")
        accepted, reason, *_ = contract.assess(
            "run_command",
            {"command": ["python3", "-m", "unittest.main"]},
            "Exit code: 0\nSTDOUT:\n\nSTDERR:\nRuntimeWarning: no tests discovered\n",
        )
        self.assertFalse(accepted)
        self.assertIn("zero tests", reason)
        accepted, *_ = contract.assess(
            "run_command",
            {"command": ["python3", "test_metrics.py"]},
            "Exit code: 0\nSTDOUT:\n\nSTDERR:\ntest_calculation ... ok\n",
        )
        self.assertTrue(accepted)

    def test_failure_diagnostic_extracts_assertion_diff(self):
        diagnostic = _failure_diagnostic(
            "AssertionError: values differ\n"
            "- 'taxed_total': 3.0\n"
            "+ 'taxed_total': 33.0\n"
        )
        self.assertIn("taxed_total", diagnostic)
        self.assertIn("3.0", diagnostic)
        self.assertIn("33.0", diagnostic)

    def test_failure_packet_calls_out_behavioral_mismatch(self):
        packet = from_task("repair the implementation").failure_packet(
            "run_command",
            {"command": "python -m unittest"},
            "AssertionError: actual {'total': 3.0} != expected {'total': 33.0}",
        )
        self.assertIn("behavioral contract", packet)

    def test_provider_refusal_is_terminal(self):
        self.assertTrue(_terminal_provider_error(ConnectionRefusedError(61, "Connection refused")))
        self.assertTrue(_terminal_provider_error(
            RuntimeError("RemoteDisconnected: Remote end closed connection without response")
        ))
        self.assertFalse(_terminal_provider_error(RuntimeError("temporary malformed response")))

    def test_missing_tokenize_endpoint_is_cached_per_provider(self):
        base_url = "http://token-test:8080/v1"
        root = "http://token-test:8080"
        _TOKENIZE_UNAVAILABLE_BASE_URLS.discard(root)
        error = urllib.error.HTTPError(
            root + "/tokenize", 404, "not found", {}, None
        )
        try:
            with patch("agent._context_window_tokens", return_value=16_384), \
                 patch("agent._llama_token_count", side_effect=error) as measure:
                _fit_llama_prompt(base_url, [], 256, 1)
                self.assertEqual(measure.call_count, 1)
            with patch("agent._llama_token_count") as measure:
                _fit_llama_prompt(base_url, [], 256, 1)
                measure.assert_not_called()
        finally:
            _TOKENIZE_UNAVAILABLE_BASE_URLS.discard(root)

    def test_run_command_rejects_silent_test_module_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            Path(tmp, "test_empty.py").write_text("def test_example():\n    assert True\n")
            result = run_command(["python3", "test_empty.py"])
            self.assertTrue(result.startswith("ERROR:"))
            self.assertIn("no test evidence", result)


if __name__ == "__main__":
    unittest.main()
