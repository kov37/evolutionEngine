# NoveltyEngine tool contracts

This is the current model-facing contract inventory. “Required” means the
model must provide the field. Defaults are applied by the host/tool when the
field is omitted. Pydantic v2 models in `tool_contracts.py` enforce the
boundary at dispatch time with strict types and `extra="forbid"`.

`patch_file` is the only model-facing editor. `apply_patch`, directory-listing
tools, and overlapping search tools remain available only to host-side
historical code; they are not loaded into the model registry.

The host confines workspace paths and converts tool failures into readable
`ERROR:` or `REJECTED:` results. These are behavior contracts, not claims that
the model is authoritative: validation and completion remain host-controlled.

## Core tools

### `read_file`

```json
{
  "path": "string, required, workspace-relative existing file",
  "offset": "integer, optional, 1-based start line; default 1",
  "limit": "integer, optional, maximum lines; omitted means through EOF"
}
```

Reads exact current file text. It is the normal prerequisite for an edit.
It does not summarize or change the file.

### `write_file`

```json
{
  "path": "string, required, workspace-relative path",
  "content": "string, required, complete file contents"
}
```

Creates a new file. If the target already exists, the host rejects the call
and directs the model to `patch_file`. This prevents accidental full-file
replacement of correct code.

### `patch_file`

```json
{
  "path": "string, required, one existing workspace file",
  "find_exact_block": "string, required, current block including indentation",
  "replace_with_block": "string, required, replacement text"
}
```

Replaces one block in one existing file. The model should call `read_file`
first. The host rejects a missing/stale block and does not create a file.
Trailing whitespace and line-ending differences are tolerated by the current
implementation; Python indentation is not silently changed. Legacy argument
names are rejected by the host contract validator.

### `find_files`

```json
{
  "pattern": "string, optional glob; default *",
  "path": "string, optional workspace-relative directory; default .",
  "max_results": "integer, optional, capped at 200; default 200"
}
```

Bounded recursive discovery. It skips common dependency/build directories,
supports `**/` as zero-or-more directories, sorts results, and reports when
the cap is reached.

### `run_command`

```json
{
  "command": "array of strings, required, executable argv",
  "timeout": "integer seconds, optional, default 15, capped at 120",
  "cwd": "string, optional workspace-relative directory; default .",
  "background": "boolean, optional; default false"
}
```

Runs without a shell, so pipes, redirects, and shell metacharacters are not
interpreted. A background call returns a host process handle. Output is
bounded. This is the general execution primitive for tests, probes, and
services.

### `process_status`

```json
{
  "handle": "string, required, handle returned by run_command",
  "tail_chars": "integer, optional, default 3000, capped at 4000"
}
```

Reads the state and recent output of a process started by this agent.

### `stop_process`

```json
{
  "handle": "string, required, managed process handle"
}
```

Stops the managed process and its descendants. It cannot stop an arbitrary
external PID by design.

### `finish_task`

```json
{
  "summary": "string, required, short completion summary"
}
```

Requests completion. It does not prove success and does not directly stop the
agent. The host accepts it only after independent behavioral evidence passes.

## Conditional review and validation tools

These are implemented and revealed only in the lifecycle phase where they are
useful. The FSM (host state machine) changes the visible list; dispatch also
rejects calls to tools remembered from earlier turns.

### `run_tests`

```json
{
  "path": "string, optional, test file or directory; default ."
}
```

Runs the trusted test harness. A passing result is validation evidence; a
service start or health-only check is setup evidence and is not completion.

### `list_symbols`

```json
{
  "path": "string, required, Python source file"
}
```

Returns top-level Python classes/functions and line numbers. It is structural
orientation, not behavioral validation.

### `diff_files`

```json
{
  "path_a": "string, required",
  "path_b": "string, required"
}
```

Produces a comparison between two files. It is a review/recovery tool, not a
mutation tool.

### `git_status`

```json
{
  "path": "string, declared default ."
}
```

Reports repository status. Current generated schema handling marks this
field as optional with a default of `.`; the host validates it as a
workspace-relative path.

### `git_diff`

```json
{
  "path": "string, declared default ."
}
```

Reports repository diff. Like `git_status`, it is review evidence and should
be phase-gated.

## Prompt and phase policy

The system prompt is intentionally short. It does not require the model to
manually print JSON or to explore on every task. Native tool calls are already
structured. The model should inspect a target when its current contents are
unknown, then act on supplied evidence.

- Exploration exposes `find_files`, `read_file`, and `list_symbols` when needed.
- Mutation exposes `patch_file`, new-file `write_file`, and permitted commands.
- Verification exposes the task's validation tools and completion request.
- The host, not the prompt, enforces these phases and rejects stale tool names.
- `run_command` uses an argv list; the host classifies commands as inspection,
  validation, or mutation before lifecycle policy allows them.
- Completion requires the task's validation contract to produce independent
  passing evidence; a health-only check is not enough.

## Internal or network surfaces

`run_shell` remains available to internal callers but is not in the normal
registry because `run_command` provides the less ambiguous argv contract.
`web_search` and `fetch` are excluded when network mode is disabled and should
be explicitly enabled only for tasks that need them. Graduated tools loaded
from `state/registry_manifest.json` are conditional and must pass their own
promotion checks before appearing.

## Contract review priorities

1. Keep `patch_file` as the default until a contract-specific A/B shows a
   benefit.
2. Make `write_file` explicitly new-file-only for the normal actor surface;
   retain a separate host-controlled full-rewrite path only when needed.
3. Fix the `git_status`/`git_diff` optional-path schema mismatch.
4. Hide overlapping inventory/search/review tools by lifecycle phase rather
   than deleting their trusted implementations.
5. Add strict boundary validation (`extra="forbid"`, strict types) after the
   host's narrow compatibility normalization.
