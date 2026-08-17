"""Deterministic checks for AgentRunState's subsystem construction.

See agent_run_state.py's module docstring (plan Step E) for why these ten
subsystem handles live on one object instead of as separate agent.py loop
locals.
"""

import tempfile
import unittest
from pathlib import Path

from agent_run_state import AgentRunState
from kernel.sandbox import set_root


class AgentRunStateTests(unittest.TestCase):
    def _create(self, **overrides):
        kwargs = dict(
            task="fix the bug",
            task_type="code_change",
            working_memory_enabled=False,
            structured_summary_enabled=False,
            working_state_enabled=False,
            novelty_context_enabled=False,
            novelty_worker_model="qwen3.5:4b",
            governed=False,
        )
        kwargs.update(overrides)
        return AgentRunState.create(**kwargs)

    def test_ungoverned_run_leaves_the_progress_governor_quartet_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            state = self._create()
        self.assertIsNone(state.ledger)
        self.assertIsNone(state.escalation)
        self.assertIsNone(state.risk)
        self.assertIsNone(state.transaction)
        # contract and lifecycle are unconditional — every run has an
        # acceptance contract and an FSM, governed or not.
        self.assertIsNotNone(state.contract)
        self.assertIsNotNone(state.lifecycle)

    def test_governed_run_constructs_the_progress_governor_quartet(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            state = self._create(governed=True)
        self.assertIsNotNone(state.ledger)
        self.assertIsNotNone(state.escalation)
        self.assertIsNotNone(state.risk)
        self.assertIsNotNone(state.transaction)
        self.assertEqual(state.ledger.history, [])

    def test_optional_summary_modes_are_off_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            state = self._create()
        self.assertIsNone(state.working_memory)
        self.assertIsNone(state.structured_state)
        self.assertIsNone(state.working_state)
        self.assertIsNone(state.novelty)

    def test_each_enabled_flag_constructs_only_its_own_subsystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            state = self._create(working_memory_enabled=True)
        self.assertIsNotNone(state.working_memory)
        self.assertIsNone(state.structured_state)

        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            state = self._create(structured_summary_enabled=True)
        self.assertIsNotNone(state.structured_state)
        self.assertIsNone(state.working_memory)

        with tempfile.TemporaryDirectory() as tmp:
            set_root(tmp)
            state = self._create(novelty_context_enabled=True)
        self.assertIsNotNone(state.novelty)
        self.assertIsNone(state.working_memory)


if __name__ == "__main__":
    unittest.main()
