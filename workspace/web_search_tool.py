#!/usr/bin/env python3
"""Web search utility powered by the Tavily Search API.

Standard-library-only implementation using urllib.request.
Exposes web_search(query: str, max_results: int = 5) -> str.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using the Tavily Search API and return formatted results.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return (default 5).

    Returns:
        A formatted string with one block per result showing title, url,
        and content — or "No results found." if the API returned an empty
        array. On any network error or non-200 HTTP response, returns a
        string starting with "ERROR:".
    """

    # Read API key from environment variable (never hardcode one).
    api_key = os.environ.get("TAVILY_API_KEY")
    if api_key is None:
        return "ERROR: TAVILY_API_KEY environment variable is not set."

    url = "https://api.tavily.com/search"

    payload = json.dumps({"query": query, "max_results": max_results}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            if status != 200:
                return f"ERROR: HTTP {status} from Tavily API."
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return f"ERROR: HTTP {exc.code} from Tavily API: {exc.reason}"
    except urllib.error.URLError as exc:
        return f"ERROR: Failed to connect to Tavily API: {exc.reason}"
    except Exception as exc:
        return f"ERROR: Unexpected error: {exc}"

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        return f"ERROR: Failed to parse JSON response: {exc}"

    results = data.get("results", [])
    if not results:
        return "No results found."

    lines = []
    for i, entry in enumerate(results, start=1):
        title = entry.get("title", "")
        url_val = entry.get("url", "")
        content = entry.get("content", "")
        lines.append(f"Result {i}:")
        lines.append(f"  Title:   {title}")
        lines.append(f"  URL:     {url_val}")
        lines.append(f"  Content: {content}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test executed when run as a standalone script with no arguments.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # No command-line arguments → run self-test
    if len(sys.argv) < 2:
        api_key = os.environ.get("TAVILY_API_KEY")
        if api_key is None:
            print(
                "SELF-TEST SKIPPED: TAVILY_API_KEY environment variable is not set.",
                file=sys.stderr,
            )
            sys.exit(1)

        # One real API call — keep max_results small.
        result = web_search("Python programming language", max_results=1)

        passed = True

        # 1. Should not return an error string.
        if result.startswith("ERROR:"):
            print(f"FAIL: got error result: {result!r}", file=sys.stderr)
            passed = False

        # 2. Must contain a URL / http substring.
        if "http" not in result.lower():
            print("FAIL: result does not contain a 'url' or 'http' substring.", file=sys.stderr)
            passed = False

        if passed:
            print("SELF-TEST PASSED — all assertions passed.\n")
            # Also print the actual result for visibility.
            print(result)
            sys.exit(0)
        else:
            sys.exit(1)

    # With command-line arguments, treat argv[1:] as the search query.
    query = " ".join(sys.argv[1:])
    output = web_search(query)
    if output.startswith("ERROR:") or output == "No results found.":
        print(output, file=sys.stderr)
        sys.exit(1)
    print(output)
    sys.exit(0)
