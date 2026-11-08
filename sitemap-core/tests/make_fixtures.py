"""Generate the adversarial fixture corpus.

Regenerable and reviewable in one place: every fixture the reliability claim
rests on is defined here as a literal or a tiny generator. Run
``python tests/make_fixtures.py`` to (re)write ``tests/fixtures/``.

The corpus is the definition of "done properly". Non-specialists cannot judge
whether a parser is robust by feel, so the claim is a measured number against
these samples instead.

Acceptance line: **>= 98% of fixtures handled, and no single bad sample may
kill the run.** A fixture "handled" means the tool produced the outcome this
manifest declares, which for hostile samples is a clean errorType rather than
a crash or garbage rows.
"""

from __future__ import annotations

import gzip
import io
import pathlib
import sys

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------
# Hand-written samples
# --------------------------------------------------------------------------

PLAIN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc><lastmod>2024-03-05</lastmod><priority>1.0</priority></url>
  <url><loc>https://example.com/about</loc><lastmod>2024-03-06T10:20:30Z</lastmod><changefreq>daily</changefreq></url>
  <url><loc>https://example.com/products/widget</loc><priority>0.8</priority></url>
  <url><loc>https://example.com/contact</loc></url>
</urlset>
"""

# No XML namespace at all. Extremely common output of old CMS plugins.
NO_NAMESPACE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset>
  <url><loc>https://no-ns.example/page-1</loc></url>
  <url><loc>https://no-ns.example/page-2</loc><lastmod>2024-01-02</lastmod></url>
  <url><loc>https://no-ns.example/page-3</loc></url>
</urlset>
"""

# Arbitrary prefix on the elements (sm:, x:, n:). Namespace-insensitive parsing
# must accept all of them.
BAD_PREFIX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sm:urlset xmlns:sm="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sm:url><sm:loc>https://prefix.example/a</sm:loc></sm:url>
  <sm:url><sm:loc>https://prefix.example/b</sm:loc></sm:url>
</sm:urlset>
"""

# Malformed: bare ampersand, unclosed tag, stray close tag. Must salvage what
# it can rather than returning nothing.
MALFORMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://malformed.example/good-1</loc></url>
  <url><loc>https://malformed.example/search?q=a&b=c</loc></url>
  <url><loc>https://malformed.example/good-2</loc>
  <url><loc>https://malformed.example/good-3</loc></url>
  </wrong>
</urlset>
"""

EMPTY_XML = ""

# Truncated mid-element, as servers do when they time out on huge sitemaps.
TRUNCATED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://truncated.example/one</loc></url>
  <url><loc>https://truncated.example/two</loc></url>
  <url><loc>https://truncated.example/three</loc>
"""

# A WAF / bot-challenge page served at /sitemap.xml with HTTP 200 and
# text/html. Without sniffing this gets parsed as a plain-text sitemap and the
# page's navigation links are emitted as discovered URLs.
HTML_CHALLENGE = """<!DOCTYPE html>
<html lang="en">
<head><title>Just a moment...</title></head>
<body>
  <h1>Attention Required!</h1>
  <p>Please enable JavaScript and cookies to continue.</p>
  <p>Verify you are human by completing the security check below.</p>
  <a href="https://challenge.example/cdn-cgi/challenge-platform">Cloudflare</a>
  <a href="https://challenge.example/pricing">Pricing</a>
  <a href="https://challenge.example/docs">Docs</a>
</body>
</html>
"""

# An HTML error page posing as robots.txt. It must not be mistaken for a
# robots file full of Sitemap: directives.
ROBOTS_HTML = """<!DOCTYPE html>
<html><head><title>404 Not Found</title></head>
<body><h1>Not Found</h1><p>The page you requested is not available.</p></body>
</html>
"""

# A billion-laughs payload. Ultimate-sitemap-parser's expat setup rejects
# DOCTYPE and ENTITY declarations; this proves it stays rejected.
BILLION_LAUGHS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://bomb.example/&lol5;</loc></url>
</urlset>
"""

# Extension namespaces (image / news / xhtml:link) must be ignored, not fatal.
NEWS_IMAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url>
    <loc>https://news.example/article-1</loc>
    <lastmod>2024-05-01</lastmod>
    <image:image>
      <image:loc>https://news.example/img/1.jpg</image:loc>
      <image:title>Headline</image:title>
    </image:image>
    <news:news>
      <news:publication><news:name>Example Times</news:name></news:publication>
      <news:publication_date>2024-05-01T00:00:00Z</news:publication_date>
      <news:title>Headline</news:title>
    </news:news>
    <xhtml:link rel="alternate" hreflang="fr" href="https://news.example/fr/article-1"/>
  </url>
  <url><loc>https://news.example/article-2</loc></url>
</urlset>
"""

# The <lastmod> zoo. Every row must survive; the raw string must be kept even
# when it cannot be parsed.
BAD_DATES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://dates.example/bare-year</loc><lastmod>2024</lastmod></url>
  <url><loc>https://dates.example/year-month</loc><lastmod>2024-03</lastmod></url>
  <url><loc>https://dates.example/date-only</loc><lastmod>2024-03-05</lastmod></url>
  <url><loc>https://dates.example/utc</loc><lastmod>2024-03-05T10:20:30Z</lastmod></url>
  <url><loc>https://dates.example/offset-no-colon</loc><lastmod>2024-03-05T10:20:30+0800</lastmod></url>
  <url><loc>https://dates.example/offset-colon</loc><lastmod>2024-03-05T10:20:30+08:00</lastmod></url>
  <url><loc>https://dates.example/space-sep</loc><lastmod>2024-03-05 10:20:30</lastmod></url>
  <url><loc>https://dates.example/rfc822</loc><lastmod>Tue, 05 Mar 2024 10:20:30 +0000</lastmod></url>
  <url><loc>https://dates.example/impossible</loc><lastmod>2023-13-45</lastmod></url>
  <url><loc>https://dates.example/garbage</loc><lastmod>not a date at all</lastmod></url>
  <url><loc>https://dates.example/empty</loc><lastmod></lastmod></url>
  <url><loc>https://dates.example/no-lastmod</loc></url>
</urlset>
"""

# Non-ASCII host and path. A naive implementation raises.
IDNA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://münchen.example/über-uns</loc></url>
  <url><loc>https://xn--mnchen-3ya.example/about</loc></url>
  <url><loc>https://пример.example/новости</loc></url>
  <url><loc>https://idna.example/中文/页面</loc></url>
</urlset>
"""

# Duplicates: same URL at index and child level, and http/https, www/apex and
# fragment variants of each other.
DUP_ENTRIES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <!-- Seven spellings of ONE url. A fragment, a default port, a doubled
       slash, a dot segment and a percent-escaped unreserved character are all
       duplicate noise and must collapse to a single row. -->
  <url><loc>https://dup.example/page</loc></url>
  <url><loc>https://dup.example/page</loc></url>
  <url><loc>https://dup.example/page#section</loc></url>
  <url><loc>https://dup.example:443/page</loc></url>
  <url><loc>https://dup.example//page</loc></url>
  <url><loc>https://dup.example/./page</loc></url>
  <url><loc>https://dup.example/pa%67e</loc></url>
  <url><loc>https://dup.example/other</loc></url>
  <!-- Deliberately NOT duplicates. Scheme, host and trailing slash are real
       differences: collapsing any of them would report a live URL as dead. -->
  <url><loc>http://dup.example/page</loc></url>
  <url><loc>https://www.dup.example/page</loc></url>
  <url><loc>https://dup.example/page/</loc></url>
</urlset>
"""

SITEMAP_TXT = """# sitemap in plain text form
https://text.example/one
https://text.example/two

https://text.example/three
https://text.example/four
"""

# Cross-domain <loc> values: CDN and partner hosts. Emitted and marked, never
# probed.
CROSS_DOMAIN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://main.example/local-page</loc></url>
  <url><loc>https://cdn.partner-network.example/asset-page</loc></url>
  <url><loc>https://www.main.example/another</loc></url>
  <url><loc>https://tracking.third-party-example.net/redirect</loc></url>
</urlset>
"""

# A URL that must never be fetched, for the SSRF guard.
SSRF_LURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ssrf-lure.example/safe</loc></url>
  <url><loc>http://169.254.169.254/latest/meta-data/</loc></url>
  <url><loc>http://127.0.0.1:8080/admin</loc></url>
  <url><loc>http://192.168.0.1/router</loc></url>
  <url><loc>http://10.0.0.5/internal</loc></url>
</urlset>
"""


# --------------------------------------------------------------------------
# Generated samples
# --------------------------------------------------------------------------

def make_gzipped(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def make_double_gzip(data: bytes) -> bytes:
    return make_gzipped(make_gzipped(data))


def make_huge(count: int = 50_000) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for i in range(count):
        parts.append(f"  <url><loc>https://huge.example/p/{i}</loc></url>")
    parts.append("</urlset>")
    return "\n".join(parts)


INDEX_OF_INDEX_ROOT = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{base}/fixtures/wp_pages.xml</loc></sitemap>
  <sitemap><loc>{base}/fixtures/wp_posts.xml</loc></sitemap>
</sitemapindex>
"""

INDEX_OF_INDEX_MID = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{base}/fixtures/wp_pages.xml</loc></sitemap>
  <sitemap><loc>{base}/fixtures/plain.xml</loc></sitemap>
</sitemapindex>
"""

WP_PAGES = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://wp.example/</loc><lastmod>2024-04-01</lastmod></url>
  <url><loc>https://wp.example/about</loc></url>
</urlset>
"""

WP_POSTS = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://wp.example/2024/01/post-one</loc></url>
  <url><loc>https://wp.example/2024/02/post-two</loc></url>
  <url><loc>https://wp.example/2024/03/post-three</loc></url>
</urlset>
"""

# A -> B -> A. Termination must come from the visited set, not from luck, and
# the walk must still harvest the sibling branch: a cycle that is "handled" by
# returning nothing is a hang in slow motion.
CYCLIC_A = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{base}/fixtures/cyclic_b.xml</loc></sitemap>
</sitemapindex>
"""

CYCLIC_B = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{base}/fixtures/cyclic_a.xml</loc></sitemap>
  <sitemap><loc>{base}/fixtures/cyclic_leaf.xml</loc></sitemap>
</sitemapindex>
"""

# Harvestable pages beside the cycle. Lives in its own urlset because a
# <sitemapindex> is a list of sitemap files, not of pages, and the parser is
# right to ignore a bare <url> inside one.
CYCLIC_LEAF = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://cyclic.example/survivor</loc></url>
  <url><loc>https://cyclic.example/survivor-two</loc></url>
</urlset>
"""

# Status-probing fixture. All URLs point at the local fixture server so DNS is
# not the thing under test. Covers the P0 transport rules directly.
STATUS_PROBE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/probe/ok</loc></url>
  <url><loc>{base}/probe/missing</loc></url>
  <url><loc>{base}/probe/head-refused</loc></url>
  <url><loc>{base}/probe/rate-limited</loc></url>
  <url><loc>{base}/probe/ok</loc></url>
</urlset>
"""


# Served with a Content-Length far larger than the body: the fixture server
# lies about the length on purpose so the reader has to notice the body ended
# early. On disk it is just a valid-but-unfinished urlset.
TRUNCATED_BODY_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    "  <url><loc>https://trunc.example/one</loc></url>\n"
    "  <url><loc>https://trunc.example/two</loc></url>\n"
)

# ISO-8859-1 sitemap whose paths really use that charset. Written as latin-1
# bytes on purpose: the library decodes every body as UTF-8 before the XML
# parser runs, so the prolog's encoding= is ignored and 0xE9 lands as U+FFFD.
# The transport re-encodes and rewrites the prolog; this fixture is what proves
# it, because a UTF-8-written copy would pass either way.
LATIN1_XML = (
    '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    "  <url><loc>https://latin1.example/café</loc></url>\n"
    "  <url><loc>https://latin1.example/über</loc></url>\n"
    "  <url><loc>https://latin1.example/plain</loc></url>\n"
    "</urlset>\n"
)


def write_fixtures(base: str = "http://127.0.0.1:8765") -> pathlib.Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    plain_files: dict[str, str] = {
        "plain.xml": PLAIN_XML,
        "no_namespace.xml": NO_NAMESPACE_XML,
        "bad_prefix.xml": BAD_PREFIX_XML,
        "malformed.xml": MALFORMED_XML,
        "empty.xml": EMPTY_XML,
        "truncated.xml": TRUNCATED_XML,
        "truncated_body.xml": TRUNCATED_BODY_XML,
        "html_challenge.xml": HTML_CHALLENGE,
        "robots_html.txt": ROBOTS_HTML,
        "billion_laughs.xml": BILLION_LAUGHS,
        "news_image.xml": NEWS_IMAGE_XML,
        "bad_dates.xml": BAD_DATES_XML,
        "idna.xml": IDNA_XML,
        "dup_entries.xml": DUP_ENTRIES_XML,
        "sitemap.txt": SITEMAP_TXT,
        "cross_domain.xml": CROSS_DOMAIN_XML,
        "ssrf_lure.xml": SSRF_LURE_XML,
        "wp_pages.xml": WP_PAGES,
        "wp_posts.xml": WP_POSTS,
        "index_of_index.xml": INDEX_OF_INDEX_ROOT.format(base=base),
        "index_mid.xml": INDEX_OF_INDEX_MID.format(base=base),
        "cyclic_a.xml": CYCLIC_A.format(base=base),
        "cyclic_b.xml": CYCLIC_B.format(base=base),
        "cyclic_leaf.xml": CYCLIC_LEAF,
        "status_probe.xml": STATUS_PROBE_XML.format(base=base),
    }

    for name, content in plain_files.items():
        (FIXTURES_DIR / name).write_text(content, encoding="utf-8")

    # Binary / bulk samples.
    (FIXTURES_DIR / "latin1.xml").write_bytes(LATIN1_XML.encode("iso-8859-1"))
    (FIXTURES_DIR / "gzipped.xml.gz").write_bytes(make_gzipped(PLAIN_XML.encode("utf-8")))
    (FIXTURES_DIR / "double_gzip.xml.gz").write_bytes(
        make_double_gzip(PLAIN_XML.encode("utf-8"))
    )
    # Named .gz but is neither gzip nor XML: must be NOT_GZIP, never garbage rows.
    (FIXTURES_DIR / "fake_gz.xml.gz").write_bytes(HTML_CHALLENGE.encode("utf-8"))
    # Named .gz but is plain XML: a renamed file, which is harmless.
    (FIXTURES_DIR / "renamed.xml.gz").write_bytes(PLAIN_XML.encode("utf-8"))
    # Compression bomb: tiny input, huge declared expansion.
    (FIXTURES_DIR / "gzip_bomb.xml.gz").write_bytes(make_gzipped(b"\x00" * (24 * 1024 * 1024)))
    (FIXTURES_DIR / "huge.xml").write_text(make_huge(), encoding="utf-8")

    return FIXTURES_DIR


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    out = write_fixtures(base)
    total = sum(1 for p in out.iterdir() if p.is_file())
    size = sum(p.stat().st_size for p in out.iterdir() if p.is_file())
    print(f"wrote {total} fixtures to {out} ({size / 1024 / 1024:.2f} MB)")
