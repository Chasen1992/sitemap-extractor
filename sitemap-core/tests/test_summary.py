"""Tests for the run summary's account of what the output is missing.

Same reason as ``test_discovery.py``: the fixture corpus cannot reach this.
It drives ``collect_rows`` per fixture file, so ``RunSummary`` is never built
and ``truncated`` is never read. Every one of these cases is about a number a
person reads before deciding whether to trust the rows in front of them.

The defect that prompted this file: a run stopped by ``maxUrls`` reported
``ok: true, truncated: false`` in the summary while ``notes`` in the same
summary said ``stopped emitting at maxUrls=200; 3691056 more were found``.
The flag contradicted the note, and ``ok: true, truncated: false`` reads as
"this is the complete list". It is the one thing the summary must never say
falsely.

``truncated`` therefore means *the rows you have are not the whole answer*,
and ``truncateReason`` says which cap caused it:

* ``requests`` / ``bytes`` / ``seconds`` — a budget ran out. Worst case: rows
  are in the output that were never checked at all.
* ``max_urls`` — the output cap was reached. The rows are complete as far as
  they go; there are simply more behind them.

When both apply, the budget wins, because it is the one that leaves broken
rows rather than merely fewer of them.

Run standalone::

    python tests/test_summary.py

or under pytest.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import pathlib
import sys
import threading

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_TESTS_DIR.parents[0]))

from sitemap_core import pipeline as _pipeline  # noqa: E402
from sitemap_core.pipeline import extract  # noqa: E402

LOOPBACK_PERMIT = ("127.0.0.0/8",)
PAGE_COUNT = 50


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves one sitemap with ``PAGE_COUNT`` URLs in it."""

    port: int = 0

    def log_message(self, *exc: object) -> None:
        pass

    def _body(self) -> bytes:
        if self.path == "/robots.txt":
            return b"User-agent: *\nAllow: /\n"
        if self.path == "/sitemap.xml":
            urls = "\n".join(
                f"  <url><loc>http://127.0.0.1:{self.port}/p{i}</loc></url>"
                for i in range(PAGE_COUNT)
            )
            return (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"{urls}\n"
                "</urlset>\n"
            ).encode("utf-8")
        return b"page\n"

    def do_GET(self) -> None:  # noqa: N802
        body = self._body()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


def _run(**kwargs):
    """Run the pipeline against a local site.

    Loopback is refused by default — that is the SSRF policy working, and
    ``test_corpus`` asserts that ``extract`` carries no way to switch it off.
    So the allowance is injected here instead, at the transport constructors,
    exactly as ``test_corpus`` does. Nothing about the shipped entry point is
    relaxed: the patch lives and dies inside this function.
    """
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    real_client = _pipeline.CompliantWebClient
    real_checker = _pipeline.StatusChecker
    _pipeline.CompliantWebClient = functools.partial(
        real_client, permit_networks=LOOPBACK_PERMIT
    )
    _pipeline.StatusChecker = functools.partial(
        real_checker, permit_networks=LOOPBACK_PERMIT
    )
    try:
        params = {"check_status_codes": False}
        params.update(kwargs)
        return asyncio.run(extract(start_urls=[f"http://127.0.0.1:{_Handler.port}/"], **params))
    finally:
        _pipeline.CompliantWebClient = real_client
        _pipeline.StatusChecker = real_checker
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# 1. reaching maxUrls is a truncation, and must say so
# --------------------------------------------------------------------------


def test_reaching_max_urls_marks_the_output_truncated() -> None:
    result = _run(max_urls=10)
    summary = result.summary

    assert summary.urlsEmitted == 10, summary.urlsEmitted
    assert summary.truncated is True, (
        "the output was capped at maxUrls and millions more were found; "
        f"the summary reported truncated={summary.truncated}"
    )
    assert summary.truncateReason == "max_urls", summary.truncateReason


def test_a_capped_run_says_how_much_was_left_behind() -> None:
    result = _run(max_urls=10)
    notes = " | ".join(result.summary.notes)
    assert "maxUrls" in notes, f"no note about the cap at all: {notes}"
    assert str(PAGE_COUNT - 10) in notes or "more were found" in notes, notes


def test_a_complete_run_is_not_reported_as_truncated() -> None:
    """The other direction. A flag that is always true is worthless."""
    result = _run(max_urls=PAGE_COUNT + 100)
    summary = result.summary

    assert summary.urlsEmitted == PAGE_COUNT, summary.urlsEmitted
    assert summary.truncated is False, (
        f"everything was emitted, but the summary claimed otherwise: "
        f"{summary.truncateReason}"
    )
    assert summary.truncateReason is None, summary.truncateReason


# --------------------------------------------------------------------------
# 2. a spent budget outranks maxUrls
# --------------------------------------------------------------------------


def test_a_spent_budget_outranks_the_output_cap() -> None:
    """Both caps can hit in one run.

    The budget is the more damaging of the two: it leaves rows in the output
    that were never checked, while maxUrls merely leaves rows behind. The
    reason has to name the worse one, or an operator reading ``max_urls``
    would not go looking for unchecked rows.
    """
    result = _run(max_urls=5, max_requests_total=4, check_status_codes=True)
    summary = result.summary

    assert summary.truncated is True, summary
    assert summary.truncateReason != "max_urls", (
        "a spent budget was reported as a mere output cap: "
        f"{summary.truncateReason}"
    )
    assert summary.truncateReason in ("requests", "bytes", "seconds"), (
        summary.truncateReason
    )


# --------------------------------------------------------------------------
# 3. the accounting that lets a reader check the claim
# --------------------------------------------------------------------------


def test_the_summary_counts_what_was_found_against_what_was_emitted() -> None:
    """`truncated` is only checkable if the two numbers are both there."""
    result = _run(max_urls=10)
    summary = result.summary

    assert summary.urlsDiscovered >= PAGE_COUNT, summary.urlsDiscovered
    assert summary.urlsEmitted == 10, summary.urlsEmitted
    assert summary.urlsDiscovered > summary.urlsEmitted, (
        "discovery and emission agree, so the cap did not bite — "
        "this case proves nothing"
    )


# --------------------------------------------------------------------------


CASES = [
    test_reaching_max_urls_marks_the_output_truncated,
    test_a_capped_run_says_how_much_was_left_behind,
    test_a_complete_run_is_not_reported_as_truncated,
    test_a_spent_budget_outranks_the_output_cap,
    test_the_summary_counts_what_was_found_against_what_was_emitted,
]


def main() -> int:
    failures = 0
    for case in CASES:
        try:
            case()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {case.__name__}: {exc}", flush=True)
        except Exception as exc:  # a crash is a failure too
            failures += 1
            print(f"FAIL  {case.__name__}: {type(exc).__name__}: {exc}", flush=True)
        else:
            print(f"ok    {case.__name__}", flush=True)
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
