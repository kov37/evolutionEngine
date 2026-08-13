import tempfile
import unittest
from pathlib import Path

from agent import _completion_ready
from dispatch import _format_result, dispatch_tool_calls
from kernel.discovery import find_files
from kernel.exec_tools import run_command
from kernel.sandbox import set_root
from novelty_context import NoveltyContext, WorkerJudgment, _parse_judgment


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeResponse:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class KernelToolTests(unittest.TestCase):
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
        context = NoveltyContext(chat_fn=lambda **kwargs: _FakeResponse("{}"), action_after_events=3)
        for iteration in range(1, 4):
            context.observe(iteration, "read_file", {"path": f"f{iteration}.py"}, "content")
        self.assertTrue(context.requires_progress())
        context.observe(4, "find_files", {"pattern": "*.py"}, "a.py")
        context.observe(5, "find_files", {"pattern": "*.py"}, "a.py")
        self.assertFalse(context.recovery_reads_allowed())
        context.observe(6, "patch_file", {"path": "f.py"}, "Wrote f.py", mutation=True)
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


if __name__ == "__main__":
    unittest.main()
