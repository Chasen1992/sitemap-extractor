"""Adversarial fixture corpus runner.

This is the evidence for the reliability claim. "Well built" is defined here as
a measured number, not as a well-written README.

**Acceptance line: >= 98% of fixtures handled, and no single bad sample may
kill the run.** A hostile sample counts as handled when the tool reports the
declared ``errorType`` and emits no garbage rows.

Run standalone for the archiveable report::

    python tests/test_corpus.py

or under pytest.
"""

from __future__ import annotations

import functools
import http.server
import pathlib
import sys
import threading
import traceback
from dataclasses import dataclass, field

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_TESTS_DIR.parents[0]))

from make_fixtures import FIXTURES_DIR, TRUNCATED_BODY_XML, write_fixtures  # noqa: E402

from sitemap_core.budget import Budget  # noqa: E402
from sitemap_core.discover import walk  # noqa: E402
from sitemap_core.models import ErrorType, UrlRecord  # noqa: E402
from sitemap_core.parse import collect_rows  # noqa: E402
from sitemap_core.statuscheck import StatusChecker  # noqa: E402
from sitemap_core.transport import CompliantWebClient  # noqa: E402

PORT = 8765
LOOPBACK_PERMIT = ("127.0.0.0/8",)


# --------------------------------------------------------------------------
# Local fixture server
# --------------------------------------------------------------------------

class _FixtureHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".xml": "application/xml",
        ".gz": "application/gzip",
        ".txt": "text/plain",
    }

    def log_message(self, fmt: str, *args: object) -> None:  # silence
        pass

    def guess_type(self, path: str) -> str:
        # .xml.gz must not be typed as XML, or the gzip sniff never triggers.
        if path.endswith(".xml.gz"):
            return "application/gzip"
        return super().guess_type(path)

    # ---- synthetic endpoints for status-probing rules ----------------
    def _send(self, code: int, body: bytes = b"", extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head=True)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(head=False)

    def _send_short_body(self) -> None:
        """Advertise 8 KiB, deliver a few hundred bytes, then close the socket.

        The reader must notice the body ended early and report ``truncated``
        rather than handing the parser a half-written sitemap and calling the
        two rows it produced a complete answer.
        """
        body = TRUNCATED_BODY_XML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", "8192")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()
        self.close_connection = True

    def _dispatch(self, *, head: bool) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/fixtures/truncated_body.xml":
            self._send_short_body()
            return
        if not path.startswith("/probe/"):
            super().do_HEAD() if head else super().do_GET()
            return

        kind = path.rsplit("/", 1)[-1]
        if kind == "ok":
            self._send(200, b"<html><body>ok</body></html>")
        elif kind == "missing":
            self._send(404, b"not found")
        elif kind == "head-refused":
            # HEAD is refused, GET works. Probing must fall back to GET.
            if head:
                self._send(405, b"HEAD not allowed")
            else:
                self._send(200, b"<html><body>reachable via GET</body></html>")
        elif kind == "rate-limited":
            self._send(429, b"slow down", {"Retry-After": "1"})
        else:
            self._send(404, b"")


class FixtureServer:
    """Local HTTP server for the fixture corpus.

    The document root is the directory *containing* ``fixtures/``, not
    ``fixtures/`` itself. The generated bodies reference each other as
    ``{base}/fixtures/<name>`` and the status-probe rows as ``{base}/probe/…``,
    so serving ``fixtures/`` directly would turn every relative link into a
    404 — which is what the first run of this suite actually did.
    """

    def __init__(self, directory: pathlib.Path, port: int = PORT) -> None:
        # `directory` is the fixtures dir; serve its parent.
        root = directory.parent if directory.name == "fixtures" else directory
        handler = functools.partial(_FixtureHandler, directory=str(root))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.port = port
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FixtureServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

@dataclass
class Expectation:
    """What a handled fixture looks like."""

    fixture: str
    description: str
    # Row-count assertions. None means "do not check".
    min_rows: int | None = 0
    max_rows: int | None = None
    exact_rows: int | None = None
    # A run is killed if extraction raises, hangs, or returns no result at all.
    error_type: ErrorType | None = None      # expected on the sitemap record
    row_error_type: ErrorType | None = None  # expected on rows
    min_row_error: int | None = None
    max_row_error: int | None = None
    # Hardest assertion: hostile samples must produce ZERO usable rows.
    forbid_rows: bool = False
    # Substrings that must / must not appear among the emitted row URLs.
    require_row_urls: list[str] = field(default_factory=list)
    forbid_row_urls: list[str] = field(default_factory=list)
    # For each flag, at least one row must show the declared value. This is how
    # "extension namespaces are surfaced" is verified rather than asserted.
    require_flags: dict[str, bool] = field(default_factory=dict)
    # Exact status-code distribution, keyed by status as a string. This is how
    # the HEAD->GET fallback rule is verified rather than eyeballed in a log.
    require_status: dict[str, int] = field(default_factory=dict)
    # Parsing fixtures deliberately skip status probing: their rows point at
    # reserved .example hosts, and a DNS lookup per row would measure DNS, not
    # the parser. Status probing has its own fixtures below.
    check_status: bool = False
    max_elapsed_sec: float = 60.0


MANIFEST: list[Expectation] = [
    Expectation(
        "plain.xml",
        "normal urlset",
        exact_rows=4,
        require_row_urls=["/about", "/products/widget", "/contact"],
    ),
    Expectation("no_namespace.xml", "no XML namespace", exact_rows=3),
    Expectation("bad_prefix.xml", "arbitrary element prefix", exact_rows=2),
    Expectation(
        "malformed.xml",
        "malformed XML (bare &, unclosed tag, stray close) still yields every "
        "URL it lists — the streaming parser stops at the first bad token, the "
        "recovery net does not",
        exact_rows=4,
        require_row_urls=["/good-1", "/search?q=a&b=c", "/good-2", "/good-3"],
    ),
    Expectation("empty.xml", "empty body", exact_rows=0, error_type=ErrorType.EMPTY_BODY),
    Expectation("truncated.xml", "truncated mid-element", min_rows=2),
    Expectation(
        "truncated_body.xml",
        "body ended early against its own Content-Length: reported truncated, "
        "not parsed as a short-but-complete sitemap",
        exact_rows=0,
        error_type=ErrorType.TRUNCATED,
        forbid_rows=True,
    ),
    Expectation(
        "html_challenge.xml",
        "WAF challenge page at /sitemap.xml must yield no rows",
        exact_rows=0,
        error_type=ErrorType.HTML_CHALLENGE,
        forbid_rows=True,
    ),
    Expectation(
        "robots_html.txt",
        "HTML error page posing as a text sitemap",
        exact_rows=0,
        error_type=ErrorType.NOT_XML,
        forbid_rows=True,
    ),
    Expectation(
        "billion_laughs.xml",
        "DOCTYPE/entity expansion refused by the hardened parser",
        exact_rows=0,
        error_type=ErrorType.PARSE_ERROR,
        forbid_rows=True,
    ),
    Expectation(
        "news_image.xml",
        "extension namespaces ignored, flags surfaced",
        exact_rows=2,
        require_flags={"hasImages": True, "hasNews": True},
    ),
    Expectation(
        "bad_dates.xml",
        "every row survives the lastmod zoo",
        exact_rows=12,
    ),
    Expectation("idna.xml", "non-ASCII host and path", exact_rows=4),
    Expectation(
        "latin1.xml",
        "ISO-8859-1 body: non-ASCII paths must not arrive as mojibake",
        exact_rows=3,
        require_row_urls=["/café", "/über", "/plain"],
        # U+FFFD is what a latin-1 body looks like after being read as UTF-8.
        forbid_row_urls=["�"],
    ),
    Expectation(
        "dup_entries.xml",
        "dedup collapses URL noise; scheme/host/trailing-slash stay distinct",
        exact_rows=5,
        require_row_urls=[
            "https://dup.example/page",
            "https://dup.example/other",
            "http://dup.example/page",
            "https://www.dup.example/page",
            "https://dup.example/page/",
        ],
        forbid_row_urls=["#section", "%67", "/./"],
    ),
    Expectation("sitemap.txt", "plain text sitemap", exact_rows=4),
    Expectation(
        "cross_domain.xml",
        "cross-domain loc marked, not probed",
        exact_rows=4,
        row_error_type=ErrorType.CROSS_DOMAIN,
        min_row_error=4,
        max_row_error=4,
    ),
    Expectation("gzipped.xml.gz", "normal gzip", exact_rows=4),
    Expectation("double_gzip.xml.gz", "double gzip", exact_rows=4),
    Expectation(
        "fake_gz.xml.gz",
        "named .gz but is HTML",
        exact_rows=0,
        error_type=ErrorType.NOT_GZIP,
        forbid_rows=True,
    ),
    Expectation(
        "renamed.xml.gz",
        "named .gz but is plain XML (harmless)",
        exact_rows=4,
    ),
    Expectation(
        "gzip_bomb.xml.gz",
        "compression bomb refused by the ratio guard, not merely truncated",
        exact_rows=0,
        error_type=ErrorType.GZIP_BOMB,
        forbid_rows=True,
    ),
    Expectation("huge.xml", "50,000 URLs in one file", exact_rows=50_000),
    Expectation("index_of_index.xml", "index of index (WordPress shape)", min_rows=5),
    Expectation("index_mid.xml", "index -> index + urlset", min_rows=6),
    Expectation(
        "cyclic_a.xml",
        "A -> B -> A terminates and the sibling branch is still harvested",
        min_rows=2,
        require_row_urls=["/survivor"],
    ),
    Expectation(
        "ssrf_lure.xml",
        "metadata and private targets surfaced, and refused by the SSRF guard",
        min_rows=5,
        require_row_urls=["169.254.169.254", "127.0.0.1:8080", "192.168.0.1", "10.0.0.5"],
    ),
    Expectation(
        "status_probe.xml",
        "HEAD+GET fallback, 404, 429 Retry-After, dedup before probing",
        exact_rows=4,
        require_status={"200": 2, "404": 1, "429": 1},
        check_status=True,
        max_elapsed_sec=30.0,
    ),
]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

@dataclass
class FixtureResult:
    name: str
    handled: bool
    rows: int
    elapsed: float
    detail: str
    killed_run: bool = False
    error_type_seen: str | None = None
    # Evidence kept so `evaluate` can check the assertions the manifest makes
    # about row *content*, not just row counts. Without these the manifest's
    # require_row_urls / forbid_row_urls / require_flags would be declared and
    # never verified — a suite that reports 100% while checking half of what
    # it claims to.
    row_urls: list[str] = field(default_factory=list)
    row_error_counts: dict[str, int] = field(default_factory=dict)
    flag_values: dict[str, set[bool]] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)


def run_fixture(
    name: str,
    base: str,
    *,
    check_status: bool = False,
    max_urls: int = 200_000,
) -> FixtureResult:
    """Run one fixture end to end. Never raises: a crash is itself a failure."""
    import time

    started = time.monotonic()
    url = f"{base}/fixtures/{name}"
    try:
        budget = Budget(max_requests=200_000, max_seconds=120.0)
        with CompliantWebClient(budget=budget, permit_networks=LOOPBACK_PERMIT) as client:
            walk_result = walk(url, budget=budget, client=client, max_depth=4)

            rows, counters = collect_rows(
                walk_result,
                seed_url=url,
                raw_bodies={
                    u: client.raw_capture.get(u) or ""
                    for u in client.raw_capture.urls()
                },
                include_subdomains=False,
                max_urls=max_urls,
            )

            if check_status and rows:
                import asyncio

                async def _probe() -> None:
                    async with StatusChecker(
                        budget=budget, permit_networks=LOOPBACK_PERMIT
                    ) as checker:
                        await checker.check_all(rows)

                asyncio.run(_probe())

        elapsed = time.monotonic() - started
        sitemap_errors = [
            s.errorType.value for s in walk_result.sitemaps if s.errorType is not None
        ]
        status_counts: dict[str, int] = {}
        row_urls: list[str] = []
        row_error_counts: dict[str, int] = {}
        flag_values: dict[str, set[bool]] = {}
        for row in rows:
            key = str(row.statusCode) if row.statusCode is not None else (
                row.errorType.value if row.errorType else "unchecked"
            )
            status_counts[key] = status_counts.get(key, 0) + 1
            row_urls.append(row.url)
            if row.errorType is not None:
                name_ = row.errorType.value
                row_error_counts[name_] = row_error_counts.get(name_, 0) + 1
            for flag in ("hasImages", "hasNews"):
                flag_values.setdefault(flag, set()).add(bool(getattr(row, flag)))
        return FixtureResult(
            name=name,
            handled=True,  # refined below by the expectation check
            rows=len(rows),
            elapsed=elapsed,
            detail=(
                f"rows={len(rows)} deduped={counters['deduped']} "
                f"sitemap_errors={sitemap_errors} status={status_counts}"
            ),
            error_type_seen=sitemap_errors[0] if sitemap_errors else None,
            row_urls=row_urls,
            row_error_counts=row_error_counts,
            flag_values=flag_values,
            status_counts=status_counts,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        return FixtureResult(
            name=name,
            handled=False,
            rows=-1,
            elapsed=elapsed,
            detail=f"RUN KILLED: {type(exc).__name__}: {exc}",
            killed_run=True,
        )


def evaluate(expectation: Expectation, result: FixtureResult) -> tuple[bool, str]:
    """Check a fixture result against its declared outcome."""
    if result.killed_run:
        return False, f"run killed — {result.detail}"

    problems: list[str] = []

    if expectation.exact_rows is not None and result.rows != expectation.exact_rows:
        problems.append(f"rows={result.rows}, expected exactly {expectation.exact_rows}")
    if expectation.min_rows is not None and result.rows < expectation.min_rows:
        problems.append(f"rows={result.rows}, expected >= {expectation.min_rows}")
    if expectation.max_rows is not None and result.rows > expectation.max_rows:
        problems.append(f"rows={result.rows}, expected <= {expectation.max_rows}")

    if expectation.error_type is not None:
        if result.error_type_seen != expectation.error_type.value:
            problems.append(
                f"errorType={result.error_type_seen}, expected {expectation.error_type.value}"
            )
    elif result.error_type_seen is not None:
        problems.append(f"unexpected sitemap errorType {result.error_type_seen}")

    if expectation.forbid_rows and result.rows > 0:
        problems.append(f"{result.rows} rows emitted where none are allowed")

    # Row-content assertions. These are what stop a fixture from "passing" on a
    # row count while emitting the wrong URLs or the wrong flags.
    if expectation.require_row_urls:
        joined = "\n".join(result.row_urls)
        for wanted in expectation.require_row_urls:
            if wanted not in joined:
                problems.append(f"expected a row matching {wanted!r}, saw none")
    if expectation.forbid_row_urls:
        for banned in expectation.forbid_row_urls:
            hits = [u for u in result.row_urls if banned in u]
            if hits:
                problems.append(
                    f"{len(hits)} row(s) match forbidden {banned!r}, e.g. {hits[0]!r}"
                )

    if expectation.row_error_type is not None:
        counted = result.row_error_counts.get(expectation.row_error_type.value, 0)
        low = 1 if expectation.min_row_error is None else expectation.min_row_error
        high = expectation.max_row_error
        if counted < low:
            problems.append(
                f"{counted} row(s) with {expectation.row_error_type.value}, expected >= {low}"
            )
        if high is not None and counted > high:
            problems.append(
                f"{counted} row(s) with {expectation.row_error_type.value}, expected <= {high}"
            )
    elif expectation.min_row_error or expectation.max_row_error:
        problems.append("row error bounds given without row_error_type")

    for flag, wanted in expectation.require_flags.items():
        observed = result.flag_values.get(flag, set())
        if wanted not in observed:
            problems.append(
                f"flag {flag} never observed as {wanted} (saw {sorted(observed)})"
            )

    for key, wanted in expectation.require_status.items():
        got = result.status_counts.get(key, 0)
        if got != wanted:
            problems.append(f"status {key}: {got} row(s), expected exactly {wanted}")

    if result.elapsed > expectation.max_elapsed_sec:
        problems.append(f"took {result.elapsed:.1f}s, limit {expectation.max_elapsed_sec}s")

    if problems:
        return False, "; ".join(problems)
    return True, result.detail


def run_corpus(base: str = f"http://127.0.0.1:{PORT}") -> tuple[list[FixtureResult], list[tuple[Expectation, bool, str]]]:
    write_fixtures(base)
    results: list[FixtureResult] = []
    verdicts: list[tuple[Expectation, bool, str]] = []

    for expectation in MANIFEST:
        fixture_path = FIXTURES_DIR / expectation.fixture
        if not fixture_path.exists():
            results.append(
                FixtureResult(expectation.fixture, False, -1, 0.0, "fixture file missing", True)
            )
            verdicts.append((expectation, False, "fixture file missing"))
            continue

        result = run_fixture(
            expectation.fixture, base, check_status=expectation.check_status
        )
        ok, detail = evaluate(expectation, result)
        result.handled = ok
        result.detail = detail
        results.append(result)
        verdicts.append((expectation, ok, detail))

    return results, verdicts


# --------------------------------------------------------------------------
# Additional assertions that the manifest cannot express
# --------------------------------------------------------------------------

def check_no_garbage_rows(base: str) -> list[str]:
    """Hostile samples must not leak navigation links as discovered URLs."""
    problems: list[str] = []
    challenge_url = f"{base}/fixtures/html_challenge.xml"
    budget = Budget(max_requests=1000, max_seconds=60.0)
    with CompliantWebClient(budget=budget, permit_networks=LOOPBACK_PERMIT) as client:
        walk_result = walk(challenge_url, budget=budget, client=client)
        rows, _ = collect_rows(
            walk_result,
            seed_url=challenge_url,
            raw_bodies={u: client.raw_capture.get(u) or "" for u in client.raw_capture.urls()},
            max_urls=1000,
        )
    for row in rows:
        problems.append(f"challenge page leaked row: {row.url}")
    return problems


def check_ssrf_lure(base: str) -> list[str]:
    """The private/metadata URLs inside a sitemap must be refused, not probed."""
    problems: list[str] = []
    url = f"{base}/fixtures/ssrf_lure.xml"
    budget = Budget(max_requests=1000, max_seconds=60.0)
    with CompliantWebClient(budget=budget, permit_networks=LOOPBACK_PERMIT) as client:
        walk_result = walk(url, budget=budget, client=client)
        rows, _ = collect_rows(
            walk_result,
            seed_url=url,
            raw_bodies={u: client.raw_capture.get(u) or "" for u in client.raw_capture.urls()},
            max_urls=1000,
        )
        if client.raw_capture.get(f"{base}/fixtures/ssrf_lure.xml") is None:
            problems.append("ssrf_lure.xml was not fetched")

    # Strict policy must refuse these even though the fixture server permits
    # loopback for its own sake.
    from sitemap_core.ssrf import SsrfBlocked, resolve_and_check

    for target in (
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/admin",
        "http://192.168.0.1/router",
        "http://10.0.0.5/internal",
    ):
        try:
            resolve_and_check(target)  # strict: no permit
            problems.append(f"SSRF guard allowed {target}")
        except SsrfBlocked:
            pass
        except Exception as exc:
            problems.append(f"SSRF guard raised {type(exc).__name__} for {target}: {exc}")

    return problems


def check_pipeline_refuses_loopback() -> list[str]:
    """pipeline.extract must never carry the test-only permit switch."""
    import inspect

    from sitemap_core import pipeline

    problems: list[str] = []
    signature = inspect.signature(pipeline.extract)
    for forbidden in ("permit_networks", "permit", "allow_private", "ssrf_off"):
        if forbidden in signature.parameters:
            problems.append(f"pipeline.extract exposes test-only switch {forbidden!r}")
    return problems


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def main() -> int:
    # Line-buffered so a long run shows progress instead of nothing at all.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    print("sitemap-core adversarial fixture corpus")
    print("=" * 78)

    with FixtureServer(FIXTURES_DIR) as server:
        base = f"http://127.0.0.1:{server.port}"
        results, verdicts = run_corpus(base)

        print(f"{'fixture':22s} {'rows':>7s} {'secs':>6s}  {'ok':>3s}  detail")
        print("-" * 78)
        for result, (expectation, ok, detail) in zip(results, verdicts):
            mark = "PASS" if ok else "FAIL"
            rows = "-" if result.killed_run else str(result.rows)
            print(
                f"{result.name:22s} {rows:>7s} {result.elapsed:6.2f}  {mark:>3s}  {detail[:70]}"
            )

        extra_problems = []
        extra_problems += check_no_garbage_rows(base)
        extra_problems += check_ssrf_lure(base)
        extra_problems += check_pipeline_refuses_loopback()

        handled = sum(1 for _, ok, _ in verdicts if ok)
        total = len(verdicts)
        rate = (handled / total * 100) if total else 0.0
        killed = [r.name for r in results if r.killed_run]

        print("-" * 78)
        print(f"fixtures handled : {handled}/{total}  ({rate:.1f}%)")
        print(f"acceptance line  : >= 98.0%")
        print(f"runs killed      : {killed if killed else 'none'}")
        if extra_problems:
            print(f"extra checks     : {len(extra_problems)} problem(s)")
            for problem in extra_problems:
                print(f"   - {problem}")
        else:
            print("extra checks     : all passed (no garbage rows, SSRF strict, no test switches)")

        passed = rate >= 98.0 and not killed and not extra_problems
        print()
        print("VERDICT:", "PASS" if passed else "FAIL")
        return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
