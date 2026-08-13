import tempfile
import unittest
from pathlib import Path

from agent import _completion_ready
from dispatch import _format_result
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
            '"confidence":0.91}', fallback)
        self.assertEqual(result.phase, "mutate")
        self.assertEqual(result.recommended_action, "patch_file")
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
