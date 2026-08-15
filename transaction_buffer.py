"""Host-controlled state for bounded multi-file product mutations.

This module deliberately contains no model calls and no filesystem writes.  It
records which accepted product files are part of an unverified change and
provides a short control-plane message for the next actor turn.  The existing
RiskLayer remains responsible for per-file checkpoints and narrow safety
rollbacks; this buffer only decides whether an intermediate validation failure
belongs to a still-live multi-file change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test(?:s)?[_-]|.*[_-]test\.(?:py|js|jsx|ts|tsx|java|go|rb|rs|php))",
    re.IGNORECASE,
)
_HELPER_PARTS = {".agentic", "__pycache__", ".pytest_cache"}
_DEPENDENCY_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
    "pipfile.lock", "requirements.txt", "pyproject.toml", "package.json",
}


def normalize_product_path(root: str | Path, path: str) -> str | None:
    """Return a safe root-relative product path, or ``None`` for non-product data."""
    raw = str(path or "").replace("\\", "/")
    if not raw or raw.startswith("/"):
        return None
    candidate = Path(raw)
    if any(part in {"", ".", ".."} for part in candidate.parts):
        # ``.`` is harmless but rejecting it keeps the recorded identity
        # canonical and avoids treating a directory mutation as a file.
        return None
    normalized = candidate.as_posix()
    if any(part.lower() in _HELPER_PARTS for part in candidate.parts):
        return None
    if _TEST_NAME.search("/" + normalized):
        return None
    if candidate.name.lower() in _DEPENDENCY_NAMES:
        return None
    return normalized


@dataclass(frozen=True)
class TransactionDecision:
    action: str
    reason: str
    turns_remaining: int


class TransactionBuffer:
    """A finite, host-only buffer for a coherent multi-file edit.

    ``followup_turns`` counts repair opportunities after a failed validation.
    The default is one: preserve the first accepted product edit and give the
    actor one bounded turn to align another file.  Expiration does not itself
    rewrite the workspace; the caller can enter FSM recovery with RiskLayer's
    checkpoint still available.
    """

    def __init__(self, root: str | Path, *, followup_turns: int = 1):
        self.root = Path(root).resolve()
        self.followup_turn_limit = max(0, int(followup_turns))
        # Private by design: no tool receives this object or its internals.
        self._unverified_transaction_files: set[str] = set()
        self._active = False
        self._expired = False
        self._checkpoint_id: int | None = None
        self._followup_turns_remaining = 0
        self._consecutive_failures = 0
        self._last_failure = ""
        self._decisions = 0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def expired(self) -> bool:
        return self._expired

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(sorted(self._unverified_transaction_files))

    def record_mutation(self, path: str, *, checkpoint_id: int | None = None) -> bool:
        """Record an accepted product mutation; return whether it was tracked."""
        normalized = normalize_product_path(self.root, path)
        if normalized is None:
            return False
        if not self._active:
            self._active = True
            self._expired = False
            self._followup_turns_remaining = self.followup_turn_limit
            self._consecutive_failures = 0
        self._unverified_transaction_files.add(normalized)
        if self._checkpoint_id is None:
            self._checkpoint_id = checkpoint_id
        return True

    def note_validation_failed(self, failure: str) -> TransactionDecision:
        """Classify the newest failure without changing workspace contents."""
        self._last_failure = self._compact_failure(failure)
        self._consecutive_failures += 1
        if not self._active:
            decision = TransactionDecision("ordinary_repair", "no active product transaction", 0)
        elif self._followup_turns_remaining > 0 and self._consecutive_failures < 2:
            self._followup_turns_remaining -= 1
            decision = TransactionDecision(
                "preserve",
                "bounded multi-file transaction remains open",
                self._followup_turns_remaining,
            )
        else:
            self._expired = True
            self._active = False
            decision = TransactionDecision(
                "recover",
                "transaction window expired; preserve checkpoint for host recovery",
                0,
            )
        self._decisions += 1
        return decision

    def note_validation_passed(self) -> bool:
        """Close and clear the buffer after authoritative validation succeeds."""
        had_transaction = bool(self._unverified_transaction_files)
        self.clear()
        return had_transaction

    def clear(self) -> None:
        self._unverified_transaction_files.clear()
        self._active = False
        self._expired = False
        self._checkpoint_id = None
        self._followup_turns_remaining = 0
        self._consecutive_failures = 0
        self._last_failure = ""

    def control_block(self) -> str:
        """Return a bounded, model-facing status block with no hidden state."""
        if not self._unverified_transaction_files:
            return ""
        state = "ACTIVE" if self._active else "EXPIRED"
        files = ", ".join(self.files)
        failure = self._last_failure or "(latest validation failure is in the repair packet)"
        remaining = self._followup_turns_remaining
        return (
            "[HARNESS TRANSACTION STATUS]\n"
            f"State: {state}\n"
            f"Unverified product files: {files}\n"
            f"Current validation blocker: {failure}\n"
            f"Remaining transaction repair turns: {remaining}\n"
            "Preserve accepted product edits. Align the remaining product files; do not edit supplied "
            "tests or temporary validation helpers. This status is host-controlled."
        )

    def metrics(self) -> dict:
        return {
            "active": self._active,
            "expired": self._expired,
            "files": list(self.files),
            "checkpoint_id": self._checkpoint_id,
            "followup_turns_remaining": self._followup_turns_remaining,
            "consecutive_failures": self._consecutive_failures,
            "decisions": self._decisions,
        }

    @staticmethod
    def _compact_failure(failure: str, limit: int = 320) -> str:
        lines = [" ".join(line.split()) for line in str(failure or "").splitlines() if line.strip()]
        if not lines:
            return "(no diagnostic output)"
        # Keep exception/assertion lines preferentially; avoid pinning a full
        # pytest transcript into the actor context.
        priority = [line for line in lines if any(token in line for token in (
            "Error", "ERROR", "Assertion", "assert", "FAILED", "failed",
        ))]
        selected = priority[:2] or lines[:2]
        return " | ".join(selected)[:limit]
