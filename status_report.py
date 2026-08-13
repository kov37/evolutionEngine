"""External, human-readable per-iteration progress snapshot for `tail -f`/
`watch` from a separate terminal — deliberately NOT the same thing as
structured_state.py/working_state.py (those exist to feed the MODEL its own
context back; neither persists anything to disk) or action_governor.py's
EvidenceLedger (in-memory only, dies with the process). Checked both before
adding this: no existing mechanism writes a status file.

Two files, same data, for two different watch styles:
- `<path>` — overwritten in place each call (atomic via os.replace), for
  `watch cat <path>` or any tool that re-reads the same file.
- `<path minus .json>.jsonl` — appended to each call, for `tail -f`.
"""

import ast
import json
import os
import time


def classes_with_method(file_path, class_names, method_name="_cdf"):
    """Deterministic (ast-based, no LLM/regex-on-text involved) check: which
    of `class_names`, if defined in `file_path`, declare `method_name`
    directly in their own body (inherited methods don't count — that's the
    whole point for a task like adding a precomputed _cdf per class).
    Returns {class_name: bool}; a missing file or missing class is False,
    never an error, since this runs on a live, uncertain file every turn."""
    result = {name: False for name in class_names}
    if not file_path or not os.path.exists(file_path):
        return result
    try:
        tree = ast.parse(open(file_path).read())
    except (SyntaxError, OSError):
        return result
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in result:
            result[node.name] = any(
                isinstance(n, ast.FunctionDef) and n.name == method_name
                for n in node.body
            )
    return result


def write(path, **fields):
    if not path:
        return
    fields["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(fields, f, indent=2)
    os.replace(tmp, path)
    jsonl_path = os.path.splitext(path)[0] + ".jsonl"
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(fields) + "\n")
