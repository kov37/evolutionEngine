import threading
import time
import unittest

from novelty_context import NoveltyContext, WorkerConfig


class _Response:
    class message:
        content = '{"phase":"localize","recommended_action":"inspect","confidence":0.8}'


class NoveltyContextTests(unittest.TestCase):
    def test_worker_judgment_is_tagged_and_pending_event_is_coalesced(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def chat_fn(**kwargs):
            calls.append(kwargs["messages"][0]["content"])
            started.set()
            release.wait(timeout=2)
            return _Response()

        ctx = NoveltyContext(config=WorkerConfig(interval=1), chat_fn=chat_fn)
        try:
            ctx.observe(1, "read_file", {"path": "a.py"}, "one")
            self.assertTrue(started.wait(timeout=1))
            ctx.observe(2, "read_file", {"path": "b.py"}, "two")
            self.assertEqual(ctx.metrics()["coalesced_events"], 1)

            release.set()
            deadline = time.monotonic() + 3
            while len(calls) < 2 and time.monotonic() < deadline:
                ctx.collect(wait=False)
                time.sleep(0.01)
            ctx.collect(wait=False)

            self.assertEqual(len(calls), 2)
            self.assertEqual(ctx.last_judgment.event_id, 2)
            self.assertEqual(ctx.metrics()["judgment_event_id"], 2)
        finally:
            release.set()
            ctx.close()

    def test_stale_judgment_is_marked_in_model_context(self):
        ctx = NoveltyContext(config=WorkerConfig(interval=99), chat_fn=lambda **_: _Response())
        try:
            ctx.observe(1, "read_file", {"path": "a.py"}, "one")
            ctx.last_judgment.event_id = 1
            ctx.observe(2, "read_file", {"path": "b.py"}, "two")
            rendered = ctx.render_for_model(action_critic=True)
            self.assertIn("latest event is 2", rendered)
            self.assertGreaterEqual(ctx.metrics()["stale_judgments"], 1)
        finally:
            ctx.close()


if __name__ == "__main__":
    unittest.main()
