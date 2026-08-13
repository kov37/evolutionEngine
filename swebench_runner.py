"""Run and independently grade noveltyEngine on SymPy #13878.

The agent never receives the official test patch.  The grader applies that
patch only to a separate copy after the agent exits, then runs the FAIL_TO_PASS
and PASS_TO_PASS tests.  This is intentionally lightweight and does not depend
on Docker; it is a local progress harness, not a replacement for official
SWE-bench evaluation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_URL = "https://huggingface.co/datasets/SWE-bench/SWE-bench/resolve/main/data/test-00000-of-00001.parquet"
DATA_PATH = ROOT / "assets" / "swebench-test.parquet"
BASE_DIR = ROOT / "assets" / "benchmarks" / "sympy-13878"
BASE_COMMIT = "7b127bdf71a36d85216315f80c1b54d22b060818"


def _load_instance() -> dict:
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
    table = pq.read_table(DATA_PATH, filters=[("instance_id", "=", "sympy__sympy-13878")])
    rows = table.to_pylist()
    if len(rows) != 1:
        raise RuntimeError(f"expected one SymPy instance, found {len(rows)}")
    return rows[0]


def _prepare_base() -> None:
    if BASE_DIR.exists():
        return
    BASE_DIR.parent.mkdir(parents=True, exist_ok=True)
    archive = Path(tempfile.mktemp(prefix="sympy-13878-", suffix=".tar.gz"))
    urllib.request.urlretrieve(
        f"https://github.com/sympy/sympy/archive/{BASE_COMMIT}.tar.gz", archive
    )
    BASE_DIR.mkdir()
    subprocess.run(
        ["tar", "-xzf", str(archive), "--strip-components=1", "-C", str(BASE_DIR)],
        check=True,
    )
    archive.unlink(missing_ok=True)


def _run_tests(project: Path, test_patch: str) -> dict:
    # This historical SymPy commit predates Python 3.10's collections ABC
    # move. Keep the compatibility shim isolated to the grader environment;
    # it is not part of the candidate workspace or the agent's task.
    (project / "sitecustomize.py").write_text(
        "import collections, collections.abc\n"
        "for _name in ('Mapping', 'MutableMapping', 'MutableSet', 'Sequence', 'Iterable', 'Callable'):\n"
        "    if not hasattr(collections, _name): setattr(collections, _name, getattr(collections.abc, _name))\n",
        encoding="utf-8",
    )
    patch_path = project / ".swebench_test.patch"
    patch_path.write_text(test_patch, encoding="utf-8")
    applied = subprocess.run(["git", "apply", str(patch_path)], cwd=project, text=True,
                             capture_output=True)
    patch_path.unlink(missing_ok=True)
    if applied.returncode:
        return {"patch_applied": False, "returncode": applied.returncode,
                "stdout": applied.stdout[-4000:], "stderr": applied.stderr[-4000:]}

    tests = " or ".join([
        "test_arcsin", "test_ContinuousDomain", "test_characteristic_function",
        "test_benini", "test_chi", "test_chi_noncentral", "test_chi_squared",
        "test_gompertz", "test_shiftedgompertz", "test_trapezoidal",
        "test_quadratic_u", "test_von_mises", "test_prefab_sampling",
        "test_input_value_assertions", "test_probability_unevaluated",
        "test_density_unevaluated", "test_random_parameters",
        "test_random_parameters_given", "test_conjugate_priors", "test_issue_10003",
    ])
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project)
    command = [sys.executable, "-m", "pytest", "-q",
               "sympy/stats/tests/test_continuous_rv.py", "-k", tests]
    result = subprocess.run(command, cwd=project, env=env, text=True,
                            capture_output=True, timeout=900)
    return {"patch_applied": True, "returncode": result.returncode,
            "passed": result.returncode == 0, "stdout": result.stdout[-12000:],
            "stderr": result.stderr[-4000:]}


def run(mode: str, iterations: int, primary_model: str | None = None,
        worker_model: str | None = None, action_critic: bool = False,
        chat_timeout: float | None = None, action_gate: bool = False) -> dict:
    instance = _load_instance()
    _prepare_base()
    run_id = f"sympy-13878-{mode}-{int(time.time())}"
    work = Path(tempfile.mkdtemp(prefix=f"{run_id}-"))
    candidate = work / "candidate"
    shutil.copytree(BASE_DIR, candidate)
    task = (
        "Work on this SymPy issue. Use the available tools to inspect and modify the repository, "
        "run focused tests, and call finish_task only after the implementation is complete and "
        "verified.\n\n" + instance["problem_statement"]
    )
    status_file = work / "status.json"
    command = [sys.executable, str(ROOT / "agent.py"), "--project", str(candidate),
               "--iteration-budget", str(iterations), "--status-file", str(status_file), task]
    if primary_model:
        command[2:2] = ["--model", primary_model]
    if mode == "novelty":
        command.insert(2, "--novelty-context")
        # Novelty context is evaluated with the deterministic progress ledger
        # enabled as well. The 4B worker supplies semantic judgments, while
        # the structured layer supplies model-independent anti-stagnation
        # intervention and durable file/fact state.
        command.insert(3, "--structured-summary")
        if action_critic:
            command.insert(4, "--novelty-action-critic")
        if action_gate:
            command.insert(5 if action_critic else 4, "--novelty-action-gate")
        if worker_model:
            insert_at = 5 if action_critic else 4
            command[insert_at:insert_at] = ["--novelty-worker-model", worker_model]
    if chat_timeout is not None:
        command[2:2] = ["--chat-timeout", str(chat_timeout)]
    command.extend([
        "--distribution-target-file", "sympy/stats/crv_types.py",
        "--distribution-names",
        "Arcsin,Dagum,Erlang,Frechet,Gamma,GammaInverse,Kumaraswamy,Laplace,"
        "Logistic,Nakagami,StudentT,UniformSum",
    ])
    started = time.time()
    agent = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=3600)
    elapsed = time.time() - started

    grade = Path(tempfile.mkdtemp(prefix=f"{run_id}-grade-")) / "candidate"
    shutil.copytree(candidate, grade)
    grading = _run_tests(grade, instance["test_patch"])
    report = {
        "run_id": run_id, "mode": mode, "primary_model": primary_model,
        "worker_model": worker_model, "base_commit": BASE_COMMIT,
        "action_critic": action_critic,
        "chat_timeout": chat_timeout,
        "action_gate": action_gate,
        "fail_to_pass": json.loads(instance["FAIL_TO_PASS"]),
        "pass_to_pass": json.loads(instance["PASS_TO_PASS"]),
        "elapsed_seconds": round(elapsed, 1), "agent_returncode": agent.returncode,
        "status_file": str(status_file),
        "agent_output_tail": agent.stdout[-12000:], "agent_error_tail": agent.stderr[-4000:],
        "grading": grading,
    }
    output = ROOT / "state" / "benchmark" / "runs" / f"{run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "mode": mode, "elapsed_seconds": round(elapsed, 1),
                      "agent_returncode": agent.returncode, "grading": grading}, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
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
    args = parser.parse_args()
    run(args.mode, args.iterations, args.primary_model, args.worker_model,
        args.action_critic, args.chat_timeout, args.action_gate)
