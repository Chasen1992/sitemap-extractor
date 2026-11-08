"""Pre-launch self-test against real websites, not fixtures.

The fixture corpus answers "does the parser survive hostile sitemap files". It
cannot answer "does this work on a real site", because every fixture is served
from loopback and nothing in it involves discovery, robots.txt, a redirect
chain, a real charset, or a server that has opinions about being asked.

This runs the shipped entry point — ``src/main.py``, the same file the Docker
image runs — against real domains and reports two numbers honestly: how many
succeeded, and how long it took.

What counts as a success is deliberately not "exit code 0". A site that
publishes no sitemap is not a tool failure and must not be counted as one,
nor silently counted as a success. Every domain lands in exactly one of:

    ok               sitemaps found, rows emitted
    no_sitemap       the site publishes none — a fact about the site
    sitemap_unreadable  a sitemap was found but none of it could be read
    truncated        rows came back, but a budget stopped the run early
    failed           the run itself broke, or input was rejected

Usage::

    python selftest_10.py                      # the default ten domains
    python selftest_10.py --check-status       # also probe URL status codes
    python selftest_10.py --domains a.com,b.com
    python selftest_10.py --max-urls 200 --max-requests 400
    python selftest_10.py --out report.txt     # also write the table to a file

Sequential on purpose. These are other people's servers.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent
ACTOR_DIR = ROOT / "sitemap-extractor"

# Public technical and commercial sites, no personal data, no login, no
# paywall. Chosen for variety of size and stack rather than for being likely to
# behave: a self-test that only visits sites known to work is not a self-test.
DEFAULT_DOMAINS = [
    "https://www.python.org",
    "https://docs.python.org",
    "https://www.djangoproject.com",
    "https://nodejs.org",
    "https://go.dev",
    "https://react.dev",
    "https://developer.mozilla.org",
    "https://www.mozilla.org",
    "https://www.kernel.org",
    "https://www.apify.com",
]

# "Success" for this report means: the run completed and produced rows.
OK_STATUSES = {"ok", "truncated"}


def classify(summary: dict, exit_code: int) -> str:
    if exit_code != 0 or not summary.get("ok", False):
        return "failed"
    if summary.get("urlsEmitted", 0):
        return "truncated" if summary.get("truncated") else "ok"

    # Nothing came back. Why is in the summary, so use it rather than
    # inferring from counts — a site with no sitemap and a site whose sitemap
    # was unreadable both emit zero rows and must not share a verdict.
    breakdown = summary.get("errorBreakdown", {})
    if breakdown.get("no_sitemap"):
        return "no_sitemap"
    if summary.get("sitemapsAttempted", 0) == 0:
        return "no_sitemap"
    if summary.get("sitemapsSucceeded", 0) == 0:
        return "sitemap_unreadable"
    return "no_sitemap"


def run_one(domain: str, args: argparse.Namespace, workdir: pathlib.Path) -> dict:
    store = workdir / domain.split("//", 1)[1].replace("/", "_").replace(":", "_")
    (store / "key_value_stores" / "default").mkdir(parents=True, exist_ok=True)

    actor_input = {
        "startUrls": [{"url": domain}],
        "checkStatusCodes": args.check_status,
        "maxUrls": args.max_urls,
        "maxRequestsTotal": args.max_requests,
    }
    (store / "key_value_stores" / "default" / "INPUT.json").write_text(
        json.dumps(actor_input), encoding="utf-8"
    )

    env = dict(os.environ)
    env["APIFY_LOCAL_STORAGE_DIR"] = str(store)
    env["PYTHONIOENCODING"] = "utf-8"

    started = time.perf_counter()
    timed_out = False
    try:
        proc = subprocess.run(
            [sys.executable, "src/main.py"],
            cwd=str(ACTOR_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
        )
        exit_code = proc.returncode
        tail = (proc.stderr or "").strip().splitlines()[-3:]
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = -1
        tail = [f"timed out after {args.timeout}s"]
    elapsed = time.perf_counter() - started

    summary_path = store / "key_value_stores" / "default" / "SUMMARY"
    summary: dict = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except ValueError:
            tail = ["SUMMARY record was not valid JSON"]

    if timed_out:
        status = "failed"
    else:
        status = classify(summary, exit_code)

    return {
        "domain": domain,
        "status": status,
        "exit": exit_code,
        "elapsed": elapsed,
        "urls": summary.get("urlsEmitted", 0),
        "sitemaps": f"{summary.get('sitemapsSucceeded', 0)}/{summary.get('sitemapsAttempted', 0)}",
        "discovered": summary.get("urlsDiscovered", 0),
        "requests": summary.get("requestsUsed", 0),
        "reason": "; ".join(tail[-1:]) if status == "failed" else "",
    }


def render(results: list[dict], total_elapsed: float) -> str:
    width = max(len(r["domain"]) for r in results)
    lines = [
        f"{'domain'.ljust(width)}  {'result':<18} {'rows':>7} {'sitemaps':>8} "
        f"{'found':>9} {'reqs':>6} {'secs':>7}",
        "-" * (width + 62),
    ]
    for r in results:
        lines.append(
            f"{r['domain'].ljust(width)}  {r['status']:<18} {r['urls']:>7} "
            f"{r['sitemaps']:>8} {r['discovered']:>9} {r['requests']:>6} "
            f"{r['elapsed']:>7.1f}"
        )
        if r["reason"]:
            lines.append(f"{' '.ljust(width)}    ! {r['reason']}")
    lines.append("-" * (width + 62))

    ok = sum(1 for r in results if r["status"] in OK_STATUSES)
    total = len(results)
    rate = ok / total * 100 if total else 0.0
    lines += [
        "",
        f"domains run        : {total}",
        f"succeeded          : {ok}  ({rate:.1f}%)",
        "  breakdown        : "
        + ", ".join(
            f"{name}={sum(1 for r in results if r['status'] == name)}"
            for name in ("ok", "truncated", "no_sitemap", "sitemap_unreadable", "failed")
        ),
    ]
    if total:
        lines.append(f"total elapsed      : {total_elapsed:.1f}s")
        lines.append(f"per domain, mean   : {total_elapsed / total:.1f}s")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Actor against real domains.")
    parser.add_argument("--domains", default=",".join(DEFAULT_DOMAINS))
    parser.add_argument("--check-status", action="store_true",
                        help="also probe each URL's status code (much slower, many more requests)")
    parser.add_argument("--max-urls", type=int, default=200)
    parser.add_argument("--max-requests", type=int, default=400)
    parser.add_argument("--timeout", type=int, default=600, help="per-domain timeout in seconds")
    parser.add_argument("--out", default=None, help="also write the report to this file")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not domains:
        print("no domains given", flush=True)
        return 2

    if not (ACTOR_DIR / "src" / "main.py").exists():
        print(f"cannot find the Actor entry point at {ACTOR_DIR / 'src' / 'main.py'}", flush=True)
        return 2

    print(f"running {len(domains)} domains through src/main.py "
          f"(checkStatusCodes={args.check_status}, "
          f"maxUrls={args.max_urls}, maxRequestsTotal={args.max_requests})\n", flush=True)

    results: list[dict] = []
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="selftest-") as tmp:
        workdir = pathlib.Path(tmp)
        for index, domain in enumerate(domains, 1):
            print(f"[{index}/{len(domains)}] {domain} ...", end="", flush=True)
            result = run_one(domain, args, workdir)
            results.append(result)
            print(f" {result['status']} in {result['elapsed']:.1f}s "
                  f"({result['urls']} rows)", flush=True)
    total_elapsed = time.perf_counter() - t0

    report = render(results, total_elapsed)
    print("\n" + report, flush=True)

    if args.out:
        pathlib.Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\nwritten to {args.out}", flush=True)

    # A tool that broke is a failure. A site with no sitemap is not.
    return 0 if all(r["status"] != "failed" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
