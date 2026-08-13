import tempfile
import unittest
from pathlib import Path

from agent import _completion_ready
from dispatch import _format_result
from kernel.discovery import find_files
from kernel.exec_tools import run_command
from kernel.sandbox import set_root


class KernelToolTests(unittest.TestCase):
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
