"""Append-only JSONL event log — one writer per run.

agent.py's loop is single-threaded and one RunStore per process, so no
file locking is needed. Every append is fsync'd before returning: a crash
right after append() must not lose the event that was just reported as
recorded.
"""

import json
import os

from memory.schema import make_event_record, new_event_id, validate_event_record


def event_seq(event_id: str) -> int:
    """The numeric ordinal inside an event_id ('evt-000042' -> 42) — event
    IDs are sequential per run, so this is what controller/ modules compare
    against to answer "did anything happen after event X" without needing
    timestamps or a separate ordering scheme."""
    return int(event_id.split("-")[1])


def read_events(run_dir: str):
    """Yield event records in write order. A line that fails to parse or
    validate is reported via a synthetic 'corrupt_event' record instead of
    raising — one bad line must not make the rest of a real run
    unreconstructable."""
    path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                validate_event_record(record)
            except (json.JSONDecodeError, ValueError) as e:
                yield {"event_type": "corrupt_event", "line_number": line_number, "error": str(e), "raw": line}
                continue
            yield record


class EventWriter:
    def __init__(self, run_dir: str, run_id: str):
        self.run_dir = run_dir
        self.run_id = run_id
        self.path = os.path.join(run_dir, "events.jsonl")
        self._seq = 0
        self._last_event_id = None
        # Resume an interrupted run's numbering/chain instead of restarting
        # it — required so a process that dies mid-run and gets re-invoked
        # against the same run_dir doesn't silently reuse event_ids or
        # break parent_event_id continuity.
        for record in read_events(run_dir):
            if record.get("event_type") == "corrupt_event":
                continue
            self._seq += 1
            self._last_event_id = record["event_id"]

    def append(self, event_type, payload, iteration=None, artifact_id=None, prompt_preview=None,
               input_tokens=None, output_tokens=None, latency_ms=None):
        self._seq += 1
        event_id = new_event_id(self._seq)
        record = make_event_record(
            event_id=event_id, run_id=self.run_id, parent_event_id=self._last_event_id,
            iteration=iteration, event_type=event_type, payload=payload, artifact_id=artifact_id,
            prompt_preview=prompt_preview, input_tokens=input_tokens, output_tokens=output_tokens,
            latency_ms=latency_ms,
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._last_event_id = event_id
        return record
