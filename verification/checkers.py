"""Trusted, independent behavioral checkers for the graduated curriculum
tools. Lives outside workspace/ — the sandbox root the model's write_file/
patch_file/run_shell tools are confined to — so a candidate can never read,
predict, or patch its own grading criteria.

Each checker takes the loaded candidate callable and returns (passed: bool,
detail: str). None of them trust anything the candidate's own __main__
self-test asserted — they independently construct fixtures, call the
function, and check the real return value against ground truth. That
distinction is the entire point: see VERIFICATION_ASSESSMENT.md findings
1-2 for why a candidate-authored self-test is not evidence.

CHECKERS maps curriculum "name" -> checker function; registry.verify()
looks a candidate up here by name and requires it to pass before promotion
proceeds, in addition to (not instead of) the filename/signature checks.
"""
import os
import shutil
import subprocess
import tempfile


def _safe(fn):
    """Wrap a checker body so an unexpected exception becomes a normal
    (False, detail) result instead of crashing verification outright — a
    checker crashing is itself evidence the candidate should fail, not a
    reason to blow up the promotion pipeline."""
    def wrapped(candidate):
        try:
            return fn(candidate)
        except Exception as e:
            return False, f"checker raised {type(e).__name__}: {e}"
    return wrapped


@_safe
def check_search_text(search_file):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "haystack.txt")
        lines = [f"filler line {i}" for i in range(20)]
        lines.insert(11, "here is the NEEDLE_7f3a9 marker")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")

        result = search_file("NEEDLE_7f3a9", path)
        if not isinstance(result, list) or len(result) != 1:
            return False, f"expected exactly 1 match, got {result!r}"
        lineno, text = result[0]
        if lineno != 12 or "NEEDLE_7f3a9" not in text:
            return False, f"expected (12, ...NEEDLE...), got {result[0]!r}"

        if search_file("NOT_PRESENT_XYZ", path) != []:
            return False, "expected no matches for an absent pattern"
    return True, "found the planted marker at the correct line, no false positive"


@_safe
def check_list_symbols(list_symbols):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sample.py")
        with open(path, "w") as f:
            f.write("def alpha():\n    pass\n\n\nclass Beta:\n    pass\n")

        result = list_symbols(path)
        kinds = {(kind, name): lineno for kind, name, lineno in result}
        if kinds.get(("function", "alpha")) != 1:
            return False, f"expected alpha at line 1, got {result!r}"
        if kinds.get(("class", "Beta")) != 5:
            return False, f"expected Beta at line 5, got {result!r}"
    return True, "correctly identified function and class with their line numbers"


@_safe
def check_list_dir(list_dir):
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "sub", "subsub"))
        open(os.path.join(d, "a.txt"), "w").close()
        open(os.path.join(d, "sub", "b.txt"), "w").close()
        open(os.path.join(d, "sub", "subsub", "c.txt"), "w").close()

        result = list_dir(d)
        rel_paths = {rel for _, rel in result}
        expected = {"a.txt", "sub", "sub/b.txt", "sub/subsub", "sub/subsub/c.txt"}
        if not expected.issubset(rel_paths):
            return False, f"missing entries; expected superset of {expected}, got {rel_paths}"
    return True, "correctly walked a nested directory tree"


@_safe
def check_diff_files(diff_files):
    with tempfile.TemporaryDirectory() as d:
        path_a = os.path.join(d, "a.txt")
        path_b = os.path.join(d, "b.txt")
        with open(path_a, "w") as f:
            f.write("alpha\nbeta\ngamma\n")
        with open(path_b, "w") as f:
            f.write("alpha\nbeta\ngamma\n")

        if diff_files(path_a, path_b) != "":
            return False, "identical files should produce an empty diff"

        with open(path_b, "w") as f:
            f.write("alpha\nchanged\ndelta\n")
        result = diff_files(path_a, path_b)
        if not result:
            return False, "differing files should produce a non-empty diff"
        lines = result.splitlines()
        if not any("-beta" in l for l in lines):
            return False, f"missing removed-line marker in: {result!r}"
        if not any("+changed" in l for l in lines):
            return False, f"missing changed-line marker in: {result!r}"
        if not any("+delta" in l for l in lines):
            return False, f"missing added-line marker in: {result!r}"
    return True, "correctly diffed identical and differing file pairs"


@_safe
def check_run_tests(run_tests):
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "test_sample.py"), "w") as f:
            f.write(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_pass(self):\n"
                "        self.assertEqual(1, 1)\n"
                "    def test_fail(self):\n"
                "        self.assertEqual(1, 2)\n"
            )
        success, summary = run_tests(d)
        if success is not False:
            return False, f"expected success=False for a suite with a failing test, got {success!r}"
        if not isinstance(summary, str) or not summary:
            return False, f"expected a non-empty summary string, got {summary!r}"

    with tempfile.TemporaryDirectory() as d2:
        # Different basename than the first fixture (test_sample.py) on
        # purpose: unittest's discovery imports by module name into
        # sys.modules, and a same-named file from an already-deleted temp
        # dir would otherwise collide with the cached first import.
        with open(os.path.join(d2, "test_allpass.py"), "w") as f:
            f.write(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_pass(self):\n"
                "        self.assertEqual(1, 1)\n"
            )
        success2, _ = run_tests(d2)
        if success2 is not True:
            return False, f"expected success=True for an all-passing suite, got {success2!r}"
    return True, "correctly reported both a failing and an all-passing suite"


@_safe
def check_web_search(web_search):
    if not os.environ.get("TAVILY_API_KEY"):
        return False, (
            "TAVILY_API_KEY not set — cannot independently verify behavior without making the "
            "live call. Treated as unverified, not as a pass (matching this tool's own self-test, "
            "which also refuses to exit 0 without the key)."
        )
    result = web_search("Python programming language", max_results=1)
    if not isinstance(result, str) or result.startswith("ERROR:"):
        return False, f"expected a real result string, got {result!r}"
    if "http" not in result and "url" not in result.lower():
        return False, f"result doesn't look like it contains a URL: {result!r}"
    return True, "live Tavily call returned a plausible result"


@_safe
def check_fetch(fetch):
    if not os.environ.get("FIRECRAWL_API_KEY"):
        return False, (
            "FIRECRAWL_API_KEY not set — cannot independently verify behavior without making the "
            "live call. Treated as unverified, not as a pass (matching this tool's own self-test, "
            "which also refuses to exit 0 without the key)."
        )
    result = fetch("https://example.com")
    if not isinstance(result, str) or result.startswith("ERROR:"):
        return False, f"expected page content, got {result!r}"
    if "Example Domain" not in result:
        return False, f"expected 'Example Domain' in fetched content, got: {result[:200]!r}"
    bad = fetch("file:///etc/hostname")
    if not isinstance(bad, str) or "ERROR: url must start with" not in bad:
        return False, f"expected rejection of a non-http(s) url, got {bad!r}"
    return True, "live Firecrawl call returned expected content, non-http url correctly rejected"


@_safe
def check_grep_dir(grep_dir):
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "sub", "deeper"))
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("line one\nNEEDLE_9f2c1 here\nline three\n")
        with open(os.path.join(d, "sub", "b.txt"), "w") as f:
            f.write("no marker\n")
        with open(os.path.join(d, "sub", "deeper", "c.txt"), "w") as f:
            f.write("also NEEDLE_9f2c1\n")

        result = grep_dir("NEEDLE_9f2c1", d)
        if not isinstance(result, list) or len(result) != 2:
            return False, f"expected exactly 2 matches, got {result!r}"

        if grep_dir("TOTALLY_ABSENT_PATTERN", d) != []:
            return False, "expected no matches for an absent pattern"
    return True, "found matches across nested files, no false positive"


@_safe
def check_git_status(git_status):
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)

        with open(os.path.join(d, "tracked.txt"), "w") as f:
            f.write("v1\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=d, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=d, check=True)
        with open(os.path.join(d, "tracked.txt"), "w") as f:
            f.write("v2\n")
        with open(os.path.join(d, "untracked.txt"), "w") as f:
            f.write("new\n")

        result = git_status(d)
        codes = {path: code for code, path in result}
        if "untracked.txt" not in codes or codes["untracked.txt"].strip() != "??":
            return False, f"expected untracked.txt as '??', got {result!r}"
        if "tracked.txt" not in codes or "M" not in codes["tracked.txt"]:
            return False, f"expected tracked.txt modified, got {result!r}"

    with tempfile.TemporaryDirectory() as not_repo:
        result2 = git_status(not_repo)
        if result2 != "ERROR: not a git repository.":
            return False, f"expected the exact not-a-repo error string, got {result2!r}"
    return True, "correctly classified untracked/modified files and rejected a non-repo"


@_safe
def check_git_diff(git_diff):
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)

        with open(os.path.join(d, "f.txt"), "w") as f:
            f.write("original\n")
        subprocess.run(["git", "add", "f.txt"], cwd=d, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=d, check=True)

        if git_diff(d) != "":
            return False, "expected an empty diff for a clean tree"

        with open(os.path.join(d, "f.txt"), "w") as f:
            f.write("changed\n")
        result = git_diff(d)
        if "-original" not in result or "+changed" not in result:
            return False, f"expected -/+ markers for the change, got: {result!r}"

    with tempfile.TemporaryDirectory() as not_repo:
        result2 = git_diff(not_repo)
        if not isinstance(result2, str) or not result2.startswith("ERROR:"):
            return False, f"expected an ERROR: string for a non-repo, got {result2!r}"
    return True, "correctly diffed a real change and rejected a non-repo"


CHECKERS = {
    "search_text": check_search_text,
    "list_symbols": check_list_symbols,
    "list_dir": check_list_dir,
    "diff_files": check_diff_files,
    "run_tests": check_run_tests,
    "web_search": check_web_search,
    "fetch": check_fetch,
    "grep_dir": check_grep_dir,
    "git_status": check_git_status,
    "git_diff": check_git_diff,
}
