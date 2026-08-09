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

`target_filename` + `expected_params` + `checker_name` (see
VERIFICATION_ASSESSMENT.md findings 1-2, 3, 6) are what registry.verify()
now checks a candidate against, instead of accepting "some file exited 0
and defines a callable with the right name" as proof. `checker_name` is a
key into verification/checkers.py's CHECKERS dict — a module outside
workspace/ the model's tools can never reach — and registry.verify() runs
it, in a fresh subprocess (never in-process), as an independent behavioral
test against the candidate's actual function, ignoring whatever the
candidate's own self-test asserted about itself. A NAME rather than the
live checker function is what's threaded through: passing an actual
function object here would mean it has to cross into the verification
subprocess somehow, which defeats the point — the subprocess looks
checkers up from its own trusted import instead. The self-test in
GRADUATION_CONTRACT below remains useful as fast local dev feedback for
the model while it iterates, but it is no longer what promotion is
decided on.
"""

from verification.checkers import CHECKERS  # only to validate entries below at import time

GRADUATION_CONTRACT = """
GRADUATION CONTRACT (required):
- Running the script directly with zero command-line arguments must execute
  an internal self-test and exit with code 0 only if every assertion in it
  passes, non-zero otherwise. This is fast local feedback while you iterate —
  it is NOT what promotion is decided on; a trusted, independent checker
  outside your control verifies the actual behavior separately afterward.
- Expose the core logic as a plain, directly-callable function (not just
  reachable via __main__), with exactly the name and signature specified
  below, so it can be reused as a tool by future goals.
"""

CURRICULUM = [
    {
        "name": "search_text",
        "function_name": "search_file",
        "target_filename": "search_text.py",
        "expected_params": ["pattern", "filepath"],
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
        "target_filename": "list_symbols.py",
        "expected_params": ["path"],
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
        "target_filename": "list_dir_tool.py",
        "expected_params": ["path"],
        "path_params": ["path"],
        "description": "Recursively list every file and subdirectory beneath a root path, as (kind, relative_path) tuples.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone recursive directory-listing utility named `list_dir_tool.py`.

        Unlike a flat single-directory listing, it must walk an entire directory tree. It must be a self-contained Python script (standard library only) that:
        - Accepts a root directory path.
        - Recursively walks every file and subdirectory beneath that root.
        - For each entry, determines its kind ('file' or 'dir') and its path relative to the root, using forward slashes regardless of platform.
        - Exposes this as `list_dir(path: str) -> list[tuple[str, str]]`, returning (kind, relative_path) tuples sorted alphabetically by relative_path.
        - When run as a script with a root path argument, prints one line per entry in the form `<kind>: <relative_path>`, and exits 0. If the root does not exist, prints an error to stderr and exits non-zero.

        {GRADUATION_CONTRACT}

        Self-test scenario: create a temporary directory tree containing at least one nested subdirectory and several files at different depths (e.g. root/a.txt, root/sub/b.txt, root/sub/subsub/c.txt), call list_dir on the temp root, assert every expected entry is present with the correct kind and relative path, clean up the temp tree afterward.
        """,
    },
    {
        "name": "diff_files",
        "function_name": "diff_files",
        "target_filename": "diff_files_tool.py",
        "expected_params": ["path_a", "path_b"],
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
        "target_filename": "run_tests_tool.py",
        "expected_params": ["path"],
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
    {
        "name": "web_search",
        "function_name": "web_search",
        "target_filename": "web_search_tool.py",
        "expected_params": ["query", "max_results"],
        "description": "Search the live web via the Tavily API and return the top results (title, url, short content snippet) as a formatted string.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone web-search utility named `web_search_tool.py`, calling the Tavily Search API.

        It must be a self-contained Python script (standard library only — use `urllib.request`, do NOT depend on the `tavily-python` package or any other third-party library) that:
        - Reads the API key from the environment variable `TAVILY_API_KEY` at call time. NEVER hardcode a literal API key anywhere in the file — if the environment variable is not set, return the string "ERROR: TAVILY_API_KEY environment variable is not set." and make no network request.
        - Sends a POST request to `https://api.tavily.com/search` with header `Authorization: Bearer <the key>` and a JSON body `{{"query": <the query>, "max_results": <n>}}`.
        - The response body is JSON with a top-level `results` array; each entry has `title`, `url`, and `content` string fields.
        - Exposes this as `web_search(query: str, max_results: int = 5) -> str`, returning a formatted string with one block per result showing its title, url, and content snippet — or the literal string "No results found." if the results array is empty.
        - On any network error or a non-200 HTTP response, return a string starting with "ERROR:" describing what went wrong — do not raise an uncaught exception.
        - When run as a script with a query argument, print the formatted results and exit 0. If TAVILY_API_KEY is missing, print the same error to stderr and exit 2.

        {GRADUATION_CONTRACT}

        Self-test scenario: THIS SELF-TEST MAKES ONE REAL, LIVE CALL TO THE TAVILY API — it is not a mock and costs real API quota, so make exactly one query and keep max_results small (e.g. 1). First check that TAVILY_API_KEY is set in the environment; if it is not, print a clear message explaining the self-test requires it and exit 1 immediately without attempting a request. If it is set, call web_search with a simple, unambiguous query (e.g. "Python programming language") and max_results=1, and assert: the call did not return a string starting with "ERROR:", and the returned string contains a "url" or "http" substring, confirming a real result came back — do not assert on the exact content of a live search result, since that is not deterministic.
        """,
    },
    {
        "name": "fetch",
        "function_name": "fetch",
        "target_filename": "fetch_tool.py",
        "expected_params": ["url"],
        "description": "Retrieve the full content of one known URL (as markdown) via the Firecrawl API — the retrieval counterpart to web_search's discovery.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone page-fetching utility named `fetch_tool.py`, calling the Firecrawl scrape API.

        This is the retrieval counterpart to web_search: web_search discovers candidate URLs from a query and returns short snippets; fetch takes one already-known URL and returns that page's actual content in full.

        It must be a self-contained Python script (standard library only — use `urllib.request`, do NOT depend on the `firecrawl-py` package or any other third-party library) that:
        - Reads the API key from the environment variable `FIRECRAWL_API_KEY` at call time. NEVER hardcode a literal API key anywhere in the file — if the environment variable is not set, return the string "ERROR: FIRECRAWL_API_KEY environment variable is not set." and make no network request.
        - Rejects any url argument that does not start with "http://" or "https://" with "ERROR: url must start with http:// or https://" and makes no network request in that case either.
        - Sends a POST request to `https://api.firecrawl.dev/v2/scrape` with header `Authorization: Bearer <the key>` and a JSON body `{{"url": <the target url>, "formats": ["markdown"]}}`.
        - The response body is JSON with a top-level `success` boolean and, on success, a `data` object containing a `markdown` string field (the page's extracted content) and a `metadata` object (which may include a `title`).
        - Exposes this as `fetch(url: str) -> str`, returning the markdown content string on success. If `success` is false or the HTTP status is not 200, return a string starting with "ERROR:" describing what went wrong — do not raise an uncaught exception.
        - When run as a script with a url argument, print the markdown content and exit 0. If FIRECRAWL_API_KEY is missing or the url is invalid, print the same error to stderr and exit 2.

        {GRADUATION_CONTRACT}

        Self-test scenario: THIS SELF-TEST MAKES ONE REAL, LIVE CALL TO THE FIRECRAWL API — it is not a mock and costs real API quota, so make exactly one call. First check that FIRECRAWL_API_KEY is set in the environment; if it is not, print a clear message explaining the self-test requires it and exit 1 immediately without attempting a request. If it is set, call fetch("https://example.com") — this is IANA's permanent, unchanging placeholder page, so the result is deterministic. Assert: the call did not return a string starting with "ERROR:", and the returned string contains the substring "Example Domain" (the fixed, known content of that specific page). Also assert that calling fetch with a non-http(s) url (e.g. "file:///etc/hostname") returns the "ERROR: url must start with" string rather than attempting a request.
        """,
    },
    {
        "name": "grep_dir",
        "function_name": "grep_dir",
        "target_filename": "grep_dir_tool.py",
        "expected_params": ["pattern", "path"],
        "path_params": ["path"],
        "description": "Recursively search every file under a directory for a substring pattern, returning (relative_path, line_number, line_text) for every match.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone recursive-search utility named `grep_dir_tool.py` — the multi-file counterpart to the existing single-file search_file tool.

        It must be a self-contained Python script (standard library only) that:
        - Accepts a search pattern and a root directory (default ".").
        - Recursively walks every file under that root, skipping any directory named ".git" entirely.
        - For each file, attempts to read it as UTF-8 text; if that raises UnicodeDecodeError (a binary file), skip that file silently rather than erroring.
        - Within each readable file, finds every line containing the pattern, and records (path relative to root, using forward slashes, 1-indexed line number, line content).
        - Caps the total number of matches returned at 200 — if more than 200 matches exist, return only the first 200 and append one final entry noting how many were omitted, so a huge hit count doesn't flood the caller.
        - Exposes this as `grep_dir(pattern: str, path: str = ".") -> list[tuple[str, int, str]]`.
        - When run as a script with a pattern and optional root argument, prints one line per match in the form `<relative_path>:<line_number>: <line_text>`, and exits 0 if at least one match was found, 1 otherwise.

        {GRADUATION_CONTRACT}

        Self-test scenario: create a temporary directory tree with at least three files across at least two levels of nesting (e.g. root/a.txt, root/sub/b.txt, root/sub/deeper/c.txt), plant a distinctive marker string (e.g. 'NEEDLE_9f2c1') in two of the three files at known lines, and leave the third file without the marker. Call grep_dir on the temp root and assert: exactly two matches were found, each with the correct relative path and line number. Also assert a pattern known not to exist anywhere returns an empty list rather than erroring. Clean up the temp tree afterward.
        """,
    },
    {
        "name": "git_status",
        "function_name": "git_status",
        "target_filename": "git_status_tool.py",
        "expected_params": ["path"],
        "path_params": ["path"],
        "description": "List every changed/untracked/staged file in a git working tree as (status_code, path) pairs, via `git status --porcelain=v1`.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone git-status utility named `git_status_tool.py`. This shells out to the real `git` binary via subprocess — do not reimplement git's diff/status logic in Python, git already provides a stable machine-readable interface for exactly this.

        It must be a self-contained Python script (standard library only — use `subprocess`) that:
        - Accepts a root directory (default ".").
        - Runs `git status --porcelain=v1` with that directory as the subprocess `cwd`.
        - If the command fails because the directory is not inside a git working tree (git's stderr will say so, or the exit code is non-zero for that reason), return the string "ERROR: not a git repository." rather than raising.
        - Otherwise, parses each line of the porcelain output into a (status_code, path) tuple — the status_code is the two-character code at the start of each line (e.g. " M", "??", "A "), and the path is the rest of the line after the code and the following space, with surrounding whitespace stripped.
        - Exposes this as `git_status(path: str = ".") -> list[tuple[str, str]]`, returning an empty list if there are no changes (a clean working tree is not an error).
        - When run as a script with an optional root argument, prints one line per entry as `<status_code> <path>`, and exits 0 whether or not there are changes (a clean tree is success, not failure) — exits 2 only if root is not a git repository.

        {GRADUATION_CONTRACT}

        Self-test scenario: in a temporary directory, run `git init` (via subprocess, capturing/discarding its output), create one file and leave it untracked, `git add` and commit a second file then modify it afterward so it shows as modified. Call git_status on that temp directory and assert: the untracked file appears with status "??", the modified file appears with a status containing "M", and no other unexpected entries are present. Also assert that calling git_status on a plain temp directory that was never `git init`-ed returns the "ERROR: not a git repository." string. Clean up the temp directory afterward.
        """,
    },
    {
        "name": "git_diff",
        "function_name": "git_diff",
        "target_filename": "git_diff_tool.py",
        "expected_params": ["path"],
        "path_params": ["path"],
        "description": "Return the full diff of a git working tree against HEAD (staged and unstaged combined) as a string, via `git diff HEAD`.",
        "goal": f"""
        CRITICAL OBJECTIVE: Build a standalone git-diff utility named `git_diff_tool.py`. This shells out to the real `git` binary via subprocess — do not reimplement diff logic in Python.

        It must be a self-contained Python script (standard library only — use `subprocess`) that:
        - Accepts a root directory (default ".").
        - Runs `git diff HEAD` with that directory as the subprocess `cwd` (this shows staged and unstaged changes combined against the last commit). Pass `--no-color` explicitly so output is never contaminated with ANSI escape codes regardless of the user's git config.
        - If the command fails because the directory is not inside a git working tree, or there is no HEAD commit yet (a brand new repo with no commits), return a string starting with "ERROR:" describing which of those it was, rather than raising.
        - Exposes this as `git_diff(path: str = ".") -> str`, returning the raw diff text — an empty string means there are no differences from HEAD (that is success, not an error).
        - When run as a script with an optional root argument, prints the diff (if any) and exits 0 whether or not there are changes — exits 2 only on the error conditions above.

        {GRADUATION_CONTRACT}

        Self-test scenario: in a temporary directory, `git init` it (via subprocess), create and commit one file, then modify that file's content. Call git_diff on the temp directory and assert the returned string is non-empty and contains a `-` line with the original content and a `+` line with the new content. Then, separately, revert the file back to its committed content (or use a second commit-and-no-further-edits directory) and call git_diff again, asserting the result is an empty string. Also assert that calling git_diff on a directory that was never `git init`-ed returns a string starting with "ERROR:". Clean up the temp directory afterward.
        """,
    },
]

for _entry in CURRICULUM:
    _entry["checker_name"] = _entry["name"] if _entry["name"] in CHECKERS else None
    if _entry.get("target_filename") and _entry["checker_name"] is None:
        raise RuntimeError(f"curriculum entry '{_entry['name']}' has a target_filename but no matching checker")
del _entry
