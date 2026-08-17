"""Strict host-side contracts for model-facing tool calls.

The provider schema is guidance (the shape shown to the model).  This module
is enforcement (the host rejects a call before it reaches a tool).  Pydantic
v2 gives us one typed definition with ``extra='forbid'`` so stale arguments
cannot silently pass through to a Python function.
"""

from __future__ import annotations

import inspect
from typing import Any, Type

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError


class StrictToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PatchFileArgs(StrictToolArgs):
    path: StrictStr = Field(min_length=1, description="Workspace-relative path to an existing file.")
    find_exact_block: StrictStr = Field(description="Exact current block to replace, including indentation.")
    replace_with_block: StrictStr = Field(description="Replacement block to write into the file.")


class EditSpecArgs(StrictToolArgs):
    start_line: int = Field(ge=1, description="First line to replace (1-based, inclusive).")
    end_line: int = Field(ge=1, description="Last line to replace (1-based, inclusive).")
    replacement: StrictStr = Field(description="Text that replaces the line range.")
    expect: StrictStr | None = Field(
        default=None,
        description="Optional short token that must appear inside the range; "
                    "the batch is rejected with the actual region if it does not.",
    )


class EditRangeArgs(StrictToolArgs):
    path: StrictStr = Field(min_length=1, description="Workspace-relative path to an existing file.")
    edits: list[EditSpecArgs] = Field(
        min_length=1, max_length=8,
        description="Batched line-range replacements, applied bottom-up atomically.",
    )
    snapshot: StrictStr | None = Field(
        default=None,
        description="Optional [snapshot ...] token from a read_file header; the "
                    "batch is rejected if the file changed since that read.",
    )


class WriteFileArgs(StrictToolArgs):
    path: StrictStr = Field(min_length=1, description="Workspace-relative path for a new file only.")
    content: StrictStr = Field(description="Complete contents of the new file.")


class ReadFileArgs(StrictToolArgs):
    path: StrictStr = Field(min_length=1, description="Workspace-relative path to an existing file.")
    offset: StrictInt | None = Field(default=1, ge=1, description="1-based line to start reading from.")
    limit: StrictInt | None = Field(default=None, ge=1, description="Maximum lines to return; omit for EOF.")


class FindFilesArgs(StrictToolArgs):
    pattern: StrictStr | None = Field(default="*", description="Glob pattern; **/ searches recursively.")
    path: StrictStr | None = Field(default=".", description="Workspace-relative directory to search.")
    max_results: StrictInt | None = Field(default=200, ge=1, le=200, description="Maximum results to return.")


class RunCommandArgs(StrictToolArgs):
    command: list[StrictStr] = Field(min_length=1, description="Executable argv list; no shell syntax.")
    timeout: StrictInt = Field(default=15, ge=1, le=120, description="Maximum runtime in seconds.")
    cwd: StrictStr = Field(default=".", description="Workspace-relative working directory.")
    background: StrictBool = Field(default=False, description="Start a managed background process.")


class ProcessStatusArgs(StrictToolArgs):
    handle: StrictStr = Field(min_length=1, description="Managed process handle.")
    tail_chars: StrictInt | None = Field(default=3000, ge=1, le=4000, description="Maximum log characters.")


class StopProcessArgs(StrictToolArgs):
    handle: StrictStr = Field(min_length=1, description="Managed process handle to stop.")


class PathOnlyArgs(StrictToolArgs):
    path: StrictStr = Field(default=".", description="Workspace-relative target path.")


CONTRACT_MODELS: dict[str, Type[BaseModel]] = {
    "patch_file": PatchFileArgs,
    "edit_range": EditRangeArgs,
    "write_file": WriteFileArgs,
    "read_file": ReadFileArgs,
    "find_files": FindFilesArgs,
    "run_command": RunCommandArgs,
    "process_status": ProcessStatusArgs,
    "stop_process": StopProcessArgs,
    "git_status": PathOnlyArgs,
    "git_diff": PathOnlyArgs,
    "run_tests": PathOnlyArgs,
}


def _signature_fields(fn) -> tuple[set[str], set[str]]:
    signature = inspect.signature(fn)
    parameters = {
        name: parameter for name, parameter in signature.parameters.items()
        if parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
    }
    required = {
        name for name, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    return set(parameters), required


def validate_tool_arguments(tool_name: str, arguments: Any, fn=None) -> dict[str, Any]:
    """Validate and normalize one tool call, raising a readable ValueError.

    Known kernel tools use strict Pydantic models.  Graduated tools still get
    deterministic unknown-field and missing-required-field checks from their
    Python signature, without importing arbitrary model-authored code into the
    validation layer.
    """
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")

    model = CONTRACT_MODELS.get(tool_name)
    if model is not None:
        try:
            return model.model_validate(arguments).model_dump()
        except ValidationError as exc:
            extra = sorted(
                str(error["loc"][-1]) for error in exc.errors()
                if error["type"] == "extra_forbidden"
            )
            if extra:
                raise ValueError(
                    f"invalid {tool_name} arguments (unexpected fields: {', '.join(extra)})"
                ) from None
            detail = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise ValueError(f"invalid {tool_name} arguments ({detail})") from None

    if fn is None:
        return dict(arguments)
    allowed, required = _signature_fields(fn)
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"invalid {tool_name} arguments (unexpected fields: {', '.join(unknown)})")
    missing = sorted(required - set(arguments))
    if missing:
        raise ValueError(f"invalid {tool_name} arguments (missing fields: {', '.join(missing)})")
    return dict(arguments)


def schema_for_tool(fn) -> dict[str, Any]:
    """Return a strict OpenAI-compatible schema for llama.cpp or inspection."""
    model = CONTRACT_MODELS.get(fn.__name__)
    if model is not None:
        parameters = model.model_json_schema()
    else:
        from ollama import _utils
        import json
        parameters = json.loads(
            _utils.convert_function_to_tool(fn).model_dump_json(exclude_none=True)
        )["function"]["parameters"]
        parameters["additionalProperties"] = False
    parameters["additionalProperties"] = False
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": (inspect.getdoc(fn) or "").split("\n", 1)[0],
            "parameters": parameters,
        },
    }
