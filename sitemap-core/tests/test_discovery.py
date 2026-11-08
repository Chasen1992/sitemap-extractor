"""Unit tests for sitemap discovery reporting.

Like ``test_normalize.py``, this exists because the fixture corpus cannot reach
these paths. Every corpus fixture is seeded with a direct sitemap URL —
``/fixtures/plain.xml`` — so the walk takes the ``direct`` branch and
``sitemap_tree_for_homepage`` is never called. The synthetic discovery tree
that it builds, and everything that goes wrong when that tree is described, is
therefore invisible to a suite that reports 28/28.

Four defects lived in this path. The first is the serious one.

1. **robots.txt was thrown away, so sitemaps named there were never found.**
   The sniffer classifies every fetched body, and it called ``User-agent: *``
   "neither XML, a text sitemap, nor recognizable HTML". Discovery therefore
   only ever found sitemaps sitting at a well-known path, and a site listing
   its sitemap in robots.txt — the primary way it is done — was reported as
   ``errorType: no_sitemap``. That is a false statement about the site, and it
   was invisible because every fixture is seeded with a direct sitemap URL.

2. A site that publishes no sitemap was reported as a **successful sitemap
   index containing nothing** — ``ok: true, kind: "sitemapindex"`` — and the
   summary claimed ``1/1 sitemaps succeeded``. ``https://go.dev`` produces
   exactly this: no ``Sitemap:`` line in robots.txt, every well-known path 404.

3. ``pagesFound`` on an index node counted ``node.all_pages()``, which walks
   the whole subtree. Every URL was therefore reported once per level of index
   above it: on a live run, ``https://www.apify.com/`` and the sitemap index
   beneath it both claimed ``pagesFound: 922814``.

4. The same sitemap file is reached twice — robots.txt lists it and a
   well-known path serves it — and was recorded twice, so the summary counted
   one sitemap index as two.

Three of these four changed a **number the user reads**, and one changed
**which sitemaps were found at all**. None of them changed a single URL in the
output of a run that found anything. That is what an untested path looks like:
the corpus reports 28/28 throughout, because it cannot reach here.

Two more arrived later, and they are a fifth kind: not a wrong number and not
a missing sitemap, but a **message that claims more than the tool observed**.

5. ``sniff`` reported "HTML document returned where a sitemap was expected"
   about a ``robots.txt`` fetch. No sitemap was ever expected there — the
   request was for robots.txt, which the tool itself made.

6. ``_mark_empty_discovery`` reported "robots.txt lists none and no well-known
   sitemap path responded". On a soft-404 site — one that 302s every missing
   path to its own error page and serves that page 200 — neither clause is
   true: robots.txt is never read, and the paths *do* respond, with HTML.

Both were found by a single run against ``chinafilm.gov.cn``, whose log
carried 23 requests and 23 statements the code had no way to know. Neither is
fixable by reading the output; both required reading the source to notice
that the words and the observations were unrelated. See section 4 below.

Run standalone::

    python tests/test_discovery.py

or under pytest.
"""

from __future__ import annotations

import http.server
import pathlib
import sys
import threading

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_TESTS_DIR.parents[0]))

from sitemap_core.budget import Budget  # noqa: E402
from sitemap_core.discover import (  # noqa: E402
    SYNTHETIC_KINDS,
    WalkResult,
    _empty_discovery_reason,
    walk,
)
from sitemap_core.models import ErrorType, SitemapRecord  # noqa: E402
from sitemap_core.normalize import normalize_url  # noqa: E402
from sitemap_core.sniff import ContentKind, sniff  # noqa: E402
from sitemap_core.transport import CompliantWebClient  # noqa: E402

LOOPBACK_PERMIT = ("127.0.0.0/8",)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves one synthetic site. ``routes`` is set per-test by ``Site``."""

    routes: dict[str, tuple[int, str, bytes]] = {}

    def log_message(self, *exc: object) -> None:
        pass

    def do_GET(self) -> None:
        status, ctype, body = self.routes.get(self.path, (404, "text/plain", b""))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()


class Site:
    """A local HTTP server serving one synthetic site.

    Route bodies may contain ``{base}``, substituted once the port is known, so
    a fixture can reference absolute URLs (a sitemap index points at its
    children by full URL) without hard-coding a port. ``{base}`` has **no**
    trailing slash: a fixture writes ``{base}/a.xml`` and gets a URL with
    exactly one. Writing the base with a slash produced ``//a.xml``, which is a
    different URL string — so the same sitemap file appeared twice in the walk
    and looked like a product defect while being a fixture one.
    """

    def __init__(self, routes: dict[str, tuple[str, str]]) -> None:
        self._templates = routes
        self.httpd = None
        self.port = 0

    def __enter__(self) -> "Site":
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        _Handler.routes = {
            path: (200, ctype, body.format(base=self.origin()).encode("utf-8"))
            for path, (ctype, body) in self._templates.items()
        }
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        _Handler.routes = {}

    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def walk_site(routes: dict[str, tuple[str, str]], seed: str = "/"):
    with Site(routes) as site:
        budget = Budget(max_requests=500, max_seconds=30.0)
        with CompliantWebClient(budget=budget, permit_networks=LOOPBACK_PERMIT) as client:
            return walk(site.url(seed), budget=budget, client=client, max_depth=4)


# --------------------------------------------------------------------------
# 1. a site that publishes no sitemap
# --------------------------------------------------------------------------

def test_site_with_no_sitemap_is_not_a_success() -> None:
    routes = {"/robots.txt": ("text/plain", "User-agent: *\nAllow: /\n")}
    result = walk_site(routes)

    assert result.sitemaps, "discovery produced no record at all"
    real = [r for r in result.sitemaps if r.kind not in SYNTHETIC_KINDS]
    assert not real, f"claimed sitemap files that do not exist: {[r.url for r in real]}"

    for record in result.sitemaps:
        assert record.kind in SYNTHETIC_KINDS, record
        assert record.ok is False, f"{record.url} reported as a success"
        assert record.errorType is ErrorType.NO_SITEMAP, record.errorType
        assert record.pagesFound == 0, record
        assert record.errorMessage, "a refused record must say why"


def test_site_with_no_sitemap_still_has_a_discovery_record() -> None:
    """The run must say it looked, not come back with an empty list."""
    routes = {"/robots.txt": ("text/plain", "User-agent: *\nAllow: /\n")}
    result = walk_site(routes)
    kinds = {r.kind for r in result.sitemaps}
    assert "website" in kinds, f"expected the discovery root, got {kinds}"


# --------------------------------------------------------------------------
# 2. a real site with a sitemap index
# --------------------------------------------------------------------------

def _index_site() -> dict[str, tuple[int, str, bytes]]:
    def route(ctype: str, body: str) -> tuple[str, str]:
        return (ctype, body)

    return {
        "/robots.txt": route(
            "text/plain",
            "User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap-index.xml\n",
        ),
        "/sitemap-index.xml": route(
            "application/xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "  <sitemap><loc>{base}/a.xml</loc></sitemap>\n"
            "  <sitemap><loc>{base}/b.xml</loc></sitemap>\n"
            "</sitemapindex>\n",
        ),
        "/a.xml": route(
            "application/xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "  <url><loc>{base}/p1</loc></url>\n"
            "  <url><loc>{base}/p2</loc></url>\n"
            "  <url><loc>{base}/p3</loc></url>\n"
            "</urlset>\n",
        ),
        "/b.xml": route(
            "application/xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "  <url><loc>{base}/q1</loc></url>\n"
            "  <url><loc>{base}/q2</loc></url>\n"
            "</urlset>\n",
        ),
    }


def test_pages_found_counts_the_file_not_the_subtree() -> None:
    result = walk_site(_index_site())

    by_kind: dict[str, list] = {}
    for record in result.sitemaps:
        by_kind.setdefault(record.kind, []).append(record)

    total = sum(r.pagesFound for r in result.sitemaps)
    assert total == 5, (
        "pagesFound must total the URLs actually listed, once. "
        f"Got {total} from {[ (r.kind, r.pagesFound) for r in result.sitemaps ]}"
    )

    assert by_kind.get("urlset"), "the two child urlsets are missing"
    assert len(by_kind["urlset"]) == 2, by_kind["urlset"]
    assert sorted(r.pagesFound for r in by_kind["urlset"]) == [2, 3], by_kind["urlset"]

    for record in by_kind.get("sitemapindex", []):
        assert record.pagesFound == 0, (
            "an index file lists sitemaps, not pages; "
            f"{record.url} reported {record.pagesFound}"
        )
    for record in by_kind.get("website", []) + by_kind.get("robots", []):
        assert record.pagesFound == 0, (
            "discovery scaffolding holds no pages of its own; "
            f"{record.url} reported {record.pagesFound}"
        )


def test_discovery_scaffolding_is_labelled_not_called_a_sitemap() -> None:
    result = walk_site(_index_site())
    for record in result.sitemaps:
        if record.depth == 0:
            assert record.kind == "website", record
            assert record.ok is True, "discovery of a real sitemap did succeed"
            break
    else:
        raise AssertionError("no depth-0 record")

    assert all(r.errorType is None for r in result.sitemaps), [
        (r.url, r.errorType) for r in result.sitemaps
    ]


def test_only_real_sitemap_files_are_counted_as_attempted() -> None:
    """The rule pipeline.py uses. Kept here so the kinds cannot drift from it."""
    result = walk_site(_index_site())
    real = [r for r in result.sitemaps if r.kind not in SYNTHETIC_KINDS]
    assert len(real) == 3, [r.kind for r in result.sitemaps]
    assert sum(1 for r in real if r.ok) == 3


def test_the_same_sitemap_file_reached_twice_is_one_record() -> None:
    """robots.txt and a well-known path routinely name the same file.

    Both are tried, so the tree holds two nodes for it — and USP spells them
    differently, joining one robots.txt path with a doubled slash. Counting
    both reports a site that publishes one sitemap index as publishing two.
    ``normalize_url`` is the identity, so the spelling does not matter.
    """
    result = walk_site(_index_site())
    urls = [r.url for r in result.sitemaps if r.kind not in SYNTHETIC_KINDS]
    identities = [normalize_url(u) for u in urls]
    assert len(identities) == len(set(identities)), (
        "the same sitemap file was recorded more than once: "
        + ", ".join(sorted(identities))
    )


# --------------------------------------------------------------------------
# 3. robots.txt — the discovery path the corpus also cannot reach
# --------------------------------------------------------------------------

def test_robots_txt_is_readable_content() -> None:
    """The sniffer must not refuse robots.txt.

    Discovery depends on this file: its ``Sitemap:`` lines are the primary way
    a site says where its sitemap is. The sniffer is asked about every fetched
    body, and it used to classify ``User-agent: *`` as "neither XML, a text
    sitemap, nor recognizable HTML" — so the file was thrown away and the site
    reported as publishing no sitemap at all.

    The ``Content-Encoding: gzip`` below is not decoration: httpx decompresses
    transparently and leaves that header set, so this is the exact combination
    a real run is handed.
    """
    body = b"User-agent: *\nAllow: /\n\nSitemap: https://example.com/custom-sitemap.xml\n"
    result = sniff(
        body,
        content_type="text/plain; charset=utf-8",
        url="https://example.com/robots.txt",
        content_encoding="gzip",
    )
    assert result.error_type is None, (result.error_type, result.detail)
    assert result.kind is ContentKind.TEXT, result.kind
    assert result.is_usable


def test_a_sitemap_named_only_in_robots_txt_is_found() -> None:
    """The end-to-end case, and the one that was silently broken.

    The sitemap is at a path no well-known guess would try, so robots.txt is
    the only way to learn of it. Before the fix this reported
    ``errorType: no_sitemap`` for a site that publishes one.
    """
    routes = {
        "/robots.txt": (
            "text/plain",
            "User-agent: *\nAllow: /\n\nSitemap: {base}/custom-sitemap.xml\n",
        ),
        "/custom-sitemap.xml": (
            "application/xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "  <url><loc>{base}/p1</loc></url>\n"
            "  <url><loc>{base}/p2</loc></url>\n"
            "</urlset>\n",
        ),
    }
    result = walk_site(routes)
    real = [r for r in result.sitemaps if r.kind not in SYNTHETIC_KINDS]
    assert len(real) == 1, [(r.kind, r.url) for r in result.sitemaps]
    assert real[0].ok is True, real[0]
    assert real[0].pagesFound == 2, real[0]
    assert all(r.errorType is None for r in result.sitemaps), [
        (r.url, r.errorType) for r in result.sitemaps
    ]


def test_a_robots_txt_naming_a_captcha_path_is_still_readable() -> None:
    """Why robots.txt is checked before the challenge sweep.

    robots.txt names paths, and one of them can be called ``captcha``. A
    content sniff that looks for block-page markers first discards the file —
    and with it the site's only statement of where its sitemap is.
    """
    body = (
        b"User-agent: *\nDisallow: /captcha\nDisallow: /cloudflare-verify\n\n"
        b"Sitemap: https://example.com/custom-sitemap.xml\n"
    )
    result = sniff(body, content_type="text/plain", url="https://example.com/robots.txt")
    assert result.error_type is None, (result.error_type, result.detail)
    assert result.kind is ContentKind.TEXT, result.kind


# --------------------------------------------------------------------------
# 4. messages that claim more than the tool observed
# --------------------------------------------------------------------------
#
# The run that exposed this was against a site whose every missing path 302s
# to its own error page: 23 requests, and the log carried 23 statements the
# code had no way to know. Two of them were written into the source.
#
#   sniff:    "HTML document returned where a sitemap was expected"
#             — said about a robots.txt fetch, where no sitemap was expected.
#   discover: "robots.txt lists none and no well-known sitemap path responded"
#             — robots.txt was never read, and the paths did respond, with HTML.
#
# Both are the same defect class as everything else in this file: a claim the
# code cannot support. These tests fail while either is present.

_HTML_ERROR_PAGE = (
    "text/html",
    "<!doctype html>\n<html><head><title>404</title></head><body>not found</body></html>\n",
)


def test_html_at_robots_txt_is_not_reported_as_a_missing_sitemap() -> None:
    """The request was for robots.txt. Say that, not "a sitemap"."""
    result = sniff(
        b"<!doctype html>\n<html><head><title>404</title></head></html>\n",
        content_type="text/html",
        url="https://example.com/robots.txt",
    )
    assert result.error_type is ErrorType.NOT_XML, (result.error_type, result.detail)
    assert "robots.txt" in result.detail, result.detail
    assert "a sitemap" not in result.detail, result.detail


def test_html_at_a_sitemap_path_still_says_a_sitemap() -> None:
    """The positive control: the sitemap wording must not drift either."""
    result = sniff(
        b"<!doctype html>\n<html><head><title>404</title></head></html>\n",
        content_type="text/html",
        url="https://example.com/sitemap.xml",
    )
    assert result.error_type is ErrorType.NOT_XML, (result.error_type, result.detail)
    assert result.detail == "HTML document returned where a sitemap was expected", (
        result.detail
    )


def test_html_at_robots_txt_with_a_query_string_is_still_robots_txt() -> None:
    """The path is what identifies the request, not the whole URL."""
    result = sniff(
        b"<!doctype html>\n<html><head><title>404</title></head></html>\n",
        content_type="text/html",
        url="https://example.com/robots.txt?ts=1",
    )
    assert "robots.txt" in result.detail, result.detail


def test_robots_txt_that_could_not_be_read_is_said_to_be_unreadable() -> None:
    result = WalkResult()
    result.failure_by_url["https://example.com/robots.txt"] = (
        ErrorType.NOT_XML,
        "HTML document returned where robots.txt was expected",
    )
    message = _empty_discovery_reason(result)
    assert message.startswith("no sitemap found at this site: "), message
    assert "robots.txt could not be read" in message, message
    assert "HTML document returned where robots.txt was expected" in message, message
    assert "robots.txt lists none" not in message, (
        "the file was not read, so it cannot have listed anything: " + message
    )


def test_robots_txt_read_and_holding_nothing_is_said_to_have_held_nothing() -> None:
    result = WalkResult()
    result.sitemaps.append(SitemapRecord(url="https://example.com/robots.txt", kind="robots"))
    message = _empty_discovery_reason(result)
    assert "robots.txt named no sitemap" in message, message
    assert "could not be read" not in message, message


def test_robots_txt_never_fetched_is_said_to_be_unconsulted() -> None:
    """Not fetched is a different fact from fetched-and-empty. Never blur them."""
    message = _empty_discovery_reason(WalkResult())
    assert "robots.txt was not consulted" in message, message
    assert "no candidate sitemap URL was tried" in message, message


def test_soft_404_site_never_claims_the_paths_did_not_respond() -> None:
    """The chinafilm.gov.cn shape, and the one that started this.

    Every missing path 302s to the site's own error page and that page is
    served 200, so the paths *did* respond. The old wording said they did
    not, which is the opposite of what happened and could not be checked by
    any reader.
    """
    result = WalkResult()
    result.failure_by_url["https://example.com/robots.txt"] = (
        ErrorType.NOT_XML,
        "HTML document returned where robots.txt was expected",
    )
    for name in ("sitemap.xml", "wp-sitemap.xml", "sitemap.txt"):
        result.failure_by_url[f"https://example.com/{name}"] = (
            ErrorType.NOT_XML,
            "HTML document returned where a sitemap was expected",
        )
    message = _empty_discovery_reason(result)
    assert "no well-known sitemap path responded" not in message, message
    assert "3 candidate sitemap URL(s) tried" in message, message
    assert "most common outcome: not_xml" in message, message


def test_the_stated_candidate_count_is_the_number_actually_tried() -> None:
    result = WalkResult()
    result.failure_by_url["https://example.com/robots.txt"] = (
        ErrorType.NOT_XML,
        "HTML document returned where robots.txt was expected",
    )
    for i in range(7):
        result.failure_by_url[f"https://example.com/p{i}.xml"] = (
            ErrorType.HTTP_ERROR,
            "HTTP 404",
        )
    message = _empty_discovery_reason(result)
    assert "7 candidate sitemap URL(s) tried" in message, message
    assert "most common outcome: http_error" in message, message
    assert "robots.txt could not be read" in message, message


def test_end_to_end_a_soft_404_site_reports_what_it_actually_got() -> None:
    """Both fixes together, through a real walk.

    robots.txt answers with HTML and every sitemap path 404s. The run must
    conclude there is no sitemap — that part was already right — and it must
    describe that conclusion in terms of what came back.
    """
    result = walk_site({"/robots.txt": _HTML_ERROR_PAGE})
    refused = [r for r in result.sitemaps if r.errorType is ErrorType.NO_SITEMAP]
    assert refused, [(r.kind, r.ok, r.errorType) for r in result.sitemaps]
    for record in refused:
        message = record.errorMessage or ""
        assert "robots.txt lists none" not in message, message
        assert "no well-known sitemap path responded" not in message, message
        assert "robots.txt" in message, message
        assert "candidate sitemap URL(s) tried" in message, message


# --------------------------------------------------------------------------

CASES = [
    test_site_with_no_sitemap_is_not_a_success,
    test_site_with_no_sitemap_still_has_a_discovery_record,
    test_pages_found_counts_the_file_not_the_subtree,
    test_discovery_scaffolding_is_labelled_not_called_a_sitemap,
    test_only_real_sitemap_files_are_counted_as_attempted,
    test_the_same_sitemap_file_reached_twice_is_one_record,
    test_robots_txt_is_readable_content,
    test_a_sitemap_named_only_in_robots_txt_is_found,
    test_a_robots_txt_naming_a_captcha_path_is_still_readable,
    test_html_at_robots_txt_is_not_reported_as_a_missing_sitemap,
    test_html_at_a_sitemap_path_still_says_a_sitemap,
    test_html_at_robots_txt_with_a_query_string_is_still_robots_txt,
    test_robots_txt_that_could_not_be_read_is_said_to_be_unreadable,
    test_robots_txt_read_and_holding_nothing_is_said_to_have_held_nothing,
    test_robots_txt_never_fetched_is_said_to_be_unconsulted,
    test_soft_404_site_never_claims_the_paths_did_not_respond,
    test_the_stated_candidate_count_is_the_number_actually_tried,
    test_end_to_end_a_soft_404_site_reports_what_it_actually_got,
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
