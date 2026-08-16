"""Cheap model-free adversarial checks for the agent control plane.

These cases are deliberately about representations and evidence boundaries,
not about one application. They replay the classes of tool output that have
previously fooled the live actor loop without starting a model or a service.
"""

import unittest
import tempfile
from pathlib import Path

from independent_grader import run_python_grader

import action_governor
from agentic_benchmark import (
    TASKS,
    _check_protected_paths,
    _baseline_contract_valid,
    _grader_strength_valid,
    _post_task_evidence,
    _protected_task_paths,
    _run_shadow_evidence,
    _scorecard_passed,
    _snapshot_protected_paths,
    _apply_shadow_result,
    _write_setup,
    _verifier_repair_prompt,
)
from lifecycle_fsm import InvalidTransition, LifecycleFSM, LifecycleState
from lifecycle_policy import is_inspection_command, is_output_only_command
from risk_layer import RiskLayer
from transaction_buffer import TransactionBuffer
from validation_contract import from_task
from workspace.run_tests_tool import run_tests


class AdversarialPreflightTests(unittest.TestCase):
    def test_frozen_tasks_prove_fail_to_pass_precondition_before_model_call(self):
        for name in ("cascading_loop", "multi_file_transaction"):
            with self.subTest(task=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = TASKS[name]
                _write_setup(root, task.setup)
                valid, evidence = _baseline_contract_valid(task, root)
                self.assertTrue(valid)
                self.assertTrue(evidence["fail_to_pass"]["valid"])
                self.assertEqual(evidence["fail_to_pass"]["expected"], "FAIL")

    def test_pass_to_pass_evidence_is_checked_after_acceptance(self):
        task = type("Task", (), {
            "baseline": None,
            "pass_to_pass": "assert open('stable.txt', encoding='utf-8').read() == 'ok\\n'\n",
            "grade": "assert True\n",
        })()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stable.txt").write_text("ok\n", encoding="utf-8")
            valid, evidence = _baseline_contract_valid(task, root)
            self.assertTrue(valid)
            self.assertIn("pass_to_pass_before", evidence)
            self.assertTrue(_post_task_evidence(task, root, evidence))
            self.assertTrue(evidence["pass_to_pass_after"]["valid"])

    def test_hidden_shadow_evidence_is_run_but_not_exposed_to_the_prompt(self):
        shadow_source = (
            "from app.inventory import low_stock\n"
            "items = [{'name':'a','quantity':4},{'name':'b','quantity':2},{'name':'c','quantity':6}]\n"
            "assert [x['name'] for x in low_stock(items, 6)] == ['b','a']\n"
        )
        task = type("Task", (), {
            "prompt": "Add app.inventory.low_stock and run a focused check.",
            "setup": {
                "app/__init__.py": "",
                "app/inventory.py": (
                    "def low_stock(items, threshold):\n"
                    "    return sorted((x for x in items if x['quantity'] < threshold),\n"
                    "                  key=lambda x: x['quantity'])\n"
                ),
            },
            "grade": "from app.inventory import low_stock\nassert True\n",
            "baseline": None,
            "pass_to_pass": None,
            "shadow": shadow_source,
        })()
        self.assertNotIn(shadow_source, task.prompt)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_setup(root, task.setup)
            result = _run_shadow_evidence(task, root)
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["phase"], "shadow_acceptance")

    def test_hidden_shadow_failure_is_not_converted_to_visible_detail(self):
        task = type("Task", (), {"shadow": "raise AssertionError('shadow-specific detail')\n"})()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = _run_shadow_evidence(task, root)
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "FAIL")
        passed, detail = _apply_shadow_result(True, result)
        self.assertFalse(passed)
        self.assertEqual(detail, "hidden acceptance evidence failed")
        self.assertNotIn("shadow-specific detail", detail)

    def test_hidden_shadow_does_not_change_visible_acceptance_failure(self):
        shadow_result = {"status": "FAIL", "detail": "hidden detail"}
        passed, detail = _apply_shadow_result(False, shadow_result)
        self.assertFalse(passed)
        self.assertIsNone(detail)

    def test_grader_strength_mutations_reject_plausible_wrong_behavior(self):
        for name, task in TASKS.items():
            if not task.strength_mutations:
                continue
            with self.subTest(task=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_setup(root, task.setup)
                valid, evidence = _grader_strength_valid(task, root)
                self.assertTrue(valid, evidence)
                for mutation in task.strength_mutations:
                    self.assertIn(mutation.name, evidence)
                    self.assertTrue(evidence[mutation.name]["valid"], evidence)

    def test_supplied_test_integrity_is_detected_outside_agent_loop(self):
        task = type("Task", (), {
            "setup": {
                "test_app.py": "assert True\n",
                "src/app.py": "value = 1\n",
                "docs/README.md": "notes\n",
            }
        })()
        self.assertEqual(_protected_task_paths(task), ("test_app.py",))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_path = root / "test_app.py"
            test_path.write_text("assert True\n", encoding="utf-8")
            snapshot = _snapshot_protected_paths(root, ("test_app.py",))
            self.assertEqual(_check_protected_paths(root, snapshot)[0], True)
            test_path.write_text("assert True\nassert False\n", encoding="utf-8")
            ok, detail = _check_protected_paths(root, snapshot)
            self.assertFalse(ok)
            self.assertIn("test_app.py", detail)

    def test_independent_grader_source_stays_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target.py").write_text("value = 41\n", encoding="utf-8")
            result = run_python_grader(
                "from target import value\nassert value + 1 == 42\n",
                root,
            )
            self.assertTrue(result.passed)
            self.assertEqual(result.status, "PASS")
            self.assertFalse((root / ".agentic_grader.py").exists())
            self.assertEqual(len(result.checker_sha256), 64)

    def test_independent_grader_distinguishes_failure_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failed = run_python_grader("raise AssertionError('wrong behavior')\n", root)
            self.assertFalse(failed.passed)
            self.assertEqual(failed.status, "FAIL")
            timed_out = run_python_grader("import time; time.sleep(2)\n", root, timeout_seconds=0.05)
            self.assertFalse(timed_out.passed)
            self.assertEqual(timed_out.status, "TIMEOUT")

    def test_independent_grader_reports_invalid_environment_before_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_python_grader(
                "raise AssertionError('acceptance must not run')\n",
                root,
                preflight_source="raise RuntimeError('missing dependency')\n",
            )
            self.assertFalse(result.passed)
            self.assertEqual(result.status, "ENVIRONMENT_INVALID")
            self.assertIn("preflight failed", result.detail)

    def test_external_verifier_feedback_is_preserved_for_one_repair_pass(self):
        task = type("Task", (), {"prompt": "Repair the application."})()
        prompt = _verifier_repair_prompt(task, "AssertionError: required artifact is stale")
        self.assertIn("Independent verifier feedback", prompt)
        self.assertIn("required artifact is stale", prompt)
        self.assertIn("do not invoke or rewrite its generated grader", prompt)
        self.assertIn("call finish_task promptly", prompt)
        self.assertIn("do not invoke", prompt)

    def test_readback_wrappers_are_inspection(self):
        commands = [
            ["cat", "index.html"],
            ["bash", "-c", "cat index.html"],
            ["python3", "-c", "print(open('index.html').read())"],
            ["node", "-e", "console.log(require('fs').readFileSync('index.html','utf8'))"],
            ["node", "--version"],
            "sed -n '1,20p' index.html",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(is_inspection_command(command))

    def test_behavioral_and_compound_commands_remain_available(self):
        commands = [
            ["node", "probe.cjs"],
            ["python3", "-m", "unittest", "test_app.py"],
            ["npm", "install"],
            ["bash", "-c", "npm install && node probe.cjs"],
            "python3 -m pytest test_app.py 2>&1 | head -40",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(is_inspection_command(command))

    def test_shell_mutation_matrix_is_classified(self):
        commands = [
            ["bash", "-c", "cat > index.html <<'EOF'\nx\nEOF"],
            ["tee", "index.html"],
            ["python3", "-c", "open('index.html','w').write('x')"],
            "sed -i 's/http/ws/' index.html",
            "cp repaired.html index.html",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    action_governor.classify("run_command", {"command": command}),
                    "MUTATE",
                )

    def test_read_classifier_never_calls_obvious_writes_inspection(self):
        commands = [
            ["sed", "-i", "s/http/ws/", "index.html"],
            ["cat", ">", "copy.html"],
            ["bash", "-c", "sed -i 's/http/ws/' index.html"],
            ["bash", "-c", "cat index.html > copy.html"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(is_inspection_command(command))

    def test_diagnostic_redirects_are_not_file_mutations(self):
        commands = [
            ["cat", "index.html", "2>/dev/null"],
            ["cat", "index.html", "2>", "/dev/null"],
            ["bash", "-c", "cat index.html 2>/dev/null"],
            ["bash", "-c", "cat index.html >&2"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(is_inspection_command(command))
                self.assertNotEqual(
                    action_governor.classify("run_command", {"command": command}),
                    "MUTATE",
                )

    def test_common_interpreter_writes_are_mutations_case_insensitively(self):
        commands = [
            ["node", "-e", "require('fs').writeFileSync('index.html','x')"],
            ["node", "-e", "require('fs').appendFileSync('index.html','x')"],
            ["perl", "-pi", "-e", "s/a/b/", "index.html"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    action_governor.classify("run_command", {"command": command}),
                    "MUTATE",
                )

    def test_interpreter_reads_are_not_mutations(self):
        command = ["node", "-e", "console.log(require('fs').readFileSync('index.html','utf8'))"]
        self.assertTrue(is_inspection_command(command))
        self.assertEqual(
            action_governor.classify("run_command", {"command": command}),
            "OBSERVE",
        )

    def test_http_urlopen_is_not_mistaken_for_file_open(self):
        command = [
            "python3", "-c",
            "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8765'); print(r.read())",
        ]
        self.assertFalse(is_inspection_command(command))
        self.assertEqual(
            action_governor.classify("run_command", {"command": command}),
            "VALIDATE",
        )

    def test_dynamic_interpreter_writes_are_mutations(self):
        commands = [
            ["node", "-e", "fs.promises.writeFile('index.html','x')"],
            ["node", "-e", "require('fs')['writeFileSync']('index.html','x')"],
            ["python3", "-c", "Path('index.html').write_text('x')"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    action_governor.classify("run_command", {"command": command}),
                    "MUTATE",
                )

    def test_inline_assertion_is_validation_but_printed_assertion_is_not(self):
        real_check = ["python3", "-c", "assert 2 > 1"]
        fake_check = ["python3", "-c", "print('assert passed')"]
        self.assertFalse(is_output_only_command(real_check))
        self.assertEqual(
            action_governor.classify("run_command", {"command": real_check}),
            "VALIDATE",
        )
        self.assertTrue(is_output_only_command(fake_check))
        self.assertEqual(
            action_governor.classify("run_command", {"command": fake_check}),
            "OBSERVE",
        )
        contract = from_task("Build a service and run a real behavioral check.")
        accepted, *_ = contract.assess(
            "run_command", {"command": fake_check},
            "Exit code: 0\nSTDOUT: assert passed\n",
        )
        self.assertFalse(accepted)

    def test_dependency_install_is_not_file_mutation(self):
        self.assertNotEqual(
            action_governor.classify("run_command", {"command": ["npm", "install", "ws"]}),
            "MUTATE",
        )
        self.assertEqual(
            action_governor.classify("run_command", {"command": ["install", "src", "dest"]}),
            "MUTATE",
        )

    def test_javascript_arrow_functions_are_not_shell_redirects(self):
        command = ["node", "-e", "setTimeout(() => console.log('passed'), 10)"]
        self.assertNotEqual(
            action_governor.classify("run_command", {"command": command}),
            "MUTATE",
        )

    def test_printable_arrow_is_not_shell_redirect(self):
        self.assertNotEqual(
            action_governor.classify(
                "run_command", {"command": ["python3", "-c", "print('PASS -> value')"]}
            ),
            "MUTATE",
        )

    def test_quoted_program_operators_are_not_shell_redirects(self):
        for command in (
            ["python3", "-c", "assert 2 > 1"],
            ["python3", "-c", "print('<div>ok</div>')"],
            ["node", "-e", "console.log('a > b')"],
        ):
            self.assertNotEqual(
                action_governor.classify("run_command", {"command": command}),
                "MUTATE",
            )
        self.assertEqual(
            action_governor.classify(
                "run_shell", {"command": "python3 -c \"print('ok')\" > result.txt"}
            ),
            "MUTATE",
        )
        self.assertEqual(
            action_governor.classify(
                "run_shell", {"command": "echo ok > result.txt"}
            ),
            "MUTATE",
        )

    def test_output_only_claims_cannot_be_validation_evidence(self):
        contract = from_task("Build a service and run a real behavioral check.")
        for command in (["echo", "assert passed"], ["printf", "connected\\n"]):
            with self.subTest(command=command):
                result = "Exit code: 0\\nSTDOUT: assert passed\\nSTDERR:\\n"
                accepted, *_ = contract.assess("run_command", {"command": command}, result)
                self.assertFalse(accepted)
                self.assertEqual(
                    action_governor.classify("run_command", {"command": command}),
                    "OBSERVE",
                )

    def test_file_text_cannot_fake_web_behavior(self):
        contract = from_task("Build a WebSocket server and run a real local client smoke test.")
        deceptive_outputs = [
            "Exit code: 0\nSTDOUT: const WebSocket = require('ws'); received message pong",
            "Exit code: 0\nSTDOUT: <script>new WebSocket('ws://localhost:8080')</script>",
            "Exit code: 0\nSTDOUT: package installed; websocket response handler present",
        ]
        commands = [
            ["cat", "index.html"],
            ["bash", "-c", "cat index.html"],
            ["node", "-e", "console.log(require('fs').readFileSync('index.html','utf8'))"],
        ]
        for command, output in zip(commands, deceptive_outputs):
            with self.subTest(command=command):
                accepted, *_ = contract.assess("run_command", {"command": command}, output)
                self.assertFalse(accepted)

    def test_real_client_evidence_is_accepted(self):
        contract = from_task("Build a WebSocket server and run a real local client smoke test.")
        accepted, *_ = contract.assess(
            "run_command",
            {"command": ["node", "probe.cjs"]},
            "Exit code: 0\nSTDOUT: connected; sent ping; received pong; assertion passed",
        )
        self.assertTrue(accepted)

    def test_data_report_json_success_is_not_web_only_evidence(self):
        contract = from_task(
            "Read sales.csv and write report.json containing total_sales, "
            "sales_by_region, and top_product. Use the Python standard library. "
            "Run a check against the supplied data before finishing."
        )
        self.assertFalse(contract.categories.intersection({"api", "web"}))
        output = (
            "Exit code: 0\n"
            "STDOUT:\n"
            '{"total_sales": 28.0, "sales_by_region": {"North": 16.0, "South": 12.0}, '
            '"top_product": "Book"}\n'
            "VALIDATION OK: {'total_sales': 28.0, 'sales_by_region': {'North': 16.0, "
            "'South': 12.0}, 'top_product': 'Book'}\n"
            "STDERR:\n"
        )
        accepted, reason, *_ = contract.assess(
            "run_command",
            {"command": ["python3", "report.py"]},
            output,
        )
        self.assertTrue(accepted, reason)

    def test_cascading_function_tests_report_the_current_failure(self):
        """A repair must reveal the next defect, not collapse into zero tests."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target.py").write_text(
                "def calculate(values):\n"
                "    total = sum(values\n"
                "    return total / '2'\n",
                encoding="utf-8",
            )
            (root / "test_target.py").write_text(
                "from target import calculate\n\n"
                "def test_calculate():\n"
                "    assert calculate([10, 20]) == 15\n",
                encoding="utf-8",
            )

            syntax_ok, syntax_summary = run_tests(tmp)
            self.assertFalse(syntax_ok)
            self.assertIn("SyntaxError", syntax_summary)
            self.assertIn("target.py", syntax_summary)

            (root / "target.py").write_text(
                "def calculate(values):\n"
                "    total = sum(values)\n"
                "    return total / '2'\n",
                encoding="utf-8",
            )
            type_ok, type_summary = run_tests(tmp)
            self.assertFalse(type_ok)
            self.assertIn("TypeError", type_summary)
            self.assertNotIn("no tests discovered", type_summary.lower())

            (root / "target.py").write_text(
                "def calculate(values):\n"
                "    total = sum(values)\n"
                "    return total / 2\n",
                encoding="utf-8",
            )
            passed, passed_summary = run_tests(tmp)
            self.assertTrue(passed)
            # The dependency-free fallback and an installed pytest are both
            # valid runners. The contract is that one assertion executed and
            # passed, not which available runner produced the summary.
            self.assertTrue(
                "1 function-style tests" in passed_summary
                or "pytest passed" in passed_summary
            )

    def test_multi_file_transaction_resilience(self):
        """A failed intermediate state must preserve the first product edit.

        This is a control-plane test, so the two mutations are deliberately
        explicit. The model-facing benchmark uses the same dependency shape
        but does not reveal these patches to the actor.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_a = root / "core_math.py"
            file_b = root / "matrix_solver.py"
            test_file = root / "test_solver.py"
            file_a.write_text(
                "class Symbol:\n"
                "    def __init__(self):\n"
                "        self.is_real = True\n",
                encoding="utf-8",
            )
            file_b.write_text(
                "from core_math import Symbol\n"
                "def solve(s):\n"
                "    return s.is_real\n",
                encoding="utf-8",
            )
            test_file.write_text(
                "from core_math import Symbol\n"
                "from matrix_solver import solve\n\n"
                "def test_solver_contract():\n"
                "    symbol = Symbol()\n"
                "    assert not hasattr(symbol, 'is_real')\n"
                "    assert getattr(symbol, 'is_symbolic', False) is True\n"
                "    assert solve(symbol) is True\n",
                encoding="utf-8",
            )

            fsm = LifecycleFSM()
            risk = RiskLayer()
            transaction = TransactionBuffer(root, followup_turns=1)
            fsm.transition("turn")

            first_version = (
                "class Symbol:\n"
                "    def __init__(self):\n"
                "        self.is_symbolic = True\n"
            )
            risk.checkpoint("core_math.py", str(file_a), turn=1)
            file_a.write_text(first_version, encoding="utf-8")
            self.assertTrue(transaction.record_mutation("core_math.py", checkpoint_id=1))
            fsm.transition("mutation")

            intermediate_ok, intermediate_summary = run_tests(tmp)
            self.assertFalse(intermediate_ok, intermediate_summary)
            self.assertIn("AttributeError", intermediate_summary)
            self.assertEqual(fsm.transition("validation_failed"), LifecycleState.REPAIR)
            self.assertEqual(file_a.read_text(encoding="utf-8"), first_version)
            decision = transaction.note_validation_failed(intermediate_summary)
            self.assertEqual(decision.action, "preserve")
            self.assertIn("core_math.py", transaction.control_block())

            final_b = (
                "from core_math import Symbol\n"
                "def solve(s):\n"
                "    return s.is_symbolic\n"
            )
            risk.checkpoint("matrix_solver.py", str(file_b), turn=2)
            file_b.write_text(final_b, encoding="utf-8")
            self.assertTrue(transaction.record_mutation("matrix_solver.py", checkpoint_id=2))
            fsm.transition("mutation")

            final_ok, final_summary = run_tests(tmp)
            self.assertTrue(final_ok, final_summary)
            self.assertTrue(transaction.note_validation_passed())
            self.assertFalse(transaction.active)
            self.assertEqual(transaction.files, ())
            self.assertEqual(fsm.transition("validation_passed"), LifecycleState.COMPLETE)

    def test_fsm_recovery_has_no_implicit_transition(self):
        fsm = LifecycleFSM()
        self.assertEqual(fsm.transition("turn"), LifecycleState.ACT)
        with self.assertRaises(InvalidTransition):
            fsm.transition("validation_passed")
        self.assertEqual(fsm.transition("orientation_stalled"), LifecycleState.RECOVER)
        self.assertEqual(fsm.transition("turn"), LifecycleState.RECOVER)
        self.assertEqual(fsm.transition("mutation"), LifecycleState.VALIDATE)

    def test_completion_truth_table(self):
        for artifact in (False, True):
            for finish in (False, True):
                for process in (False, True):
                    expected = artifact and finish and process
                    self.assertEqual(
                        _scorecard_passed(artifact, finish, process), expected,
                        (artifact, finish, process),
                    )


if __name__ == "__main__":
    unittest.main()
