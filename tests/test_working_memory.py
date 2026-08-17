"""Deterministic checks for the host-owned working memory board.

Everything here is mechanical: fingerprints, epochs, cycles, counters, and
the renderer's priority budget. No model calls, no task-specific knowledge.
"""

import tempfile
import unittest
from pathlib import Path

from working_memory import (
    BLIND_EDIT_WARNING,
    STAGNATION_CYCLES,
    WorkingMemory,
    build_working_memory,
    normalize_fingerprint,
    normalize_target,
    validation_event_target,
)


class WorkingMemoryTests(unittest.TestCase):
    def test_target_normalization_ignores_verbosity_flags(self):
        self.assertEqual(
            normalize_target(["python", "tests/runtests.py", "--verbosity", "2", "a.b"]),
            normalize_target(["python", "tests/runtests.py", "a.b"]),
        )
        self.assertEqual(
            normalize_target(["pytest", "-q", "test_x.py"]),
            normalize_target(["pytest", "test_x.py"]),
        )
        self.assertNotEqual(
            normalize_target(["pytest", "test_a.py"]),
            normalize_target(["pytest", "test_b.py"]),
        )

    def test_fingerprint_normalizes_numbers_paths_and_hex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "m.py").write_text("x = 1\n", encoding="utf-8")
            text = (
                f"File \"{root}/probe.py\", line 42, in check\n"
                "AssertionError: value 17 != expected 24\n"
            )
            fp = normalize_fingerprint(text, root)
            self.assertIsNotNone(fp)
            self.assertIn("value N != expected N", fp)
            self.assertNotIn("17", fp)
            self.assertNotIn(str(root), fp)

    def test_fingerprint_distinguishes_different_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp_a = normalize_fingerprint("AssertionError: value 1 != expected 2\n", root)
            fp_b = normalize_fingerprint("TypeError: unsupported operand\n", root)
            self.assertNotEqual(fp_a, fp_b)

    def test_fingerprint_returns_none_without_failure_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(normalize_fingerprint("Exit code: 0\nok\n", Path(tmp)))

    def test_fingerprint_falls_back_to_summary_error_label(self):
        # Delegated unittest summaries carry a traceback location but no
        # extracted exception line. Distinct labels must stay distinct
        # instead of collapsing into "Failure: traceback".
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.py").write_text("pass\n", encoding="utf-8")
            text_a = (
                f'File "{root}/x.py", line 1, in check\n'
                "ERROR setUpClass (a.b.C)\n"
            )
            text_b = (
                f'File "{root}/x.py", line 1, in check\n'
                "ERROR setUpClass (a.b.D)\n"
            )
            fp_a = normalize_fingerprint(text_a, root)
            fp_b = normalize_fingerprint(text_b, root)
            self.assertIn("setUpClass", fp_a)
            self.assertNotEqual(fp_a, fp_b)

    def test_cycles_unchanged_and_mutations_since_change(self):
        wm = WorkingMemory()
        wm.on_validation("pytest test_x.py", False, "F1")
        self.assertEqual(wm.state.epochs["pytest test_x.py"].fp_cycles_unchanged, 1)
        wm.on_mutation("a.py")
        wm.on_mutation("a.py")
        wm.on_validation("pytest test_x.py", False, "F1")
        epoch = wm.state.epochs["pytest test_x.py"]
        self.assertEqual(epoch.fp_cycles_unchanged, 2)
        self.assertEqual(epoch.mutations_since_change, 2)
        # A different fingerprint resets the unchanged counter and the
        # mutations-since-change window.
        wm.on_validation("pytest test_x.py", False, "F2")
        self.assertEqual(epoch.fp_cycles_unchanged, 1)
        self.assertEqual(epoch.mutations_since_change, 0)

    def test_edit_count_and_active_epoch_cycles_unchanged(self):
        # These two accessors replace what used to be agent.py reaching
        # directly into wm.state.mutations/wm.state.epochs — see
        # agent_run_state.py's module docstring (Step E).
        wm = WorkingMemory()
        self.assertEqual(wm.edit_count("a.py"), 0)
        self.assertEqual(wm.active_epoch_cycles_unchanged(), 0)
        wm.on_validation("t", False, "F1")
        self.assertEqual(wm.active_epoch_cycles_unchanged(), 1)
        wm.on_mutation("a.py")
        wm.on_mutation("a.py")
        self.assertEqual(wm.edit_count("a.py"), 2)
        self.assertEqual(wm.edit_count("b.py"), 0)
        wm.on_validation("t", False, "F1")
        self.assertEqual(wm.active_epoch_cycles_unchanged(), 2)

    def test_passing_validation_resolves_active_failure(self):
        wm = WorkingMemory()
        wm.on_validation("t", False, "F1")
        wm.on_validation("t", True, None)
        epoch = wm.state.epochs["t"]
        self.assertIsNone(epoch.current_fp)
        self.assertEqual(epoch.resolved, ["F1"])

    def test_goal_transitions_are_mechanical(self):
        wm = WorkingMemory()
        wm.on_validation("t", False, "F1")
        goal = wm.state.goals["acceptance"]
        self.assertEqual(goal.status, "failing")
        self.assertEqual(goal.last_transition, "unverified->failing")
        wm.on_validation("t", True, None)
        self.assertEqual(goal.status, "passing")
        self.assertEqual(goal.last_transition, "failing->passing")

    def test_stagnation_flag_and_events(self):
        wm = WorkingMemory()
        for _ in range(STAGNATION_CYCLES - 1):
            wm.on_validation("t", False, "F1")
        self.assertFalse(wm.stagnant())
        wm.on_validation("t", False, "F1")
        self.assertTrue(wm.stagnant())
        self.assertEqual(wm.state.stagnation_events, 1)
        # A new fingerprint clears stagnation.
        wm.on_validation("t", False, "F2")
        self.assertFalse(wm.stagnant())

    def test_blind_editing_counter(self):
        wm = WorkingMemory()
        wm.on_mutation("a.py")
        wm.on_mutation("b.py")
        self.assertTrue(wm.blind_editing())
        wm.on_validation("t", False, "F1")
        self.assertFalse(wm.blind_editing())
        self.assertEqual(wm.state.blind_edit_events, 1)

    def test_target_switch_preserves_history(self):
        wm = WorkingMemory()
        wm.on_validation("pytest a.py", False, "FA")
        wm.on_validation("pytest b.py", False, "FB")
        self.assertEqual(wm.state.target_switches, 1)
        self.assertIn("pytest a.py", wm.state.epochs)
        self.assertIn("pytest b.py", wm.state.epochs)
        # Switching back to an earlier target keeps its accumulated state.
        wm.on_validation("pytest a.py", False, "FA")
        self.assertEqual(wm.state.epochs["pytest a.py"].fp_cycles_unchanged, 2)

    def test_renderer_keeps_goals_under_a_tiny_budget(self):
        wm = WorkingMemory()
        wm.on_validation("target-with-a-very-long-name test_a.py", False, "F1")
        for index in range(12):
            wm.on_mutation(f"module_{index}.py")
        rendered = wm.render(budget_chars=80)
        self.assertLessEqual(len(rendered), 80)
        self.assertIn("goal acceptance", rendered)
        self.assertNotIn("edits:", rendered)  # lowest priority dropped first

    def test_renderer_uses_only_mechanical_claims(self):
        wm = WorkingMemory()
        wm.on_validation("t", False, "F1")
        wm.on_validation("t", True, None)
        rendered = wm.render()
        for banned in ("improved", "closer", "likely", "probably", "intent"):
            self.assertNotIn(banned, rendered)
        self.assertIn("failing->passing", rendered)

    def test_validation_event_target_is_normalized(self):
        self.assertEqual(
            validation_event_target("run_command", {
                "command": ["python", "tests/runtests.py", "--verbosity", "2", "a.b"],
            }),
            "python tests/runtests.py a.b",
        )
        self.assertEqual(
            validation_event_target("run_tests", {"path": "tests/forms"}),
            "run_tests:tests/forms",
        )

    def test_build_working_memory_derives_interface_goals(self):
        wm = build_working_memory(
            "Build a service with a GET /health endpoint and a POST /api/tasks "
            "endpoint; validate both."
        )
        self.assertIn("/health", wm.state.goals)
        self.assertIn("/api/tasks", wm.state.goals)
        self.assertNotIn("acceptance", wm.state.goals)

    def test_build_working_memory_defaults_to_acceptance_goal(self):
        wm = build_working_memory("Repair this repository and run the tests.")
        self.assertEqual(list(wm.state.goals), ["acceptance"])

    def test_reads_are_mechanical_and_relevance_is_token_exposure(self):
        wm = WorkingMemory()
        wm.on_validation("t", False, "AssertionError: required attribute missing")
        wm.on_read("a.py", "def helper(): return 1")
        wm.on_read("b.py", "widget.required = True")
        coverage = wm.state.coverage
        self.assertEqual(coverage["a.py"].reads, 1)
        self.assertEqual(coverage["a.py"].relevant_reads, 0)
        self.assertEqual(coverage["b.py"].reads, 1)
        self.assertEqual(coverage["b.py"].relevant_reads, 1)

    def test_localization_ranks_identifier_over_filename_and_skips_tests(self):
        wm = WorkingMemory()
        wm.on_validation("t", False, "AssertionError: widget required flag missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "widgets.py").write_text(
                "class MultiWidget:\n    required = True\n", encoding="utf-8"
            )
            (root / "fields.py").write_text(
                "class Field:\n    pass\n", encoding="utf-8"
            )
            (root / "widget_utils.py").write_text("x = 1\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_widget.py").write_text(
                "import widgets\n", encoding="utf-8"
            )
            candidates = wm.localization_candidates(str(root))
            paths = [path for path, _label, _count in candidates]
            self.assertIn("widgets.py", paths)
            self.assertIn("widget_utils.py", paths)  # filename rank
            self.assertNotIn("tests/test_widget.py", paths)
            # identifier rank sorts above filename rank
            widget_index = paths.index("widgets.py")
            utils_index = paths.index("widget_utils.py")
            self.assertLess(widget_index, utils_index)

    def test_uninspected_candidates_are_failure_scoped(self):
        wm = WorkingMemory()
        wm.on_validation("t", False, "AssertionError: widget required flag missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "widgets.py").write_text(
                "class MultiWidget:\n    required = True\n", encoding="utf-8"
            )
            (root / "other.py").write_text("x = 1\n", encoding="utf-8")
            uninspected = wm.uninspected_candidates(str(root))
            paths = [path for path, _label, _count in uninspected]
            self.assertIn("widgets.py", paths)
            wm.on_read("widgets.py", "class MultiWidget: required = True")
            uninspected = wm.uninspected_candidates(str(root))
            paths = [path for path, _label, _count in uninspected]
            self.assertNotIn("widgets.py", paths)

    def test_render_includes_stagnation_policy_and_candidates(self):
        wm = WorkingMemory()
        wm.on_validation("t", False, "AssertionError: widget required flag missing")
        for _ in range(STAGNATION_CYCLES):
            wm.on_validation("t", False, "AssertionError: widget required flag missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "widgets.py").write_text("required = True\n", encoding="utf-8")
            rendered = wm.render(project_root=str(root))
            self.assertIn("STAGNATION ACTION", rendered)
            self.assertIn("UNINSPECTED candidates", rendered)
            self.assertIn("widgets.py", rendered)
            # Prescriptive wording is banned: the host names candidates but
            # never commands an edit.
            for banned in ("edit widgets.py", "change widgets.py", "patch widgets.py"):
                self.assertNotIn(banned, rendered)


if __name__ == "__main__":
    unittest.main()
