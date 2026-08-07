"""Ordered bootstrapping goals for harness.py. Each entry is a standalone
goal fed to the model with the full current tool registry available.

patch_validator was retired — it duplicated kernel/io_tools.py::patch_file,
which already does exact-match search/replace with syntax validation. No
point having the model re-solve something already hardened in the kernel.

`function_name` + `description` are what harness.py hands to
registry.verify_and_promote() after a goal wins — that's what turns a
proven workspace script into a permanent tool for every goal after it.

`path_params` names which of the tool's parameters are filesystem paths.
registry.py uses this to sandbox those arguments to os.getcwd() at call
time — the tool's own code doesn't implement confinement itself; that's a
trusted-code concern, not something to ask the model to get right per-tool.
"""

GRADUATION_CONTRACT = """
GRADUATION CONTRACT (required):
- Running the script directly with zero command-line arguments must execute
  an internal self-test and exit with code 0 only if every assertion in it
  passes, non-zero otherwise. This is the only verification the harness
  runs — there is no separate test runner.
- Expose the core logic as a plain, directly-callable function (not just
  reachable via __main__), with exactly the name and signature specified
  below, so it can be reused as a tool by future goals.
"""

CURRICULUM = [
    {
        "name": "search_text",
        "function_name": "search_file",
        "path_params": ["filepath"],
        "description": "Search a file line-by-line for a substring pattern; returns (line_number, line_text) for every match.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone text-search utility named `search_text.py`.

        It must be a self-contained Python script (no third-party dependencies) that:
        - Accepts a search pattern and a target file path.
        - Scans the target file line by line and finds every line containing the pattern.
        - Reports each match as its 1-indexed line number and the line's content.
        - Exits with code 0 if the pattern is found at least once, and a non-zero code if it is not found.
        - Exposes the core logic as `search_file(pattern: str, filepath: str) -> list[tuple[int, str]]`.

        {GRADUATION_CONTRACT}

        Self-test scenario: generate a temporary multi-line text file containing a distinctive marker string (e.g. 'NEEDLE_7f3a9') planted on a known line among plenty of unrelated filler lines, call search_file on it, assert the correct line number was reported, clean up the temp file.
        """,
    },
    {
        "name": "list_symbols",
        "function_name": "list_symbols",
        "path_params": ["path"],
        "description": "Parse a Python file's AST and list every top-level function/class definition with its kind and line number.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone code-structure utility named `list_symbols.py`.

        It must be a self-contained Python script (standard library only — use the `ast` module) that:
        - Accepts a path to a Python source file.
        - Parses it and finds every top-level function definition (`def`) and class definition (`class`).
        - For each, determines its kind ('function' or 'class'), its name, and its 1-indexed line number.
        - Exposes this as `list_symbols(path: str) -> list[tuple[str, str, int]]`, returning (kind, name, lineno) tuples sorted by line number.
        - When run as a script with a file path argument, prints one line per symbol in the form `<lineno>: <kind> <name>`, and exits 0.

        {GRADUATION_CONTRACT}

        Self-test scenario: write a small temporary Python source string to a temp file containing at least one top-level function and one top-level class at known line numbers, call list_symbols on it, assert the correct kind/name/line for each, clean up the temp file.
        """,
    },
    {
        "name": "list_dir",
        "function_name": "list_dir",
        "path_params": ["root"],
        "description": "Recursively list every file and subdirectory beneath a root path, as (kind, relative_path) tuples.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone recursive directory-listing utility named `list_dir_tool.py`.

        Unlike a flat single-directory listing, it must walk an entire directory tree. It must be a self-contained Python script (standard library only) that:
        - Accepts a root directory path.
        - Recursively walks every file and subdirectory beneath that root.
        - For each entry, determines its kind ('file' or 'dir') and its path relative to the root, using forward slashes regardless of platform.
        - Exposes this as `list_dir(root: str) -> list[tuple[str, str]]`, returning (kind, relative_path) tuples sorted alphabetically by relative_path.
        - When run as a script with a root path argument, prints one line per entry in the form `<kind>: <relative_path>`, and exits 0. If the root does not exist, prints an error to stderr and exits non-zero.

        {GRADUATION_CONTRACT}

        Self-test scenario: create a temporary directory tree containing at least one nested subdirectory and several files at different depths (e.g. root/a.txt, root/sub/b.txt, root/sub/subsub/c.txt), call list_dir on the temp root, assert every expected entry is present with the correct kind and relative path, clean up the temp tree afterward.
        """,
    },
    {
        "name": "diff_files",
        "function_name": "diff_files",
        "path_params": ["path_a", "path_b"],
        "description": "Compute a unified diff between two files, as a string, so a change can be inspected before or after it's applied.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone file-comparison utility named `diff_files_tool.py`.

        It must be a self-contained Python script (standard library only — use the `difflib` module) that:
        - Accepts two file paths, A and B.
        - Computes a unified diff between them (contextual +/- line format, like `diff -u`).
        - Exposes this as `diff_files(path_a: str, path_b: str) -> str`, returning the unified diff as a single string. If the files are identical, return an empty string.
        - When run as a script with two file path arguments: print the diff (if any) and exit 0 if the files are identical, exit 1 if they differ but both were read successfully, and exit 2 if either path does not exist (printing an error to stderr in that case).

        {GRADUATION_CONTRACT}

        Self-test scenario: create two temporary files — one pair that are byte-for-byte identical, and one pair that differ by at least one changed line, one added line, and one removed line. Call diff_files on the identical pair and assert the result is an empty string. Call it on the differing pair and assert the returned diff string contains the expected `-`/`+` markers for each change. Clean up all temp files afterward.
        """,
    },
    {
        "name": "run_tests",
        "function_name": "run_tests",
        "path_params": ["path"],
        "description": "Discover and run a unittest test suite under a directory, returning (success, summary) instead of raw subprocess text a caller has to parse.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone test-runner utility named `run_tests_tool.py`.

        It must be a self-contained Python script (standard library only) that:
        - Accepts a directory path to discover tests under.
        - Uses the `unittest` module's TestLoader/TestRunner APIs directly (loader.discover(...) + a runner that produces a TestResult object) to run every test in that directory — do NOT shell out to `pytest` or parse subprocess text output; read the pass/fail counts straight off the TestResult object.
        - Exposes this as `run_tests(path: str = ".") -> tuple[bool, str]`, where the first element is True only if every discovered test passed (and at least one test ran), and the second element is a short human-readable summary, e.g. "Ran 5 tests: 4 passed, 1 failed, 0 errors".
        - When run as a script with an optional directory argument (default "."), prints the summary and exits 0 if success is True, 1 if any test failed or errored, and 2 if no tests were discovered at all.

        {GRADUATION_CONTRACT}

        Self-test scenario: in a temp directory, write one throwaway unittest test module containing at least one test that passes and one that intentionally fails (e.g. `assert 1 == 2`) using `unittest.TestCase`. Call run_tests on that temp directory and assert: the returned success is False (because a test in it failed — this is run_tests correctly reporting the failure, not a bug), and the summary mentions both a passed and a failed count. Then write a second temp directory containing only passing tests, call run_tests on it, and assert success is True. Clean up both temp directories afterward. The overall self-test itself must still exit 0 — you are asserting that run_tests reports failures correctly, not requiring the inner throwaway suite to pass.
        """,
    },
]
