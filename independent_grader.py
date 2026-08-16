"""Host-owned, independent acceptance checks for agent workspaces.

The actor's workspace contains the product and supplied tests.  This module
keeps the grader source outside that workspace, runs it in a fresh subprocess,
and returns an explicit outcome instead of collapsing every non-zero result
into a boolean.  The grader is evidence about the artifact; it is not part of
the actor's tool surface and must never be used as a model instruction.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


MAX_DETAIL_CHARS = 4000


@dataclass(frozen=True)
class GradeResult:
    """One independent check result with enough provenance to audit it."""

    status: str
    passed: bool
    detail: str
    returncode: int | None
    elapsed_seconds: float
    checker_sha256: str
    phase: str = "acceptance"

    def as_dict(self) -> dict:
        return asdict(self)


def _digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _bounded_detail(stdout: str, stderr: str) -> str:
    combined = (stdout or "") + (stderr or "")
    return combined.strip()[-MAX_DETAIL_CHARS:]


def _run_source(
    source: str,
    workspace: Path,
    *,
    timeout_seconds: float,
    phase: str,
    python_executable: str,
) -> GradeResult:
    """Run a Python checker whose source is never written into `workspace`."""
    started = time.monotonic()
    digest = _digest(source)
    workspace = workspace.resolve()

    # The temporary directory is host-owned and is not inside the candidate
    # workspace.  The checker still uses the candidate as cwd so relative
    # artifact paths behave exactly as they do in the real project.
    with tempfile.TemporaryDirectory(prefix="novelty-independent-grader-") as host_dir:
        checker_path = Path(host_dir) / "check.py"
        checker_path.write_text(source, encoding="utf-8")
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(workspace), existing_pythonpath) if item
        )
        env["NOVELTY_GRADER_PHASE"] = phase
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            completed = subprocess.run(
                [python_executable, str(checker_path)],
                cwd=str(workspace),
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            detail = _bounded_detail(
                getattr(exc, "stdout", "") or "",
                getattr(exc, "stderr", "") or "",
            ) or f"checker exceeded {timeout_seconds:.1f}s"
            return GradeResult(
                status="TIMEOUT",
                passed=False,
                detail=detail,
                returncode=None,
                elapsed_seconds=round(time.monotonic() - started, 3),
                checker_sha256=digest,
                phase=phase,
            )
        except OSError as exc:
            return GradeResult(
                status="ENVIRONMENT_INVALID",
                passed=False,
                detail=f"could not execute independent checker: {exc}",
                returncode=None,
                elapsed_seconds=round(time.monotonic() - started, 3),
                checker_sha256=digest,
                phase=phase,
            )

    return GradeResult(
        status="PASS" if completed.returncode == 0 else "FAIL",
        passed=completed.returncode == 0,
        detail=_bounded_detail(completed.stdout, completed.stderr),
        returncode=completed.returncode,
        elapsed_seconds=round(time.monotonic() - started, 3),
        checker_sha256=digest,
        phase=phase,
    )


def run_python_grader(
    source: str,
    workspace: Path,
    *,
    timeout_seconds: float = 45.0,
    phase: str = "acceptance",
    python_executable: str = sys.executable,
    preflight_source: str | None = None,
) -> GradeResult:
    """Run optional environment preflight, then the independent acceptance check.

    A failed preflight is deliberately not reported as a product failure.  It
    means the grader could not establish a trustworthy execution environment.
    """
    if preflight_source is not None:
        preflight = _run_source(
            preflight_source,
            workspace,
            timeout_seconds=timeout_seconds,
            phase="preflight",
            python_executable=python_executable,
        )
        if not preflight.passed:
            return GradeResult(
                status="ENVIRONMENT_INVALID",
                passed=False,
                detail="preflight failed: " + preflight.detail,
                returncode=preflight.returncode,
                elapsed_seconds=preflight.elapsed_seconds,
                checker_sha256=_digest(source),
                phase="preflight",
            )
    return _run_source(
        source,
        workspace,
        timeout_seconds=timeout_seconds,
        phase=phase,
        python_executable=python_executable,
    )
