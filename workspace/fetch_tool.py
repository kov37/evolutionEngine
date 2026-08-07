#!/usr/bin/env python3
"""Standalone page-fetching utility using the Firecrawl scrape API."""

import json
import os
import sys
import urllib.error
import urllib.request


def fetch(url: str) -> str:
    """Fetch a page's markdown content via the Firecrawl scrape API.

    Parameters
    ----------
    url : str
        The HTTP/HTTPS URL to scrape.

    Returns
    -------
    str
        The extracted markdown content on success, or an error message
        starting with "ERROR:" on failure.
    """
    # --- Validate API key -----------------------------------------------
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return "ERROR: FIRECRAWL_API_KEY environment variable is not set."

    # --- Validate URL scheme --------------------------------------------
    if not (url.startswith("http://") or url.startswith("https://")):
        return "ERROR: url must start with http:// or https://"

    # --- Build and send the POST request --------------------------------
    payload = json.dumps({"url": url, "formats": ["markdown"]}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.firecrawl.dev/v2/scrape",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return f"ERROR: HTTP {exc.code} – {exc.reason}"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", str(exc))
        return f"ERROR: URL error – {reason}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"

    if status != 200:
        return f"ERROR: HTTP {status} – expected 200"

    # --- Parse and extract markdown -------------------------------------
    try:
        resp_json = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        return f"ERROR: Failed to parse JSON response – {exc}"

    if not isinstance(resp_json, dict):
        return "ERROR: Unexpected response format (not a JSON object)"

    if resp_json.get("success") is False:
        # Try to grab an error detail from the API if available
        err_detail = resp_json.get("error", "")
        msg = "ERROR: Firecrawl reported failure"
        if err_detail:
            msg += f" – {err_detail}"
        return msg

    data = resp_json.get("data")
    if not isinstance(data, dict):
        return "ERROR: Missing or invalid 'data' object in response"

    markdown = data.get("markdown")
    if markdown is None:
        return "ERROR: No 'markdown' field found in response data"

    return str(markdown)


def _run_self_test() -> bool:
    """Run the internal self-test. Returns True on success, False otherwise."""
    passed = 0
    total = 0

    def _assert(condition: bool, message: str) -> None:
        nonlocal passed, total
        total += 1
        if condition:
            passed += 1
        else:
            print(f"FAIL: {message}", file=sys.stderr)

    # --- Check that FIRECRAWL_API_KEY is set in the environment -------
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    _assert(api_key is not None, "FIRECRAWL_API_KEY must be set for self-test")

    if api_key is None:
        print(
            "Self-test requires FIRECRAWL_API_KEY to be set in the environment.",
            file=sys.stderr,
        )
        return False

    # --- Test 1: valid URL fetch via real API call ---------------------
    result = fetch("https://example.com")
    _assert(not result.startswith("ERROR:"), "fetch(example.com) must not start with ERROR:")
    _assert("Example Domain" in result, 'fetch(example.com) must contain "Example Domain"')

    # --- Test 2: invalid URL scheme is rejected ------------------------
    result_bad = fetch("file:///etc/hostname")
    _assert(
        result_bad.startswith("ERROR: url must start with"),
        "fetch(file://…) must return the expected error string",
    )

    return passed == total


def main() -> int:
    """Entry point when run as a script."""
    if len(sys.argv) < 2:
        # No URL provided → run self-test
        try:
            ok = _run_self_test()
        except Exception as exc:
            print(f"Self-test raised {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0 if ok else 1

    url = sys.argv[1]
    result = fetch(url)
    print(result, end="\n")

    # Exit 0 on success; exit 2 on error
    if result.startswith("ERROR:"):
        print(result, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
