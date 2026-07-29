"""
link_tests.py
---------------
Parses html, extracts all external hrefs, checks each one for HTTP status.

Results are grouped:
  OK        — 200 (or 2xx) final response
  REDIRECT  — 3xx resolved successfully; flagged so the URL can be updated
  BLOCKED   — 403/429; server is live but rejecting scrape, manual review needed
  DEAD      — 404, 410, 5xx, connection error, timeout

Usage:
  python link_tests.py
  python link_tests.py --file path/to/other.html
  python link_tests.py --workers 5
  python link_tests.py --timeout 15
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urldefrag
from pathlib import Path
from typing import Callable, TypedDict
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run:  pip install requests beautifulsoup4")
    sys.exit(1)


# constants
DEFAULT_FILE = str(Path(__file__).parent.parent / "sec-cert-roadmap.html")
DEFAULT_WORKERS = 5
DEFAULT_TIMEOUT = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

OK_CODES = {200, 201, 202, 203, 204}
REDIRECT_CODES = {301, 302, 303, 307, 308}
BLOCKED_CODES = {403, 429}

class LinkResult(TypedDict):
    url:           str
    final_url:     str
    status:        int | None
    redirected:    bool
    redirect_hops: list[str]
    error:         str | None
    bucket:        str
    elapsed:       float

# scrape logic
def extract_links(html_path: str) -> list[str]:
    """Return a sorted, deduplicated list of external hrefs from the HTML file."""
    with open(html_path, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    seen: set[str] = set()
    urls: list[str] = []
    for tag in soup.find_all("a", href=True):
        href: str = str(tag["href"]).strip()
        # drop fragments, skip non-http
        href, _ = urldefrag(href)
        if not href.startswith("http"):
            continue
        if href not in seen:
            seen.add(href)
            urls.append(href)

    return sorted(urls)

# single link check
def check_link(url: str, timeout: float | tuple[float, float]) -> LinkResult:
    """
    Check one URL. Returns a dict:
      {
        url:          str   — original URL
        final_url:    str   — URL after any redirects (may differ from url)
        status:       int   — final HTTP status code, or None on error
        redirected:   bool  — True if the URL was permanently redirected (301/308)
        redirect_hops: list — list of intermediate URLs if redirected
        error:        str   — exception message, or None
        bucket:       str   — 'ok' | 'redirect' | 'blocked' | 'dead'
        elapsed:      float — seconds taken
      }
    """
    start = time.monotonic()
    result: LinkResult = {
        "url":           url,
        "final_url":     url,
        "status":        None,
        "redirected":    False,
        "redirect_hops": [],
        "error":         None,
        "bucket":        "dead",
        "elapsed":       0.0,
    }

    session = requests.Session()
    session.headers.update(HEADERS)
    session.max_redirects = 10

    try:
        # Try HEAD first (faster, no body)
        resp = session.head(url, timeout=timeout, allow_redirects=True)

        # If HEAD rejected fall back to GET
        if resp.status_code in (405, 501, 404):
            resp = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
            try:
                resp.close()
            except Exception:
                pass

        result["status"]    = resp.status_code
        result["final_url"] = resp.url

        # Capture redirect chain
        if resp.history:
            result["redirect_hops"] = [r.url for r in resp.history]
            # Flag as "redirected" if the first hop was a permanent redirect
            first_status = resp.history[0].status_code
            if first_status in (301, 308):
                result["redirected"] = True

        # Classify
        code = resp.status_code
        if code in OK_CODES:
            result["bucket"] = "ok"
        elif code in REDIRECT_CODES: # landing page wasn't 2xx
            result["bucket"] = "redirect"
        elif code in BLOCKED_CODES:
            result["bucket"] = "blocked"
        else:
            result["bucket"] = "dead"

        # A final 2xx after following a permanent redirect; ok but flag redirect
        if code in OK_CODES and result["redirected"]:
            result["bucket"] = "redirect"

    except requests.exceptions.Timeout:
        result["error"]  = "Timed out"
        result["bucket"] = "dead"
    except requests.exceptions.TooManyRedirects:
        result["error"]  = "Too many redirects"
        result["bucket"] = "dead"
    except requests.exceptions.ConnectionError as e:
        result["error"]  = f"Connection error: {e}"
        result["bucket"] = "dead"
    except Exception as e:
        result["error"]  = f"Unexpected error: {e}"
        result["bucket"] = "dead"

    result["elapsed"] = round(time.monotonic() - start, 2)
    return result

# reporting
def print_section(title: str, items: list[LinkResult], printer: Callable[[LinkResult], None]) -> None:
    if not items:
        return
    bar = "─" * 72
    print(f"\n{bar}")
    print(f"  {title}  ({len(items)})")
    print(bar)
    for item in items:
        printer(item)

def fmt_redirect(r: LinkResult) -> None:
    arrow = f" → {r['final_url']}" if r["final_url"] != r["url"] else ""
    hops  = ""
    if len(r["redirect_hops"]) > 1:
        hops = f"\n         chain: {' → '.join(r['redirect_hops'])}"
    print(f"  [301→]  {r['url']}{arrow}{hops}")

def fmt_blocked(r: LinkResult) -> None:
    print(f"  [{r['status']}]    {r['url']}")

def fmt_dead(r: LinkResult) -> None:
    if r["error"]:
        print(f"  [ERR]   {r['url']}")
        print(f"           {r['error']}")
    else:
        print(f"  [{r['status']}]    {r['url']}")

def fmt_ok(r: LinkResult) -> None:
    redirect_note = f"  (was: {r['url']})" if r["final_url"] != r["url"] else ""
    print(f"  [200]   {r['final_url']}{redirect_note}")


# main
def main():
    parser = argparse.ArgumentParser(description="Check all hrefs in an HTML file.")
    parser.add_argument("--file",    default=DEFAULT_FILE,    help="Path to the HTML file")
    parser.add_argument("--workers", default=DEFAULT_WORKERS, type=int, help="Parallel workers")
    parser.add_argument("--timeout", default=DEFAULT_TIMEOUT, type=int, help="Per-request timeout (s)")
    args = parser.parse_args()

    # extract
    print(f"Parsing {args.file} …")
    try:
        urls = extract_links(args.file)
    except FileNotFoundError:
        print(f"File not found: {args.file}")
        sys.exit(1)

    print(f"Found {len(urls)} unique external links. Checking with {args.workers} workers …\n")

    # check
    results: list[LinkResult] = []
    completed = 0
    t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_link, url, args.timeout): url for url in urls}
        for future in as_completed(futures):
            res = future.result()
            results.append(res)
            completed += 1
            # Inline progress tick
            bucket_char = {"ok": ".", "redirect": "R", "blocked": "!", "dead": "X"}
            print(bucket_char.get(res["bucket"], "?"), end="", flush=True)

    elapsed_total = round(time.monotonic() - t0, 1)
    print(f"\n\nDone in {elapsed_total}s.\n")

    # sort results into buckets
    ok       = [r for r in results if r["bucket"] == "ok"       and not r["redirected"]]
    redirect = [r for r in results if r["bucket"] == "redirect"  or  r["redirected"]]
    blocked  = sorted([r for r in results if r["bucket"] == "blocked"],  key=lambda r: r["url"])
    dead     = sorted([r for r in results if r["bucket"] == "dead"],     key=lambda r: r["url"])

    # summary
    print("=" * 72)
    print("  SUMMARY")
    print("=" * 72)
    print(f"  Total checked : {len(results)}")
    print(f"  OK (2xx)      : {len(ok)}")
    print(f"  Redirected    : {len(redirect)}  ← update these URLs")
    print(f"  Blocked (403) : {len(blocked)}  ← manual review needed")
    print(f"  Dead / error  : {len(dead)}")

    # detail sections
    print_section("REDIRECTED — update these hrefs to the resolved URL",
                  sorted(redirect, key=lambda r: r["url"]), fmt_redirect)

    print_section("BLOCKED (403 / 429) — server is live but rejecting automation; verify manually",
                  blocked, fmt_blocked)

    print_section("DEAD — 404, 5xx, timeout, or connection failure",
                  dead, fmt_dead)

    # optional full OK list printed
    #print_section("OK", ok, fmt_ok)

    # exit code
    sys.exit(0 if not dead else 1)


if __name__ == "__main__":
    main()