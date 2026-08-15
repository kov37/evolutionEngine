"""Cheap model-free adversarial checks for the agent control plane.

These cases are deliberately about representations and evidence boundaries,
not about one application. They replay the classes of tool output that have
previously fooled the live actor loop without starting a model or a service.
"""

import unittest
import tempfile
from pathlib import Path

import action_governor
from agentic_benchmark import _scorecard_passed, _verifier_repair_prompt
from lifecycle_fsm import InvalidTransition, LifecycleFSM, LifecycleState
from lifecycle_policy import is_inspection_command, is_output_only_command
from validation_contract import from_task
from workspace.run_tests_tool import run_tests


class AdversarialPreflightTests(unittest.TestCase):
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
            self.assertIn("1 function-style tests", passed_summary)

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
