"""Host-owned working memory for the agent loop.

The board exists so a small actor does not have to remember, by itself, what
it has already tried and what those attempts did. Every update is a
deterministic function of tool results and validation events; the model
never writes to this state. Checkpoint rebuilds re-render the board from
state instead of reconstructing it from messages, so context surgery cannot
erase it.

Design rules, from the working-memory spec review:

- Failure identity is a normalized content fingerprint scoped to a
  validation target (an experiment epoch). Locations are metadata, never
  identity.
- Two distinct stagnation signals are tracked: unchanged test cycles and
  blind mutations since the last validation.
- Board claims are mechanically observable only: no interpretation of
  intent or semantic progress ("improved", "closer", "likely fixed").
- The renderer is a token-budgeted priority view: unresolved goals and
  current failures are lossless; history compresses first.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from validation_packet import extract_first_failure

# Phase 2: localization search. Bounded, mechanical, model-agnostic.
_NOISE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", ".venv-django", "venv",
    "build", "dist", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    "htmlcov", "coverage",
}
_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".c", ".h", ".cpp", ".hpp",
    ".html", ".css", ".rs", ".go", ".java",
}
_STOP_TOKENS = {
    "assertionerror", "attributeerror", "typeerror", "valueerror", "keyerror",
    "importerror", "syntaxerror", "failure", "error", "traceback", "failed",
    "expected", "value", "values", "return", "returns", "none", "true", "false",
    "line", "file", "test", "tests", "found", "cannot", "could", "not", "the",
    "this", "that", "with", "from", "self", "module", "response", "exit",
    "code", "status",
}
_MAX_SEARCH_FILES = 400
_MAX_FILE_BYTES = 200_000


# ---------------------------------------------------------------- normalization

_VERBOSITY_FLAGS = re.compile(r"^--verbosity$|^--verbose$|^-v+$|^-q+$|^--quiet$")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_PATH = re.compile(r"(?:/[^\s'\"<>]+)+")
_WS = re.compile(r"\s+")


def normalize_target(command) -> str:
    """Identity of a validation experiment, insensitive to cosmetic flags.

    Two invocations of the same test target with different verbosity flags
    are the same experiment; a different target is a new epoch.
    """
    if isinstance(command, (list, tuple)):
        parts = [str(part) for part in command]
    else:
        parts = str(command or "").split()
    kept = []
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if _VERBOSITY_FLAGS.match(part):
            skip_next = part == "--verbosity"
            continue
        kept.append(part)
    return " ".join(kept).strip()


def normalize_fingerprint(result_content: str, project_root) -> str | None:
    """Content-normalized failure identity, stable under edits and data.

    Strips what changes while the failure stays the same: literal numbers,
    hex addresses, absolute paths, and whitespace. Keeps identifiers and
    operators so genuinely different failures stay different. Returns None
    when the output carries no actionable failure.
    """
    text = str(result_content or "")
    failure = extract_first_failure(text, project_root)
    if failure is None:
        return None
    if failure.error_type or failure.message:
        core = f"{failure.error_type or 'Failure'}: {failure.message or failure.kind}".strip()
    else:
        # Some runner summaries (delegated unittest collections) carry a
        # traceback location but no extracted exception line. Fall back to
        # the first summary-level error label so distinct failures stay
        # distinct instead of collapsing into "Failure: traceback".
        match = re.search(r"(?:ERROR|FAIL):?[^\n]{0,180}", text)
        if match:
            core = match.group(0).strip()
        else:
            match = re.search(r"\b(?:AssertionError|TypeError|ValueError|ImportError|AttributeError|KeyError|SyntaxError)[^\n]{0,180}", text)
            core = match.group(0).strip() if match else failure.kind
    core = _HEX.sub("HEX", core)
    core = _NUM.sub("N", core)
    core = _PATH.sub("PATH", core)
    core = _WS.sub(" ", core).strip()
    return core[:240]


# ---------------------------------------------------------------- state

@dataclass
class GoalEntry:
    name: str
    status: str = "unverified"  # unverified | failing | passing
    last_transition: str = ""
    cycles: int = 0  # mutation->test cycles since the last transition


@dataclass
class TargetEpoch:
    target: str
    cycles: int = 0
    current_fp: str | None = None
    fp_cycles_unchanged: int = 0
    mutations_since_change: int = 0
    failures: dict[str, int] = field(default_factory=dict)  # fp -> occurrences
    resolved: list[str] = field(default_factory=list)
    first_fp_cycle: int = 0


@dataclass
class FileCoverage:
    edits: int = 0
    reads: int = 0
    relevant_reads: int = 0  # reads whose content contained failure tokens


@dataclass
class WorkingMemoryState:
    goals: dict[str, GoalEntry] = field(default_factory=dict)
    active_target: str | None = None
    epochs: dict[str, TargetEpoch] = field(default_factory=dict)
    target_switches: int = 0
    mutations_since_last_validation: int = 0
    mutations: dict[str, int] = field(default_factory=dict)  # file -> count
    coverage: dict[str, FileCoverage] = field(default_factory=dict)
    stagnation_events: int = 0  # validation events while the failure was stagnant
    blind_edit_events: int = 0  # validation events preceded by unvalidated edits


STAGNATION_CYCLES = 3
BLIND_EDIT_WARNING = 2


class WorkingMemory:
    """Deterministic updater and renderer for the agent's state board."""

    def __init__(self, goal_names=()):
        self.state = WorkingMemoryState(
            goals={name: GoalEntry(name=name) for name in goal_names}
        )
        if not self.state.goals:
            self.state.goals["acceptance"] = GoalEntry(name="acceptance")

    # ----- event feed -------------------------------------------------

    def on_mutation(self, path: str | None) -> None:
        self.state.mutations_since_last_validation += 1
        if path:
            self.state.mutations[path] = self.state.mutations.get(path, 0) + 1
            coverage = self.state.coverage.setdefault(path, FileCoverage())
            coverage.edits += 1
        epoch = self._active_epoch()
        if epoch is not None:
            epoch.mutations_since_change += 1

    def on_read(self, path: str | None, content: str) -> None:
        """Record a host-observed file read (mechanical, never comprehension)."""
        if not path:
            return
        coverage = self.state.coverage.setdefault(path, FileCoverage())
        coverage.reads += 1
        tokens = self._failure_tokens()
        if tokens and any(token.lower() in content.lower() for token in tokens):
            # The implicated token text was placed in the model's context.
            # This claims exposure of evidence, not understanding.
            coverage.relevant_reads += 1

    def on_validation(self, target: str, passed: bool, fingerprint: str | None) -> None:
        if self.state.mutations_since_last_validation >= BLIND_EDIT_WARNING:
            self.state.blind_edit_events += 1
        self.state.mutations_since_last_validation = 0
        if not target:
            return
        if self.state.active_target is None or self.state.active_target != target:
            if self.state.active_target is not None:
                self.state.target_switches += 1
            self.state.active_target = target
        epoch = self.state.epochs.setdefault(target, TargetEpoch(target=target))
        epoch.cycles += 1
        if passed:
            if epoch.current_fp is not None:
                epoch.resolved.append(epoch.current_fp)
                epoch.current_fp = None
                epoch.fp_cycles_unchanged = 0
            self._set_goal(self._goal_for(target), "passing")
            return
        fp = fingerprint or "unparseable-failure"
        if epoch.current_fp == fp:
            epoch.fp_cycles_unchanged += 1
        else:
            epoch.current_fp = fp
            epoch.fp_cycles_unchanged = 1
            epoch.mutations_since_change = 0
            epoch.first_fp_cycle = epoch.cycles
        epoch.failures[fp] = epoch.failures.get(fp, 0) + 1
        if epoch.fp_cycles_unchanged >= STAGNATION_CYCLES:
            self.state.stagnation_events += 1
        self._set_goal(self._goal_for(target), "failing")

    # ----- goal bookkeeping -------------------------------------------

    def _goal_for(self, target: str) -> str:
        # Interface-derived goals associate mechanically: the interface
        # token must appear in the validation command itself.
        for name in self.state.goals:
            if name != "acceptance" and name in target:
                return name
        return "acceptance"

    def _set_goal(self, name: str, status: str) -> None:
        goal = self.state.goals.get(name)
        if goal is None:
            goal = self.state.goals[name] = GoalEntry(name=name)
        if goal.status == status:
            goal.cycles += 1
            return
        goal.last_transition = f"{goal.status}->{status}"
        goal.status = status
        goal.cycles = 0

    # ----- derived signals --------------------------------------------

    def _active_epoch(self) -> TargetEpoch | None:
        if self.state.active_target is None:
            return None
        return self.state.epochs.get(self.state.active_target)

    def stagnant(self) -> bool:
        epoch = self._active_epoch()
        return bool(
            epoch is not None
            and epoch.current_fp is not None
            and epoch.fp_cycles_unchanged >= STAGNATION_CYCLES
        )

    def edit_count(self, path: str) -> int:
        """How many mutations `path` has received so far this run."""
        return self.state.mutations.get(path, 0)

    def active_epoch_cycles_unchanged(self) -> int:
        """How many validation cycles the active target's failure fingerprint
        has been unchanged for. 0 if there is no active epoch."""
        epoch = self._active_epoch()
        return epoch.fp_cycles_unchanged if epoch is not None else 0

    def blind_editing(self) -> bool:
        return self.state.mutations_since_last_validation >= BLIND_EDIT_WARNING

    # ----- localization (Phase 2) ------------------------------------

    def _failure_tokens(self) -> list[str]:
        """Distinctive identifiers from the active failure fingerprint."""
        epoch = self._active_epoch()
        if epoch is None or epoch.current_fp is None:
            return []
        tokens = []
        for token in re.split(r"[^A-Za-z_]+", epoch.current_fp):
            if len(token) >= 4 and token.lower() not in _STOP_TOKENS:
                if token.lower() not in tokens:
                    tokens.append(token.lower())
        return tokens[:8]

    def localization_candidates(self, project_root: str, limit: int = 5) -> list[tuple[str, str, int]]:
        """Ranked uninspected/implicated files for the active failure.

        Returns ``(path, rank, occurrences)`` where rank is
        ``identifier`` (the token appears in the file body) or ``filename``
        (the token appears in the file name). Test-owned paths are excluded
        and the search is bounded. This is evidence, never a directive: the
        host does not tell the model what to edit.
        """
        tokens = self._failure_tokens()
        if not tokens or not project_root:
            return []
        root = Path(project_root)
        candidates: dict[str, tuple[int, int, str]] = {}
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames if name not in _NOISE_DIRS
            )
            for filename in filenames:
                if Path(filename).suffix.lower() not in _TEXT_EXTENSIONS:
                    continue
                if self._is_test_path(filename, dirpath):
                    continue
                full = Path(dirpath) / filename
                try:
                    if full.stat().st_size > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                scanned += 1
                if scanned > _MAX_SEARCH_FILES:
                    return self._rank_candidates(candidates, limit)
                try:
                    text = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                lowered = text.lower()
                name_lowered = filename.lower()
                for token in tokens:
                    occurrences = lowered.count(token)
                    if occurrences:
                        rank = 1
                        label = "identifier"
                        candidates[str(full.relative_to(root))] = (
                            rank, -occurrences, label,
                        )
                    elif token in name_lowered:
                        rank = 2
                        label = "filename"
                        existing = candidates.get(str(full.relative_to(root)))
                        if existing is None:
                            candidates[str(full.relative_to(root))] = (
                                rank, 0, label,
                            )
        return self._rank_candidates(candidates, limit)

    @staticmethod
    def _is_test_path(filename: str, dirpath: str) -> bool:
        lower = (dirpath + "/" + filename).lower()
        return (
            filename.lower().startswith("test")
            or "/tests/" in lower
            or "/test/" in lower
        )

    @staticmethod
    def _rank_candidates(candidates, limit: int) -> list[tuple[str, str, int]]:
        ranked = sorted(
            candidates.items(),
            key=lambda item: (item[1][0], item[1][1], item[0]),
        )
        return [
            (path, label, max(0, -occurrences))
            for path, (rank, occurrences, label) in ranked[:limit]
        ]

    def uninspected_candidates(self, project_root: str, limit: int = 3) -> list[tuple[str, str, int]]:
        """Failure-scoped candidate files the actor has never read."""
        return [
            candidate for candidate in self.localization_candidates(project_root, limit * 2)
            if self.state.coverage.get(candidate[0], FileCoverage()).reads == 0
        ][:limit]

    def metrics(self) -> dict:
        """Run-end telemetry for search-process evaluation."""
        return {
            "target_switches": self.state.target_switches,
            "targets": {
                target: {
                    "cycles": epoch.cycles,
                    "failures_seen": len(epoch.failures),
                    "resolved": len(epoch.resolved),
                }
                for target, epoch in self.state.epochs.items()
            },
            "stagnation_events": self.state.stagnation_events,
            "blind_edit_events": self.state.blind_edit_events,
            "mutations_total": sum(self.state.mutations.values()),
            "files_read": sum(1 for c in self.state.coverage.values() if c.reads > 0),
            "files_edited": len(self.state.mutations),
        }

    # ----- rendering ---------------------------------------------------

    def render(self, budget_chars: int = 900, project_root: str = "") -> str:
        """Render the board under a token budget, highest priority first."""
        blocks: list[str] = []

        # 1. unresolved goals (lossless)
        for goal in self.state.goals.values():
            line = f"goal {goal.name}: {goal.status}"
            if goal.last_transition:
                line += f" ({goal.last_transition})"
            blocks.append((0, line))

        # 2. current failure
        epoch = self._active_epoch()
        if epoch is not None and epoch.current_fp is not None:
            blocks.append((
                1,
                f"failure: {epoch.current_fp[:110]} | cycles_unchanged="
                f"{epoch.fp_cycles_unchanged} | mutations_since_change="
                f"{epoch.mutations_since_change}",
            ))

        # 3. stagnation warning
        if self.stagnant():
            blocks.append((
                2,
                f"STAGNATION: same failure {epoch.fp_cycles_unchanged} cycles",
            ))

        # 3.5 failure-scoped uninspected candidates (only when stagnant)
        if self.stagnant() and project_root:
            candidates = self.uninspected_candidates(project_root)
            if candidates:
                listing = ", ".join(
                    f"{path}[{label}]" for path, label, _count in candidates
                )
                blocks.append((
                    3,
                    f"UNINSPECTED candidates implicated by the failure tokens: {listing}",
                ))

        # 4. stagnation action policy
        if self.stagnant():
            blocks.append((
                3,
                "STAGNATION ACTION: before another edit to the already-edited file, "
                "inspect an uninspected candidate or run a search that could falsify "
                "the current localization.",
            ))

        # 5. target history
        if self.state.target_switches:
            history = ", ".join(
                f"{t[:40]}:{e.cycles}" for t, e in self.state.epochs.items()
            )
            blocks.append((4, f"targets: {history} (switched {self.state.target_switches}x)"))

        # 6. blind editing
        if self.blind_editing():
            blocks.append((
                5,
                f"edits since last validation: {self.state.mutations_since_last_validation}",
            ))

        # 7. coverage concentration (compresses first under budget pressure)
        if self.state.mutations:
            ranked = sorted(self.state.mutations.items(), key=lambda kv: -kv[1])
            top = ranked[0]
            total = sum(self.state.mutations.values())
            coverage = self.state.coverage.get(top[0], FileCoverage())
            blocks.append((
                6,
                f"edits: {top[0]} {top[1]}/{total} recent "
                f"(read {coverage.reads}x, relevant {coverage.relevant_reads}x)",
            ))

        # Budget: keep priority 0-5 lossless; drop from the lowest tier up.
        chosen: list[str] = []
        budget = budget_chars
        for priority in (0, 1, 2, 3, 4, 5, 6):
            for block_priority, line in blocks:
                if block_priority != priority:
                    continue
                if budget - len(line) - 1 < 0:
                    return "\n".join(chosen)
                chosen.append(line)
                budget -= len(line) + 1
        return "\n".join(chosen)


# ---------------------------------------------------------------- event wiring


def build_working_memory(task: str) -> WorkingMemory:
    """Create the board with goals derived from the task contract.

    Interface-oriented task wording produces one goal per interface; any
    other task shape gets the single mechanical ``acceptance`` goal.
    """
    try:
        from validation_contract import from_task
        contract = from_task(task)
        endpoints = list(getattr(contract, "endpoints", ()) or ())
    except Exception:
        endpoints = []
    return WorkingMemory(goal_names=endpoints or ())


def validation_event_target(tool_name: str, arguments: dict) -> str:
    """Normalized experiment identity for one validation tool call."""
    if tool_name == "run_tests":
        path = (arguments or {}).get("path") or "."
        return f"run_tests:{path}"
    command = (arguments or {}).get("command", "")
    normalized = normalize_target(command)
    return normalized or tool_name
