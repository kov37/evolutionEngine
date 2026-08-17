# NoveltyEngine Toolset Audit

Date: 2026-08-16

## Plain-English conclusion

The toolkit is not fundamentally missing a magical “universal” tool. The main
problem is that too many tools can be visible at once, several tools overlap,
and the provider-generated JSON schema did not match the Python defaults. That
combination makes a small model spend turns choosing tools and occasionally
emit arguments that the host could have made unnecessary.

The right direction is a small, phase-based tool surface (only show the tools
needed for the current lifecycle phase), honest schemas, and host-side guards.
The 4B worker should receive no filesystem or shell tools. It should remain a
bounded JSON advisor; the host may accept its suggestion but must decide and
execute the action.

## Current model-facing inventory

The registry currently contains eight trusted kernel tools and ten graduated
tools, plus `finish_task` in the real agent. Network tools are optional, but
when enabled the full surface is still large for a small model.

| Area | Current tools | Audit decision |
| --- | --- | --- |
| Read/mutate | `read_file`, `write_file`, `patch_file` | Keep. These are the core file primitives. |
| Discover | `find_files`, `list_workspace`, graduated `list_dir` | Keep `find_files`; hide `list_workspace` and `list_dir` from the normal actor surface. They overlap and encourage broad inventory. |
| Search | graduated `search_file`, `grep_dir` | Keep both as lazy tools: single-file search for a known target, recursive search only when cross-file evidence is needed. |
| Structure | graduated `list_symbols` | Lazy-load. It is useful but language-specific, not a universal primitive. |
| Execute/validate | `run_command`, graduated `run_tests` | Keep both. `run_command` is the universal argv primitive; `run_tests` is a safer test shortcut with no-install behavior. |
| Processes | `process_status`, `stop_process` | Expose only after the actor starts a managed background process. |
| Review | graduated `diff_files`, `git_status`, `git_diff` | Keep for review, but expose late, near completion or recovery. |
| Network | graduated `web_search`, `fetch` | Opt-in only; never expose to the 4B worker and do not include in default coding runs. |
| Delivery | `finish_task` | Keep, but host completion evidence remains authoritative. |

`run_shell` is intentionally not in the default model-facing registry. That is
good: a universal shell looks powerful but creates a second, less structured
execution path and makes mutation/validation classification harder.

## Problems found

### 1. Default arguments were advertised as required

Ollama builds a Pydantic schema from the callable signature. A plain default
such as `path: str = "."` was still emitted as a required JSON property. This
affected `find_files`, `run_command`, `read_file`, `process_status`,
`run_tests`, and `grep_dir`.

The core functions now use nullable type annotations where an argument is
optional, while retaining the same runtime defaults. The provider schema now
requires only the truly essential fields, such as `run_command.command` and
`patch_file.path/search/replace`.

### 2. Recursive glob behavior was inconsistent

Python `fnmatch` treats `**/` as literal text, so `**/*.py` did not match a
root-level `cache.py`. The existing `find_files` tool now gives `**/` its
expected “zero or more directories” behavior while retaining path confinement,
noise-directory skipping, sorting, and a hard result cap.

### 3. Tool descriptions were incomplete

Several graduated tools used NumPy-style `Parameters` sections or no argument
section, while the local Ollama parser looks for `Args:`. The important search
and execution tools now expose field descriptions in the generated schema.
The remaining graduated review tools should be normalized before they are
made globally visible.

### 4. The registry is broader than the actor needs

The engine already narrows tools later through lifecycle policy, but the model
can still remember names from earlier turns. The host dispatch guard correctly
rejects stale calls, yet the better design is to reduce the visible set before
the model has to choose. This is a choice-load problem, not a lack of model
intelligence.

### 5. Error handling has two layers

Dispatch errors are already bounded and never crash the loop. Tool-specific
errors still vary in wording, which is useful for humans but less predictable
for a small actor. The next safe improvement is a compact error envelope with a
stable code, the failed tool, and one generic next action; preserve the raw
detail in the monitor log, not in the repeated actor context.

## Recommended graduated surfaces

These are policy surfaces, not separate implementations:

```python
SURFACES = {
    "orient": {
        "find_files", "read_file", "search_file", "grep_dir", "list_symbols",
    },
    "mutate": {"read_file", "patch_file", "write_file"},
    "validate": {"run_command", "run_tests", "read_file", "process_status"},
    "review": {"git_status", "git_diff", "diff_files", "read_file"},
    "deliver": {"finish_task"},
}
```

The host should add `stop_process` only when a managed process is active, add
`web_search`/`fetch` only when network access was explicitly enabled, and add
`list_workspace`/`list_dir` only for a setup-recovery case that actually needs
directory inventory. A surface is a visibility optimization; dispatch must
continue enforcing the same allow-list because models can remember old tool
names.

## 4B versus 27B

The 4B receives a compact event/state packet and a strict JSON schema. It gets
no tool definitions and has no authority to mutate files, run commands, or
declare success. Its useful job is to label a stale/repeated event and suggest
one next category such as `inspect`, `patch_file`, or `validate`. The host's
deterministic fallback wins whenever the worker is late, malformed, or stale.

The 27B receives the phase-appropriate actor surface and performs the actual
work. It should not receive the entire registry when four or five tools are
enough.

## Verification record

After the schema and discovery changes:

```text
targeted: 159 passed, 1 warning
full:     202 passed, 1 warning, 38 subtests passed
```

The LRU real-model run that exposed the discovery failure is not a pass. Its
child loaded the pre-fix implementation. The unchanged LRU fixture must be
rerun before claiming the schema or discovery changes improved agent behavior.

## Next implementation order

1. Add a host-owned phase surface selector without deleting legacy tools.
2. Add stable error codes and bounded actor-facing error text while retaining
   full raw errors in the monitor stream.
3. Add schema snapshots for every globally visible tool.
4. Rerun the unchanged LRU task, then pair it with a baseline run.
5. Measure tool-choice errors, repeated calls, first mutation time, validation
   count, useful progress per second, and hidden-grader outcome.

This sequence keeps the benchmark honest: it changes generic infrastructure,
not the LRU implementation or its expected answer.

## Pydantic V2 recommendation

Use strict Pydantic V2 models at external boundaries, not as a replacement for
every dataclass in the engine.

Good candidates:

- tool arguments after deterministic alias normalization;
- the 4B worker judgment;
- failed-validation packets and grader results;
- transaction records and monitor event records when they cross a process or
  JSONL boundary.

Keep dataclasses for high-frequency internal event/state objects, FSM state, and
objects containing process handles or callables. Those objects are controlled
by the host already and do not need JSON parsing on every transition.

The boundary pattern should be:

```python
from pydantic import BaseModel, ConfigDict, Field


class StrictContract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class RunCommandArgs(StrictContract):
    command: list[str] = Field(min_length=1)
    timeout: int = Field(default=15, ge=1, le=120)
    cwd: str = "."
    background: bool = False


def parse_tool_args(tool_name: str, raw: dict) -> BaseModel:
    # First perform only unambiguous host normalization, such as argv ->
    # command and timeout_ms -> timeout. Then validate strictly. Do not let
    # Pydantic silently turn "30" into 30 or accept unknown fields.
    return TOOL_ARG_MODELS[tool_name].model_validate(normalize(raw))
```

This gives the small model a precise contract while preserving a useful,
human-readable error path. Strict validation must happen after the existing
deterministic normalization layer: otherwise harmless provider shape drift
would become a hard failure. `extra="forbid"` is especially valuable because
it rejects hallucinated argument names instead of silently ignoring them.

The migration should be incremental: tool arguments first, then worker and
grader packets, followed by persisted telemetry. Do not rewrite the FSM or
the whole event ledger merely to use `BaseModel`.

## Second-pass research result: the minimal multi-file surface

The current registry is functionally capable but too broad as a single visible
surface: 16 non-network tools and about 10,000 serialized schema characters
before the completion tool. That is not automatically bad for a large model,
but it gives a 27B model too many nearby choices and increases stale-tool
replays.

The primary implementations reviewed converge on a small primitive set:

- SWE-agent documents a shell, file viewer, search, and editor as its core
  bundle.
- Claude Code exposes Read, Edit, Write, and Bash, with allow/deny filtering
  and bounded turns.
- OpenAI's current agent guidance describes shell and apply-patch as local
  execution primitives, plus conditional tool enabling and per-tool
  guardrails.

The important pattern is progressive disclosure: the model sees only the
tools needed for the current phase. This is also consistent with tool schemas
being part of the input context and therefore part of the choice and token
budget.

### Recommended actor surface

The recommended long-term surface is seven capabilities:

```text
find_files     # Glob: locate files and bounded project inventory
read_file      # Read: exact source excerpts
grep_dir       # Grep: recursive text search across files
apply_patch    # Edit: one atomic patch that may touch multiple files
write_file     # Write: create a new file; reject accidental overwrite
run_command    # Bash-equivalent: argv execution, tests, services, probes
finish_task    # Completion request, still subject to host evidence gates
```

`apply_patch` is a proposed replacement for the current single-file
`patch_file`, not an additional permanent mutation tool. It should support a
standard multi-file patch and be tested against the transaction buffer. Keeping
`write_file` distinct is useful for creating a brand-new application file, but
it should not silently rewrite an existing product file.

### Conditional tools

These should remain implemented but hidden from the normal initial prompt:

```text
process_status, stop_process  -> expose only after background=true
git_diff, git_status           -> expose during review or recovery
run_tests                      -> host shortcut or validation phase only
list_symbols                   -> expose only when structural Python lookup helps
diff_files                     -> recovery/review only; run_command can cover it
list_workspace, list_dir       -> remove from normal actor surface
search_file                    -> merge into grep_dir or expose only for a known file
web_search, fetch              -> explicit network mode only
```

This is consolidation by visibility and semantics, not by deleting useful
host capabilities. The host can still invoke specialized checks itself.

### Decision

The present set is a good internal implementation inventory, but it is not the
best default model-facing inventory. The next tool change should be a measured
replacement experiment:

1. add `apply_patch` as a multi-file-capable editor;
2. run the unchanged cascading and multi-file benchmarks;
3. compare it against the current `patch_file` surface;
4. only then remove `patch_file` from the actor surface;
5. add the phase selector so the actor normally sees 4–7 tools, not 16+.

Do not add a universal “smart repository” tool. Its broad mode parameter would
hide several behaviors behind one schema and make a small model's decisions
less predictable.
