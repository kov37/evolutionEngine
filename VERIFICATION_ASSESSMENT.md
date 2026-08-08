# Verification and Tool-Promotion Assessment

## Executive summary

The current verification system is bypassable by design. Tool promotion primarily proves that a candidate Python file exits with code `0` and exposes a callable with the expected name. It does not independently establish that the callable satisfies its contract, and the candidate authors the self-test used as evidence for its own approval.

The bootstrap environment also exposes unrestricted shell execution, while candidate modules are imported directly into the trusted verifier process. Either path allows model-authored code to modify the registry manifest or verification infrastructure instead of passing the intended promotion gate.

The most important architectural change is to separate candidate construction, independent verification, and promotion into distinct trust domains. Promotion should apply to the exact content that passed trusted external tests, identified by a cryptographic digest and stored immutably.

## Critical findings

### 1. Verification checks existence, not correctness

`registry.verify()` only:

1. Checks that the candidate path exists.
2. Imports the candidate module.
3. Looks up the requested attribute.
4. Confirms that the attribute is callable.

It performs no behavioral or contract validation. A candidate equivalent to the following can graduate:

```python
def required_function(*args, **kwargs):
    return "completely wrong"


if __name__ == "__main__":
    pass  # exits 0
```

The automatic execution check in `kernel/io_tools.py` treats any zero exit as success and sets `RUN_STATE["goal_met"]`. The harness then invokes the callable-only check, and `verify_and_promote()` records the candidate in the manifest.

Relevant code:

- `kernel/io_tools.py`, `_test_after_write()`, lines 55-79
- `harness.py`, `run_recursive_engine()`, lines 57-76
- `registry.py`, `verify()`, lines 102-118
- `registry.py`, `verify_and_promote()`, lines 121-128

### 2. The candidate controls its own test

The graduation contract in `curriculum.py` explicitly states that the candidate's internal zero-argument self-test is the only verification performed by the harness. The implementation therefore authors both the behavior being judged and most of the evidence used to approve it.

This creates trivial deliberate bypasses, such as an empty `__main__` block, as well as accidental false positives caused by errors in candidate-authored tests.

There is already a concrete example in a promoted tool. In `workspace/diff_files_tool.py`, the following assignments create nonempty tuples rather than booleans:

```python
found_minus_beta = any(
    "-beta" in line for line in lines
), "Expected '-beta' (removed line)"
```

The same pattern is used for two other expected markers. Because nonempty tuples are always truthy, the subsequent assertions cannot fail even when the expected diff markers are absent.

Relevant code:

- `curriculum.py`, `GRADUATION_CONTRACT`, lines 18-25
- `workspace/diff_files_tool.py`, lines 113-127

### 3. Candidate code executes inside the trusted verifier

`registry.verify()` uses `spec.loader.exec_module(module)` to import the candidate directly into the verifier process. Importing Python is code execution, not inspection.

Candidate import-time code can therefore:

- Call `registry.promote()` directly.
- Rewrite `state/registry_manifest.json`.
- Modify verifier or kernel state.
- Change `RUN_STATE` or `AUTO_RUN_AFTER_WRITE`.
- Replace existing tools or verifier functions in memory.
- Read arbitrary files available to the process.
- Launch subprocesses or make network requests.
- Hang indefinitely because the import has no timeout.

A candidate can mutate the approval system even if `verify()` later reports failure.

Relevant code:

- `registry.py`, `verify()`, lines 109-115
- `registry.py`, `_load_graduated_tools()`, lines 67-78

### 4. `run_shell` bypasses the promotion boundary

The bootstrap model receives `run_shell` as a kernel tool. `run_shell` passes arbitrary model-provided text to `subprocess.run(..., shell=True)`.

Its working directory is set to the workspace, but commands are not confined to that directory. The module's documentation explicitly acknowledges that commands can use parent traversal, absolute paths, and network access.

From the default `workspace/` directory, a model-authored command can directly target files such as:

```text
../state/registry_manifest.json
../registry.py
../harness.py
```

This means a model can modify the registry or verifier instead of satisfying the promotion contract. The automatic candidate execution check has the same underlying limitation: it is a normal subprocess with a selected working directory, not an OS security sandbox.

Relevant code:

- `kernel/exec_tools.py`, module warning, lines 1-10
- `kernel/exec_tools.py`, `run_shell()`, lines 27-46
- `registry.py`, `KERNEL_TOOLS`, line 30

### 5. Promoted code remains mutable after verification

The registry manifest stores absolute paths to live files under `workspace/`. Every registry load imports the current contents at those paths.

The promotion record contains no:

- Source digest.
- Immutable promoted copy.
- Verification timestamp or result.
- Verifier or contract version.
- Declared capability policy beyond path-parameter names.
- Detection of changes made after promotion.

A tool can be safe when promoted and replaced afterward. The replacement is loaded automatically without re-verification.

Relevant code:

- `state/registry_manifest.json`
- `registry.py`, `_load_graduated_tools()`, lines 67-78
- `registry.py`, `promote()`, lines 90-99

### 6. Manifest entries are trusted blindly

`is_promoted()` only checks whether a tool name exists as a key in the manifest. It does not confirm that the module still exists, loads successfully, matches an approved digest, or exposes a valid function.

Consequences include:

- A missing or unloadable tool remains classified as promoted.
- The curriculum skips rebuilding stale or invalid entries.
- Arbitrary module paths and function names are accepted.
- A forged entry can point outside the expected promoted-tool directory.
- Invalid manifest structure can crash registry operations.

There is also a tool-name collision risk. `load_registry()` returns kernel tools followed by graduated tools, and the harness builds its dispatch map using a dictionary keyed by each function's `__name__`. A graduated function named `write_file`, `run_shell`, or another trusted kernel function can replace the kernel implementation in the dispatch map.

Relevant code:

- `registry.py`, `load_registry()`, lines 81-83
- `registry.py`, `is_promoted()`, lines 86-87
- `harness.py`, tool-map construction, line 17
- `harness.py`, promoted-entry skip, lines 88-91

### 7. Path confinement is not a capability boundary

`_wrap_with_confinement()` rewrites declared string keyword arguments through `kernel.sandbox.confine()` before invoking a graduated function. This protects against cooperative tools receiving an escaping path through a known parameter.

It does not constrain the Python code itself. A promoted tool can independently perform operations such as:

```python
open("/some/absolute/path")
subprocess.run([...])
urllib.request.urlopen(...)
```

Additional weaknesses include:

- Only parameters listed in `path_params` are checked.
- Only string keyword arguments are rewritten.
- The declaration does not cover paths constructed internally.
- Network, subprocess, environment, and other capabilities are unrestricted.

`path_params` is useful input normalization, but it is not a sandbox for untrusted tool implementations.

Relevant code:

- `registry.py`, `_wrap_with_confinement()`, lines 49-64
- `kernel/sandbox.py`, `confine()`, lines 38-45

## Additional weaknesses

### Expected filename is not enforced

The target filename is included in curriculum prose, but the harness promotes whichever Python file most recently exited successfully and defined the expected callable. There is no trusted comparison against a curriculum-owned expected path.

### Function signatures are not checked

The graduation contract specifies exact signatures, but verification only checks `callable()`. Missing parameters, extra required parameters, incorrect defaults, and incompatible calling conventions can all pass.

### Verification and loading are vulnerable to time-of-check/time-of-use changes

Verification imports a mutable path and promotion subsequently records that path. The candidate can modify itself during import or be changed between verification, promotion, and later loading.

### Import and runtime resource limits are incomplete

The automatic script execution has a ten-second timeout, but candidate imports in `verify()` and `_load_graduated_tools()` have no timeout or process isolation. A promoted tool can also consume unbounded memory or CPU once called.

### Graduated-tool failures can crash orchestration

`dispatch_tool_calls()` converts `TypeError` and `ValueError` into tool messages, but other exceptions from graduated tools propagate and can terminate the agent loop.

## Assessment of the `evolve/` verifier

The untracked `evolve/` work introduces a substantially better verification pattern:

- It uses fixed checkers that are not authored by each candidate.
- It invokes the produced callable against independent cases.
- It runs goals in fresh subprocesses.
- It distinguishes harness-reported success from checker-confirmed success.

The key implementation is in `evolve/run_single_goal.py`, particularly the independent invocation around lines 63-79.

This is a useful starting point but not yet a security boundary. Candidate code is still imported into the same process as the checker and retains ordinary filesystem, import, subprocess, environment, and network access. A deliberately adversarial candidate could inspect or mutate modules in `sys.modules`, locate checker source on disk, or alter process state before the checker runs.

Fresh subprocesses improve state isolation between attempts, but candidate execution still needs OS-level isolation from the verifier and its trusted inputs.

## Recommended architecture

### Phase 1: Candidate construction

1. Create a fresh disposable staging directory for each candidate.
2. Restrict model writes to that directory.
3. Remove unrestricted `run_shell` from the bootstrap tool set.
4. If command execution is necessary, expose an argument-based allowlist rather than `shell=True` text execution.
5. Do not treat successful writes or zero-exit self-tests as proof of correctness.
6. Treat candidate-authored self-tests as optional development feedback only.

### Phase 2: Independent verification

1. Maintain trusted, external checkers for each tool contract.
2. Keep checkers outside all model-editable directories.
3. Test normal behavior, edge cases, errors, return types, and resource limits.
4. Verify the exact expected function signature with `inspect.signature()`.
5. Enforce the expected filename or an explicit trusted mapping.
6. Run each candidate in a fresh process with hard time and output limits.
7. Use OS-level sandboxing, a container, or a VM to restrict filesystem, network, subprocess, and environment access.
8. Communicate with the candidate runner through a narrow serialized protocol rather than importing candidate code into the orchestrator.

### Phase 3: Attested promotion

1. Compute a SHA-256 digest of the exact source that passed verification.
2. Copy verified source into a dedicated immutable promoted-tool directory.
3. Name or address the promoted artifact by its digest.
4. Store a structured attestation containing:
   - Tool name.
   - Function name and expected signature.
   - Source digest.
   - Contract version.
   - Verifier version.
   - Verification timestamp.
   - Test result summary.
   - Approved capabilities.
5. Make promotion require a valid verifier-produced attestation rather than public path and function-name arguments.
6. Write manifest updates atomically.

### Phase 4: Secure loading and execution

1. Validate manifest schema before using it.
2. Require all module paths to resolve beneath the promoted-tool directory.
3. Recompute and compare the source digest before every load.
4. Reject kernel-tool name collisions and duplicate callable names.
5. Treat missing, changed, or unloadable tools as unpromoted.
6. Prefer running graduated tools in isolated worker processes rather than importing them into the agent process.
7. Enforce declared capabilities outside the tool's own code.

## Required regression tests

The redesigned verifier should include explicit tests proving rejection of:

- A script that exits `0` without testing anything.
- A callable with incorrect behavior.
- A callable with the wrong signature.
- A self-test containing assertions that are accidentally always truthy.
- A candidate that attempts import-time self-promotion.
- A candidate that attempts to modify the manifest or verifier.
- A candidate that hangs during import or invocation.
- A candidate that passes and then modifies itself.
- A manifest entry pointing outside the promoted directory.
- A stale manifest entry whose source is missing.
- A changed promoted file whose digest no longer matches.
- A graduated tool attempting to shadow a kernel tool.
- A tool accessing undeclared files, network hosts, environment variables, or subprocesses.

Trusted checker suites should also be mutation-tested: deliberately broken implementations should be generated or maintained, and every one must fail the relevant checker. This tests the quality of the verification system rather than merely testing successful implementations.

## Recommended implementation order

### Immediate containment

1. Remove unrestricted `run_shell` from the curriculum bootstrap environment.
2. Stop using candidate-authored self-tests as the promotion decision.
3. Add trusted external behavioral checkers for current curriculum tools.
4. Enforce expected filename, callable name, and function signature.
5. Reject all kernel and graduated-tool name collisions.
6. Change `is_promoted()` to validate the underlying entry rather than checking key presence.

### Integrity hardening

1. Promote immutable copies instead of mutable workspace paths.
2. Add source digests and verifier-version attestations.
3. Validate manifest schema and canonical paths.
4. Make promotion callable only with a verifier-issued result.
5. Add regression and mutation tests for all known bypasses.

### Isolation hardening

1. Move candidate verification into an OS-level sandbox or container.
2. Execute promoted tools through isolated workers.
3. Introduce explicit capability declarations and enforcement.
4. Add CPU, memory, time, output, filesystem, network, and subprocess limits.

## Conclusion

The present system is suitable as a cooperative tool-generation experiment, but it does not provide a meaningful verification or security boundary. Its central trust inversion is that model-authored code creates the implementation, creates its own evidence, and executes with enough authority to change the approval mechanism.

The strongest existing direction is the independent-checker pattern under `evolve/`. The production promotion path should adopt that separation, strengthen it with immutable content-addressed artifacts and attestations, and place candidate execution behind an actual process or OS isolation boundary.
