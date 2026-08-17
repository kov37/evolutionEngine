"""Run and independently grade noveltyEngine on SWE-bench instances.

The agent never receives the official test patch.  The grader applies that
patch only to a separate copy after the agent exits, then runs the FAIL_TO_PASS
and PASS_TO_PASS tests.  This is intentionally lightweight and does not depend
on Docker; it is a local progress harness, not a replacement for official
SWE-bench evaluation.

Instances are declared in ``INSTANCES``.  Per-instance configuration covers
only harness concerns (source archive, interpreter environment, test command
shape); the problem statement, test patch, and test lists always come from the
dataset metadata so no task-specific answer ever enters the harness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from run_streaming import stream_agent


ROOT = Path(__file__).resolve().parent
DATA_URL = "https://huggingface.co/datasets/SWE-bench/SWE-bench/resolve/main/data/test-00000-of-00001.parquet"
DATA_PATH = ROOT / "assets" / "swebench-test.parquet"

SYMPY_COMPAT_COMMIT = "7b127bdf71a36d85216315f80c1b54d22b060818"
DJANGO_COMPAT_COMMIT = "db1fc5cd3c5d36cdb5d0fe4404efd6623dd3e8fb"


def _compatibility_shim_text() -> str:
    """Bridge the frozen Python-legacy checkout to the host interpreter."""
    return (
        "import collections, collections.abc\n"
        "for _name in ('Mapping', 'MutableMapping', 'MutableSet', 'Sequence', 'Iterable', 'Callable'):\n"
        "    if not hasattr(collections, _name): setattr(collections, _name, getattr(collections.abc, _name))\n"
        "try:\n"
        "    import setuptools._distutils as _distutils\n"
        "    import sys\n"
        "    sys.modules.setdefault('distutils', _distutils)\n"
        "    import setuptools._distutils.version as _version\n"
        "    sys.modules.setdefault('distutils.version', _version)\n"
        "except ImportError:\n"
        "    pass\n"
    )


def sympy_test_command(project: Path, instance: dict) -> list[str]:
    """SymPy #13878: pytest selection over the continuous-RV test module."""
    tests = " or ".join([
        "test_arcsin", "test_ContinuousDomain", "test_characteristic_function",
        "test_benini", "test_chi", "test_chi_noncentral", "test_chi_squared",
        "test_gompertz", "test_shiftedgompertz", "test_trapezoidal",
        "test_quadratic_u", "test_von_mises", "test_prefab_sampling",
        "test_input_value_assertions", "test_probability_unevaluated",
        "test_density_unevaluated", "test_random_parameters",
        "test_random_parameters_given", "test_conjugate_priors", "test_issue_10003",
    ])
    return [
        sys.executable, "-m", "pytest", "-q",
        "sympy/stats/tests/test_continuous_rv.py", "-k", tests,
    ]


def _runnable_labels(entries: list[str]) -> list[str]:
    """Map SWE-bench unittest-style names to runnable test labels.

    The dataset mixes real entries of the form
    ``method (module.Class)`` with docstring fragments. Only the former are
    runnable labels; anything else is skipped.
    """
    labels: list[str] = []
    for entry in entries:
        match = re.match(r"^([\w.]+) \(([\w.]+)\.(\w+)\)$", entry)
        if match:
            method, module, cls = match.group(1), match.group(2), match.group(3)
            labels.append(f"{module}.{cls}.{method}")
    return labels


def django_test_command(project: Path, instance: dict) -> list[str]:
    """Django: its own test runner over the FAIL_TO_PASS/PASS_TO_PASS labels."""
    tests = _runnable_labels(
        json.loads(instance["FAIL_TO_PASS"]) + json.loads(instance["PASS_TO_PASS"])
    )
    if not tests:
        raise RuntimeError("no runnable test labels parsed from instance metadata")
    return ["tests/runtests.py", "--verbosity", "2", *tests]


@dataclass(frozen=True)
class InstanceConfig:
    """Harness configuration for one SWE-bench instance."""

    repo: str
    base_commit: str
    base_dir: Path
    # Builds the grader argv list (no interpreter prefix; added by the runner).
    test_command: object
    # Interpreter used for agent-launched commands and grading. None means
    # the runner's own interpreter.
    interpreter: Path | None = None
    # Python source written into the grader copy as sitecustomize.py.
    sitecustomize: str | None = None
    # Extra agent.py flags beyond the shared command line.
    extra_agent_args: tuple[str, ...] = ()
    grade_timeout: float = 120.0
    # How the actor should run the repository's own tests. This is harness
    # environment metadata (the runner must know it to grade), never an
    # answer: it names the test entry point, not the tests to satisfy.
    test_entry_hint: str | None = None


INSTANCES = {
    "sympy__sympy-13878": InstanceConfig(
        repo="sympy/sympy",
        base_commit=SYMPY_COMPAT_COMMIT,
        base_dir=ROOT / "assets" / "benchmarks" / "sympy-13878",
        test_command=sympy_test_command,
        sitecustomize=_compatibility_shim_text(),
        extra_agent_args=(
            "--distribution-target-file", "sympy/stats/crv_types.py",
            "--distribution-names",
            "Arcsin,Dagum,Erlang,Frechet,Gamma,GammaInverse,Kumaraswamy,Laplace,"
            "Logistic,Nakagami,StudentT,UniformSum",
        ),
    ),
    "django__django-14034": InstanceConfig(
        repo="django/django",
        base_commit=DJANGO_COMPAT_COMMIT,
        base_dir=ROOT / "assets" / "benchmarks" / "django-14034",
        test_command=django_test_command,
        interpreter=ROOT / "assets" / "benchmarks" / "django-14034" / ".venv-django" / "bin" / "python",
        grade_timeout=180.0,
        test_entry_hint="python tests/runtests.py --verbosity 2 <test.label.path>",
    ),
}


def _load_instance(instance_id: str) -> dict:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit(
            "pyarrow is required. Create .venv-swebench and install pyarrow, "
            "or use the existing .venv-swebench/bin/python."
        ) from exc
    if not DATA_PATH.exists():
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(DATA_URL, DATA_PATH)
    table = pq.read_table(DATA_PATH, filters=[("instance_id", "=", instance_id)])
    rows = table.to_pylist()
    if len(rows) != 1:
        raise RuntimeError(f"expected one {instance_id} instance, found {len(rows)}")
    return rows[0]


def _prepare_base(cfg: InstanceConfig) -> None:
    if cfg.base_dir.exists():
        return
    cfg.base_dir.parent.mkdir(parents=True, exist_ok=True)
    archive = Path(tempfile.mktemp(prefix=f"{cfg.base_dir.name}-", suffix=".tar.gz"))
    urllib.request.urlretrieve(
        f"https://github.com/{cfg.repo}/archive/{cfg.base_commit}.tar.gz", archive
    )
    cfg.base_dir.mkdir()
    subprocess.run(
        ["tar", "-xzf", str(archive), "--strip-components=1", "-C", str(cfg.base_dir)],
        check=True,
    )
    archive.unlink(missing_ok=True)


def _clone_tree(source: Path, target: Path) -> None:
    """Copy a tree with copy-on-write clones where the filesystem allows it.

    Every run needs two full source-tree copies (the actor's candidate and
    the grader's isolated copy). On APFS, ``cp -cR`` shares extents between
    the copies until either side modifies a file, so the marginal disk cost
    of each run is only the edits and the applied test patch, not two
    full ~60MB trees. Editing one side never touches the other: the clone
    and the original are separate inodes. Falls back to a real copy when
    the clone is unavailable (non-APFS volume, missing -c support).
    """
    try:
        result = subprocess.run(
            ["cp", "-cR", str(source), str(target)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and target.exists():
            return
    except (OSError, subprocess.SubprocessError):
        pass
    shutil.copytree(source, target)


def _run_tests(
    cfg: InstanceConfig, instance: dict, project: Path, timeout: float
) -> dict:
    if cfg.sitecustomize is not None:
        # Compatibility shims are isolated to the grader environment; they are
        # not part of the candidate workspace or the agent's task.
        (project / "sitecustomize.py").write_text(
            cfg.sitecustomize, encoding="utf-8"
        )
    patch_path = project / ".swebench_test.patch"
    patch_path.write_text(instance["test_patch"], encoding="utf-8")
    applied = subprocess.run(["git", "apply", str(patch_path)], cwd=project, text=True,
                             capture_output=True)
    patch_path.unlink(missing_ok=True)
    if applied.returncode:
        return {"patch_applied": False, "returncode": applied.returncode,
                "stdout": applied.stdout[-4000:], "stderr": applied.stderr[-4000:]}

    interpreter = str(cfg.interpreter or Path(sys.executable))
    argv = [interpreter, *cfg.test_command(project, instance)]
    env = os.environ.copy()
    # The workspace root shadows any installed copy of the same package so the
    # grader exercises the candidate's source, not the base environment.
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(project), env.get("PYTHONPATH")) if item
    )
    try:
        result = subprocess.run(argv, cwd=project, env=env, text=True,
                                capture_output=True, timeout=max(1.0, float(timeout)))
    except subprocess.TimeoutExpired as exc:
        return {
            "patch_applied": True,
            "returncode": 124,
            "passed": False,
            "timed_out": True,
            "timeout_seconds": float(timeout),
            "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }
    return {"patch_applied": True, "returncode": result.returncode,
            "passed": result.returncode == 0, "timed_out": False,
            "timeout_seconds": float(timeout), "stdout": result.stdout[-12000:],
            "stderr": result.stderr[-4000:]}


def _agent_env(cfg: InstanceConfig, project: Path, shim_dir: Path | None) -> dict[str, str]:
    agent_env = os.environ.copy()
    interpreter = Path(cfg.interpreter or sys.executable)
    # Tool commands launched by the actor must resolve the same interpreter
    # and package manager that launched the agent. Without this, a model's
    # ordinary `python`/`pip` probe can silently hit the host installation
    # while the independent grader uses the configured virtual environment.
    # Do not call ``resolve()`` here: virtualenv Python is commonly a symlink
    # to the host binary, and resolving it would discard the virtualenv's
    # `bin` directory—the exact mismatch this environment contract prevents.
    interpreter_bin = str(interpreter.absolute().parent)
    agent_env["PATH"] = os.pathsep.join(
        part for part in (interpreter_bin, agent_env.get("PATH")) if part
    )
    agent_env["VIRTUAL_ENV"] = sys.prefix if cfg.interpreter is None else str(
        (interpreter.parent.parent).resolve()
    )
    existing_pythonpath = agent_env.get("PYTHONPATH")
    leading: list[str] = []
    if shim_dir is not None:
        # Compatibility shims live outside the candidate so the actor cannot
        # see or mutate them.
        leading.append(str(shim_dir))
    if cfg.interpreter is not None:
        # A dedicated venv means the workspace source must shadow any
        # installed copy of the package for the actor's own test runs.
        leading.append(str(project))
    agent_env["PYTHONPATH"] = os.pathsep.join(
        item for item in (*leading, existing_pythonpath) if item
    )
    # Unbuffered child output is required for true event-level monitoring;
    # otherwise Python holds agent logs until the entire run exits.
    agent_env["PYTHONUNBUFFERED"] = "1"
    return agent_env


def _preflight_probe_target(candidate: Path, instance: dict) -> str:
    """Best-effort probe target for the harness preflight check.

    Purely generic (a filesystem search, no per-repo path convention): find
    the test module implicated by the dataset's own FAIL_TO_PASS list
    inside the candidate tree, so the preflight self-check discovers the
    same kind of test package the actor naturally converges on. Probing the
    bare repository root instead can crash during module import itself
    (before any TestResult exists to classify) — measured live on
    django-14034, where a root-level probe raised an uncaught
    ``ImproperlyConfigured`` instead of producing the classified
    harness-evidence result the actor's own run actually hit. Falls back to
    the repository root when no runnable label parses or no matching file
    is found, which only makes the probe less targeted, never wrong in a
    way that could block a healthy run.
    """
    try:
        labels = _runnable_labels(json.loads(instance.get("FAIL_TO_PASS", "[]")))
    except (json.JSONDecodeError, TypeError):
        labels = []
    if not labels or len(labels[0].split(".")) < 3:
        return "."
    basename = labels[0].split(".")[-3] + ".py"
    matches = sorted(candidate.rglob(basename))
    if not matches:
        return "."
    return str(matches[0].relative_to(candidate))


def _preflight_harness_evidence(
    cfg: InstanceConfig, candidate: Path, shim_dir: Path | None, instance: dict,
) -> str | None:
    """Detect a broken agent-facing test harness before spending a model turn.

    Runs the exact same generic self-check the actor's ``run_tests`` kernel
    tool would run, against the pristine (unmodified) baseline checkout —
    decoupling "is the harness invocable" from "does the product behave
    correctly", the same way the independent grader's own
    ``ENVIRONMENT_INVALID`` classification does for the final grading pass.
    If the harness comes back harness-evidence-only (test-framework
    initialization, never a product assertion — see
    ``_framework_setup_only`` in ``workspace/run_tests_tool.py``), no
    product edit the actor could ever make would change that signal, so no
    model turn should be spent discovering this the slow way. Returns the
    harness-evidence detail when the harness is broken, else ``None``.
    """
    env = _agent_env(cfg, candidate, shim_dir)
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (env.get("PYTHONPATH"), str(ROOT)) if item
    )
    target = _preflight_probe_target(candidate, instance)
    probe = (
        "import json\n"
        "from workspace.run_tests_tool import run_tests\n"
        f"ok, summary = run_tests({target!r})\n"
        "print(json.dumps({'ok': ok, 'summary': summary}))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=candidate, env=env,
            text=True, capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        # The preflight itself stalling is not harness-evidence either way;
        # let the run proceed normally rather than guessing.
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None
    summary = str(payload.get("summary", ""))
    if not payload.get("ok") and "harness evidence, not product evidence" in summary:
        return summary
    return None


def run(instance_id: str, mode: str, iterations: int, primary_model: str | None = None,
        worker_model: str | None = None, action_critic: bool = False,
        chat_timeout: float | None = None, action_gate: bool = False,
        structured_summary: bool = False, backend: str = "ollama",
        base_url: str = "http://127.0.0.1:8080/v1",
        action_first: bool = False, grade_timeout: float | None = None,
        thinking: bool = False, working_memory: bool = False,
        thinking_repair: bool = False, reproduce_first: bool = False,
        editor: str = "patch_file") -> dict:
    cfg = INSTANCES[instance_id]
    instance = _load_instance(instance_id)
    _prepare_base(cfg)
    run_id = f"{instance_id.split('__')[-1]}-{mode}-{int(time.time())}"
    work = Path(tempfile.mkdtemp(prefix=f"{run_id}-"))
    candidate = work / "candidate"
    _clone_tree(cfg.base_dir, candidate)
    # Make the candidate a real git repository with one baseline commit so
    # the review tools (git_status/git_diff) and any rollback path work
    # exactly as they would in a normal checkout. The base archive ships
    # without history; the baseline commit is generated locally.
    subprocess.run(["git", "init", "-q", str(candidate)], check=False)
    subprocess.run(["git", "-C", str(candidate), "add", "-A"], check=False)
    subprocess.run(
        ["git", "-C", str(candidate), "-c", "user.email=novelty@local",
         "-c", "user.name=novelty-engine", "commit", "-q", "-m", "baseline"],
        check=False,
    )
    shim_dir: Path | None = None
    if cfg.sitecustomize is not None:
        shim_dir = work / "compat"
        shim_dir.mkdir()
        (shim_dir / "sitecustomize.py").write_text(
            cfg.sitecustomize, encoding="utf-8"
        )
    harness_evidence = _preflight_harness_evidence(cfg, candidate, shim_dir, instance)
    if harness_evidence is not None:
        # The actor's self-check tool cannot produce real product evidence
        # for this instance even on an untouched baseline. No model turn
        # can fix that, so refuse the run instead of burning the iteration
        # budget against a signal that structurally cannot change.
        report = {
            "run_id": run_id, "instance_id": instance_id, "mode": mode,
            "status": "ENVIRONMENT_INVALID",
            "detail": harness_evidence[:4000],
            "primary_model": primary_model, "backend": backend, "base_url": base_url,
            "editor": editor,
            "elapsed_seconds": 0.0, "agent_returncode": None,
            "grading": {"passed": False, "status": "ENVIRONMENT_INVALID"},
        }
        output = ROOT / "state" / "benchmark" / "runs" / f"{run_id}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"run_id": run_id, "instance_id": instance_id,
                          "status": "ENVIRONMENT_INVALID",
                          "detail": harness_evidence[:2000]}, indent=2))
        shutil.rmtree(work, ignore_errors=True)
        return report
    task = (
        f"Work on this {cfg.repo} issue. Use the available tools to inspect and modify "
        "the repository, run focused tests, and call finish_task only after the "
        "implementation is complete and verified."
    )
    if cfg.test_entry_hint:
        task += (
            "\n\nThe repository's supported test entry point is: "
            + cfg.test_entry_hint
            + ". Run it with a focused test label to validate changes."
        )
    task += "\n\n" + instance["problem_statement"]
    status_file = work / "status.json"

    def launch_phase(prompt: str, budget: int, extra_flags: tuple[str, ...],
                      label: str = "primary") -> tuple[str, float, bool]:
        """Run one agent phase on the candidate and return (stdout, elapsed)."""
        phase_command = [
            sys.executable, str(ROOT / "agent.py"), "--project", str(candidate),
            "--iteration-budget", str(budget), "--status-file", str(status_file),
            "--backend", backend, "--base-url", base_url, prompt,
        ]
        if action_first:
            phase_command[2:2] = ["--action-first"]
        if thinking:
            phase_command[2:2] = ["--thinking"]
        if working_memory:
            phase_command[2:2] = ["--working-memory"]
        if reproduce_first:
            phase_command[2:2] = ["--reproduce-first"]
        if editor:
            phase_command[2:2] = ["--editor", editor]
        if primary_model:
            phase_command[2:2] = ["--model", primary_model]
        phase_command[2:2] = list(extra_flags)
        if mode == "novelty":
            option_index = len(phase_command) - 1
            phase_command.insert(option_index, "--novelty-context")
            if structured_summary:
                phase_command.insert(option_index + 1, "--structured-summary")
                option_index += 1
            if action_critic:
                phase_command.insert(option_index + 1, "--novelty-action-critic")
                option_index += 1
            if action_gate:
                phase_command.insert(option_index + 1, "--novelty-action-gate")
                option_index += 1
            if worker_model:
                phase_command[option_index + 1:option_index + 1] = ["--novelty-worker-model", worker_model]
        if chat_timeout is not None:
            phase_command[2:2] = ["--chat-timeout", str(chat_timeout)]
        phase_command[2:2] = list(cfg.extra_agent_args)
        started = time.time()
        started_monotonic = time.monotonic()
        agent_env = _agent_env(cfg, candidate, shim_dir)
        # The hard cap must exceed the worst case the agent can legally
        # spend: budget turns x (chat timeout + retry headroom). A cap
        # shorter than the loop's legal worst case kills healthy runs.
        hard_cap = max(3600, budget * max(60.0, (chat_timeout or 60.0) * 2))
        monitor_path = ROOT / "state" / "benchmark" / "runs" / f"monitor-{run_id}-{label}.jsonl"
        proc = subprocess.Popen(
            phase_command, cwd=ROOT, env=agent_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        stdout_text, timed_out, _returncode = stream_agent(
            proc, started_monotonic, hard_cap, monitor_path
        )
        if timed_out:
            stdout_text += "\n[runner hard cap expired; agent was terminated mid-phase]\n"
        return stdout_text, time.time() - started, timed_out

    stdout_text, elapsed, primary_timed_out = launch_phase(task, iterations, (), label="primary")

    grade_parent = Path(tempfile.mkdtemp(prefix=f"{run_id}-grade-"))
    grade = grade_parent / "candidate"
    _clone_tree(candidate, grade)
    grading = _run_tests(
        cfg, instance, grade, grade_timeout if grade_timeout is not None else cfg.grade_timeout
    )

    # "Thinking as a multiplier": when enabled and the primary phase did not
    # resolve the instance, a bounded thinking repair continues on the same
    # workspace with the independent grader's exact failure evidence. The
    # harness names no test and prescribes no fix; it only passes the
    # rejection back.
    thinking_repair_record = None
    if (thinking_repair and not grading.get("passed")
            and not grading.get("timed_out")):
        repair_prompt = (
            task
            + "\n\n## Independent verifier feedback\n"
            "The previous handoff was rejected by the independent grader. Treat the exact "
            "failure evidence below as authoritative. Inspect the current workspace, make the "
            "smallest coherent repair, run the repository's supported test entry point on the "
            "failing behavior, and call finish_task promptly after it passes. Do not weaken or "
            "rewrite supplied tests.\n"
            + (grading.get("stdout", "") + grading.get("stderr", ""))[-6000:]
        )
        repair_stdout, repair_elapsed, _repair_timed_out = launch_phase(
            repair_prompt, max(10, min(iterations, 30)), ("--thinking",), label="repair"
        )
        grade_parent2 = Path(tempfile.mkdtemp(prefix=f"{run_id}-repair-grade-"))
        grade2 = grade_parent2 / "candidate"
        _clone_tree(candidate, grade2)
        grading = _run_tests(
            cfg, instance, grade2,
            grade_timeout if grade_timeout is not None else cfg.grade_timeout,
        )
        repair_log_path = ROOT / "state" / "benchmark" / "runs" / f"{run_id}-repair.log"
        repair_log_path.write_text(repair_stdout, encoding="utf-8")
        thinking_repair_record = {
            "elapsed_seconds": round(repair_elapsed, 1),
            "log": str(repair_log_path),
            "monitor_log": str(ROOT / "state" / "benchmark" / "runs" / f"monitor-{run_id}-repair.jsonl"),
            "grading": grading,
        }
        shutil.rmtree(grade_parent2, ignore_errors=True)
    # The JSON keeps only a bounded tail; preserve the complete actor
    # transcript for forensics so a failed run's early trajectory is not
    # lost when the interesting events happened before the final turns.
    log_path = ROOT / "state" / "benchmark" / "runs" / f"{run_id}.log"
    log_path.write_text(stdout_text, encoding="utf-8")
    report = {
        "run_id": run_id, "instance_id": instance_id, "mode": mode,
        "primary_model": primary_model,
        "worker_model": worker_model, "base_commit": cfg.base_commit,
        "action_critic": action_critic,
        "chat_timeout": chat_timeout,
        "action_gate": action_gate,
        "structured_summary": structured_summary,
        "backend": backend,
        "base_url": base_url,
        "action_first": action_first,
        "thinking": thinking,
        "working_memory": working_memory,
        "thinking_repair": thinking_repair_record,
        "reproduce_first": reproduce_first,
        "editor": editor,
        "grade_timeout": grade_timeout,
        "fail_to_pass": json.loads(instance["FAIL_TO_PASS"]),
        "pass_to_pass": json.loads(instance["PASS_TO_PASS"]),
        "elapsed_seconds": round(elapsed, 1), "agent_returncode": 0,
        "primary_timed_out": primary_timed_out,
        "status_file": str(status_file),
        "agent_log": str(log_path),
        "monitor_log": str(ROOT / "state" / "benchmark" / "runs" / f"monitor-{run_id}-primary.jsonl"),
        "agent_output_tail": stdout_text[-12000:], "agent_error_tail": "",
        "grading": grading,
    }
    output = ROOT / "state" / "benchmark" / "runs" / f"{run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "instance_id": instance_id, "mode": mode,
                      "elapsed_seconds": round(elapsed, 1),
                      "agent_returncode": 0, "grading": grading}, indent=2))
    # The candidate and grader copies are the durable cost of each run (a
    # full source-tree copy apiece). The JSON report and log transcript are
    # already safe in the repository state directory, so remove the
    # temporary workspaces instead of letting them fill the system temp
    # volume across repeated runs.
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(grade_parent, ignore_errors=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", choices=sorted(INSTANCES),
                        default="sympy__sympy-13878")
    parser.add_argument("--mode", choices=["baseline", "novelty"], default="novelty")
    parser.add_argument("--iterations", type=int, default=250)
    parser.add_argument("--primary-model", default=None)
    parser.add_argument("--worker-model", default=None)
    parser.add_argument("--action-critic", action="store_true",
                        help="Enable the 4B worker's bounded next-action directive in novelty mode.")
    parser.add_argument("--chat-timeout", type=float, default=None,
                        help="Maximum seconds per acting-model turn; useful for bounded experiments.")
    parser.add_argument("--action-gate", action="store_true",
                        help="Enable bounded tool restriction after novelty stagnation.")
    parser.add_argument("--structured-summary", action="store_true",
                        help="Also enable the separate structured-summary/governor layer.")
    parser.add_argument("--backend", choices=["ollama", "llama-cpp"], default="llama-cpp")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--action-first", action="store_true",
                        help="Use the model-neutral initial action contract.")
    parser.add_argument("--thinking", action="store_true",
                        help="Enable the actor's hybrid thinking mode.")
    parser.add_argument("--working-memory", action="store_true",
                        help="Enable the host-owned working memory board.")
    parser.add_argument("--reproduce-first", action="store_true",
                        help="Lock product mutations until a failing reproduction runs.")
    parser.add_argument("--editor", choices=["patch_file", "edit_range"], default="patch_file",
                        help="Model-facing edit primitive.")
    parser.add_argument("--thinking-repair", action="store_true",
                        help="After a failed primary phase, run a bounded thinking repair "
                             "on the same workspace with the grader's exact evidence.")
    parser.add_argument("--grade-timeout", type=float, default=None,
                        help="Maximum seconds for the independent grader subprocess.")
    args = parser.parse_args()
    run(args.instance, args.mode, args.iterations, args.primary_model, args.worker_model,
        args.action_critic, args.chat_timeout, args.action_gate, args.structured_summary,
        args.backend, args.base_url, args.action_first, args.grade_timeout,
        args.thinking, args.working_memory, args.thinking_repair, args.reproduce_first,
        args.editor)
