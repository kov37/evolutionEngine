import unittest

from run_monitor import MonitorState, consume_event, render_dashboard


class RunMonitorTests(unittest.TestCase):
    def test_consume_event_extracts_progress_and_tool_metadata(self):
        state = MonitorState()
        display = consume_event(
            {
                "elapsed_s": 2.5,
                "kind": "iteration",
                "text": "🌀 [Iteration 2/8] Calling model...",
            },
            state,
        )
        consume_event(
            {
                "elapsed_s": 3.0,
                "kind": "tool_call",
                "text": "🔧 write_file({'path': 'cache.py'})",
            },
            state,
        )
        self.assertEqual(display, "iteration 2")
        self.assertEqual(state.iterations, 2)
        self.assertEqual(state.iteration_budget, 8)
        self.assertEqual(state.tool_calls, 1)
        self.assertIn("cache.py", state.last_event)

    def test_consume_event_extracts_json_metrics(self):
        state = MonitorState()
        consume_event(
            {
                "elapsed_s": 9,
                "kind": "novelty_metrics",
                "text": (
                    '🧬 [novelty metrics] '
                    '{"mutations": 2, "worker_calls": 3, "advice_successful": 1}'
                ),
            },
            state,
        )
        consume_event(
            {
                "elapsed_s": 9,
                "kind": "repair_metrics",
                "text": (
                    '🧰 [repair metrics] '
                    '{"repair_turns": 2, "lifecycle": {"state": "repair"}}'
                ),
            },
            state,
        )
        self.assertEqual(state.novelty["mutations"], 2)
        self.assertEqual(state.novelty["worker_calls"], 3)
        self.assertEqual(state.repair["repair_turns"], 2)
        self.assertEqual(state.repair["lifecycle"]["state"], "repair")

    def test_dashboard_explains_running_state_without_result(self):
        state = MonitorState(iterations=1, iteration_budget=12, tool_calls=2)
        text = render_dashboard(
            path=None,
            state=state,
            result={},
            task="lru_cache",
            condition="novelty",
        )
        self.assertIn("lru_cache / novelty", text)
        self.assertIn("iteration: 1/12", text)
        self.assertIn("tools:", text)
        self.assertIn("RUNNING", text)

    def test_dashboard_rejects_shadow_failure_even_when_visible_grader_passes(self):
        text = render_dashboard(
            path=None,
            state=MonitorState(),
            result={"passed": False, "grader": {"status": "PASS"}},
            task="lru_cache",
            condition="novelty",
        )
        self.assertIn("status: FAIL", text)


if __name__ == "__main__":
    unittest.main()
