"""Piece 7: risk management, separated from progress management per the
design principle — "the engine should not force reckless actions merely
because it detected stagnation... make consequential actions safer" so the
escalation governor (piece 6) can demand an attempt without demanding a
commitment nothing can undo.

Deliberately NOT git-based (no stash/commit side effects on the user's real
repository state) — plain in-memory content snapshots per file path, taken
right before a MUTATE call is dispatched. Simple, safe, and directly
testable without any filesystem/git risk of its own. A snapshot only ever
needs to survive one run in memory; nothing here is meant to persist past
process exit.
"""

import os


class RiskLayer:
    """Snapshots a file's content before a MUTATE call touches it, and can
    restore the most recent snapshot on demand. One instance per run."""

    def __init__(self):
        self.snapshots = {}  # path -> list of (turn, content_or_None) snapshots, most recent last

    def checkpoint(self, path: str, full_path: str, turn: int) -> None:
        """Record the CURRENT on-disk content of `path` before it's about
        to be mutated. content_or_None is None if the file doesn't exist
        yet (a new file being created — "rollback" for that case means
        deleting it, handled by rollback())."""
        content = None
        if os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
        self.snapshots.setdefault(path, []).append((turn, content))

    def rollback(self, path: str, full_path: str) -> bool:
        """Restore the most recent snapshot for `path`. Returns True if a
        rollback actually happened (a snapshot existed), False if there
        was nothing to roll back to. If the snapshot's content is None
        (the file didn't exist before its most recent mutation), rollback
        deletes the file instead of writing None to it."""
        history = self.snapshots.get(path)
        if not history:
            return False
        _, content = history[-1]
        if content is None:
            if os.path.exists(full_path):
                os.remove(full_path)
        else:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
        return True

    def has_checkpoint(self, path: str) -> bool:
        return bool(self.snapshots.get(path))


def _self_test() -> bool:
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="risk_layer_selftest_")
    try:
        layer = RiskLayer()
        target = os.path.join(tmp, "x.py")

        # --- Case 1: checkpoint + rollback an EXISTING file back to its
        # prior content.
        with open(target, "w", encoding="utf-8") as f:
            f.write("original content\n")
        layer.checkpoint("x.py", target, turn=1)
        assert layer.has_checkpoint("x.py") is True

        # Simulate a mutation happening.
        with open(target, "w", encoding="utf-8") as f:
            f.write("mutated content\n")
        assert open(target).read() == "mutated content\n"

        rolled_back = layer.rollback("x.py", target)
        assert rolled_back is True
        assert open(target).read() == "original content\n", "rollback must restore the exact prior content"

        # --- Case 2: checkpoint + rollback a file that didn't exist yet
        # (a genuinely new file created by write_file) — rollback must
        # delete it, not write empty/None content.
        new_file = os.path.join(tmp, "brand_new.py")
        layer.checkpoint("brand_new.py", new_file, turn=2)  # doesn't exist yet
        with open(new_file, "w", encoding="utf-8") as f:
            f.write("newly created content\n")
        assert os.path.exists(new_file)
        rolled_back2 = layer.rollback("brand_new.py", new_file)
        assert rolled_back2 is True
        assert not os.path.exists(new_file), "rollback of a file that didn't exist before must delete it"

        # --- Case 3: rollback with no prior checkpoint returns False, no
        # crash.
        layer2 = RiskLayer()
        assert layer2.rollback("never_touched.py", os.path.join(tmp, "never_touched.py")) is False

        # --- Case 4: multiple checkpoints for the same path — rollback
        # restores the MOST RECENT one, not the first.
        multi_target = os.path.join(tmp, "multi.py")
        with open(multi_target, "w", encoding="utf-8") as f:
            f.write("version 1\n")
        layer.checkpoint("multi.py", multi_target, turn=3)
        with open(multi_target, "w", encoding="utf-8") as f:
            f.write("version 2\n")
        layer.checkpoint("multi.py", multi_target, turn=4)
        with open(multi_target, "w", encoding="utf-8") as f:
            f.write("version 3 (broken)\n")
        layer.rollback("multi.py", multi_target)
        assert open(multi_target).read() == "version 2\n", "rollback must restore the MOST RECENT checkpoint, not the first"

        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import sys
    ok = _self_test()
    print("OK   risk_layer (RiskLayer: checkpoint/rollback, no git side effects)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
