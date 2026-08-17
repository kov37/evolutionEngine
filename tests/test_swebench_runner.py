"""Deterministic checks for the SWE-bench instance runner."""

import tempfile
import unittest
from pathlib import Path

from swebench_runner import _clone_tree, _runnable_labels


class SwebenchRunnerTests(unittest.TestCase):
    def test_clone_tree_produces_an_independent_equal_copy(self):
        # Whether the platform gives us APFS copy-on-write clones or a
        # fallback real copy, the contract is the same: equal contents and
        # edits on one side must never leak to the other. This matters
        # because the grader applies a test patch to its copy.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("value = 1\n", encoding="utf-8")
            (source / "pkg").mkdir()
            (source / "pkg" / "b.py").write_text("x = [1, 2]\n", encoding="utf-8")

            clone = root / "clone"
            _clone_tree(source, clone)

            self.assertEqual(
                (clone / "a.py").read_text(encoding="utf-8"), "value = 1\n"
            )
            self.assertEqual(
                (clone / "pkg" / "b.py").read_text(encoding="utf-8"), "x = [1, 2]\n"
            )

            (clone / "a.py").write_text("value = 999\n", encoding="utf-8")
            self.assertEqual(
                (source / "a.py").read_text(encoding="utf-8"), "value = 1\n"
            )

    def test_runnable_labels_map_unittest_names_and_skip_docstring_fragments(self):
        entries = [
            "test_render_required_attributes "
            "(forms_tests.field_tests.test_multivaluefield.MultiValueFieldTest)",
            "Test when the first widget's data has changed.",
            "If insufficient data is provided, None is substituted.",
            "test_bad_choice (forms_tests.field_tests.test_multivaluefield.MultiValueFieldTest)",
        ]
        self.assertEqual(_runnable_labels(entries), [
            "forms_tests.field_tests.test_multivaluefield.MultiValueFieldTest."
            "test_render_required_attributes",
            "forms_tests.field_tests.test_multivaluefield.MultiValueFieldTest."
            "test_bad_choice",
        ])
        self.assertEqual(_runnable_labels([]), [])


if __name__ == "__main__":
    unittest.main()
