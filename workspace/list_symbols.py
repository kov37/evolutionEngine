#!/usr/bin/env python3
"""Standalone code-structure utility: list top-level functions and classes in a Python source file."""

import ast
import os
import sys
import tempfile


def list_symbols(path: str) -> list[tuple[str, str, int]]:
    """Parse a Python source file and return top-level function/class symbols.

    Args:
        path: Filesystem path to a .py source file.

    Returns:
        List of (kind, name, lineno) tuples sorted by line number,
        where kind is 'function' or 'class'.
    """
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()

    tree = ast.parse(source, filename=path)

    symbols: list[tuple[str, str, int]] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            symbols.append(("function", node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            symbols.append(("class", node.name, node.lineno))

    symbols.sort(key=lambda t: t[2])
    return symbols


def _run_self_test() -> bool:
    """Self-test: build a tiny Python source with known symbols, verify list_symbols()."""
    # Build a temporary source file with known line numbers.
    lines = [
        "# -*- coding: utf-8 -*-",           # lineno 1
        "import os",                          # lineno 2
        "",                                   # lineno 3
        "class Foo:",                         # lineno 4
        "    pass",                           # lineno 5
        "",                                   # lineno 6
        "def bar():",                        # lineno 7
        "    return 42",                     # lineno 8
        "",                                   # lineno 9
        "class Baz:",                         # lineno 10
        "    pass",                           # lineno 11
        "",                                   # lineno 12
        "def qux():",                        # lineno 13
        "    pass",                           # lineno 14
    ]
    source = "\n".join(lines)

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".py")
        os.write(fd, source.encode("utf-8"))
        os.close(fd)

        results = list_symbols(tmp_path)

        # Expect exactly 4 symbols sorted by line number.
        assert len(results) == 4, f"Expected 4 symbols, got {len(results)}"

        # lineno 4: class Foo
        assert results[0] == ("class", "Foo", 4), f"Symbol 0: {results[0]}"

        # lineno 7: function bar
        assert results[1] == ("function", "bar", 7), f"Symbol 1: {results[1]}"

        # lineno 10: class Baz
        assert results[2] == ("class", "Baz", 10), f"Symbol 2: {results[2]}"

        # lineno 13: function qux
        assert results[3] == ("function", "qux", 13), f"Symbol 3: {results[3]}"

        print("Self-test: ALL assertions passed.")
        return True
    except (AssertionError, Exception) as exc:
        print(f"Self-test FAILED: {exc}")
        return False
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            os.unlink(tmp_path)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # No arguments — run self-test.
        _ok = _run_self_test()
        sys.exit(0 if _ok else 1)

    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-python-source>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    symbols = list_symbols(path)
    for kind, name, lineno in symbols:
        print(f"{lineno}: {kind} {name}")
    sys.exit(0)
