"""Turn a parsed sitemap tree into the unified output rows.

Two things the library cannot give us, both explicit in the spec:

* **raw ``<lastmod>``** — ``SitemapPage.last_modified`` is already a parsed
  ``datetime`` and the original string is gone. The plan requires keeping the
  verbatim string because ``<lastmod>`` is a zoo (``YYYY``, ``YYYY-MM``,
  ``+0800`` without a colon, RFC 822, ``2023-13-45``). We recover the raw
  value from the body text we already captured at fetch time.

* **rows survive bad dates** — a date that will not parse keeps the row and
  leaves ``lastmodIso`` empty. It never drops the row.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from usp.objects.page import SitemapPage
from usp.objects.sitemap import AbstractSitemap, InvalidSitemap

from .discover import WalkResult
from .models import DEFAULT_MAX_URLS, ErrorType, UrlRecord
from .normalize import InvalidUrl, host_of, is_same_site, normalize_url
from .ssrf import UnsupportedScheme

# Tolerant pairing of <loc> with an optional <lastmod> inside one <url> block.
# Real sitemaps ship with arbitrary prefixes (<sm:url>, <n:loc>) and with no
# namespace at all, and losing the raw <lastmod> because the generator prefixed
# its tags would break the "keep the verbatim value" promise for no reason.
_PREFIX = r"(?:[\w.-]+:)?"

_URL_BLOCK = re.compile(
    rf"<{_PREFIX}url\b[^>]*>(?P<body>.*?)</{_PREFIX}url\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Plain <loc> first: <image:loc> and <video:loc> live inside the same block and
# are not the page URL. A prefixed <loc> is only accepted when there is no
# plain one, which is the <sm:loc> shape.
_LOC_PLAIN = re.compile(
    r"<loc\b[^>]*>\s*(?P<v>.*?)\s*</loc\s*>", re.IGNORECASE | re.DOTALL
)
_LOC = re.compile(
    rf"<{_PREFIX}loc\b[^>]*>\s*(?P<v>.*?)\s*</{_PREFIX}loc\s*>",
    re.IGNORECASE | re.DOTALL,
)
_LASTMOD = re.compile(
    rf"<{_PREFIX}lastmod\b[^>]*>\s*(?P<v>.*?)\s*</{_PREFIX}lastmod\s*>",
    re.IGNORECASE | re.DOTALL,
)

MAX_LASTMOD_LEN = 64

_URL_OPEN = re.compile(rf"<{_PREFIX}url\b[^>]*>", re.IGNORECASE)
_DOCTYPE_OR_ENTITY = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _first_loc(inner: str) -> str | None:
    """The page URL of one ``<url>`` block. See ``_LOC_PLAIN``."""
    for pattern in (_LOC_PLAIN, _LOC):
        match = pattern.search(inner)
        if match:
            return _unescape(match.group("v"))
    return None


def salvage_locs(body: str) -> list[str]:
    """Recover ``<loc>`` values from a body the streaming parser stopped reading.

    A streaming parser stops at the first invalid token, so a bare ``&`` in the
    third entry of a sitemap throws away every entry after it — even though the
    ``<loc>`` values themselves are intact. That is the single most common way a
    real sitemap is malformed, and losing three quarters of the URLs over one
    unescaped ampersand is a worse answer than the user can get by eye.

    This is a recovery net, **not a second parser**. It only reads ``<url>``
    blocks (never ``<sitemap>`` — those are sitemap files, not pages), and every
    value still goes through ``normalize_url`` and the same cross-domain policy
    as a parsed row. Whatever the real parser found is emitted first and wins
    the dedup.

    Refused outright for any body carrying a DOCTYPE or ENTITY declaration.
    That is the billion-laughs signature, and re-extracting with a regex after
    the hardened parser has refused the file on security grounds would undo
    exactly the protection that refusal provides.
    """
    if not body:
        return []
    if _DOCTYPE_OR_ENTITY.search(body[:4096].encode("utf-8", "ignore")):
        return []

    out: list[str] = []
    try:
        starts = [m.end() for m in _URL_OPEN.finditer(body)]
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(body)
            value = _first_loc(body[start:end])
            if value and value not in out:
                out.append(value)
    except Exception:
        return out
    return out


def extract_raw_lastmods(body: str) -> dict[str, str]:
    """Map URL -> verbatim ``<lastmod>`` text, keyed by the raw ``<loc>``.

    Best-effort and non-fatal: an unparseable body simply yields an empty map.
    """
    out: dict[str, str] = {}
    if not body or "<" not in body:
        return out
    try:
        for block in _URL_BLOCK.finditer(body):
            inner = block.group("body")
            loc = _first_loc(inner)
            if not loc:
                continue
            last_m = _LASTMOD.search(inner)
            if not last_m:
                continue
            raw = _unescape(last_m.group("v"))[:MAX_LASTMOD_LEN]
            if loc and raw and loc not in out:
                out[loc] = raw
    except Exception:
        return out
    return out


def _unescape(value: str) -> str:
    return (
        value.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .strip()
    )


def best_effort_iso(raw: str | None) -> str | None:
    """Parse a ``<lastmod>`` string as best we can. Returns None, never raises.

    Covers the common shapes only: ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, an
    optional time, optional fractional seconds, optional ``Z`` or numeric
    offset (with or without the colon, which real sitemaps get wrong).
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    # Bare year / year-month.
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-01-01"
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return f"{text}-01"

    candidate = text
    # Normalize "+0800" -> "+08:00" and "2024-01-01 12:00:00" -> "T" separator.
    candidate = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", candidate)
    candidate = candidate.replace(" ", "T", 1)

    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # RFC 822 / RFC 2822 shows up in RSS-derived sitemaps.
    try:
        parsed = datetime.strptime(text, "%a, %d %b %Y %H:%M:%S %z")
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass

    return None


def collect_rows(
    result: WalkResult,
    *,
    seed_url: str,
    raw_bodies: dict[str, str] | None = None,
    include_subdomains: bool = False,
    max_urls: int = DEFAULT_MAX_URLS,
) -> tuple[list[UrlRecord], dict[str, int]]:
    """Flatten the tree into rows, dedup, and mark cross-domain entries.

    Returns the rows plus counters. Dedup runs before status checking, which is
    where the 10-30% request saving comes from.
    """
    raw_bodies = raw_bodies or {}
    try:
        seed_normalized = normalize_url(seed_url)
    except (InvalidUrl, UnsupportedScheme):
        seed_normalized = seed_url
    seed_host_name = host_of(seed_normalized)

    rows: list[UrlRecord] = []
    seen: set[str] = set()
    counters = {
        "discovered": 0,
        "emitted": 0,
        "deduped": 0,
        "cross_domain": 0,
        "invalid_url": 0,
        "salvaged": 0,
        "truncated_at_max_urls": 0,
    }

    if result.root is None:
        return rows, counters

    # Pre-extract raw lastmods per source sitemap file.
    lastmod_by_source: dict[str, dict[str, str]] = {}
    for source_url, body in raw_bodies.items():
        lastmod_by_source[source_url] = extract_raw_lastmods(body)

    def add(row: UrlRecord | None) -> None:
        """Dedup and append one row. Shared by the parsed and salvaged paths."""
        if row is None:
            counters["invalid_url"] += 1
            return
        key = row.normalizedUrl
        if key in seen:
            counters["deduped"] += 1
            return
        if len(rows) >= max_urls:
            counters["truncated_at_max_urls"] += 1
            return
        seen.add(key)
        if row.crossDomain:
            counters["cross_domain"] += 1
        rows.append(row)
        counters["emitted"] += 1

    depth_by_source: dict[str, int] = {}
    parsed_by_source: dict[str, int] = {}

    def walk_nodes(node: AbstractSitemap, depth: int) -> None:
        source = node.url
        raw_map = lastmod_by_source.get(source, {})
        depth_by_source.setdefault(source, depth)

        if isinstance(node, InvalidSitemap):
            return

        for page in node.all_pages():
            if not isinstance(page, SitemapPage):
                continue
            parsed_by_source[source] = parsed_by_source.get(source, 0) + 1
            counters["discovered"] += 1
            if len(rows) >= max_urls:
                counters["truncated_at_max_urls"] += 1
                continue

            add(
                _page_to_row(
                    page,
                    source_sitemap=source,
                    depth=depth,
                    raw_map=raw_map,
                    seed_host=seed_host_name,
                    include_subdomains=include_subdomains,
                )
            )

        for child in node.sub_sitemaps:
            walk_nodes(child, depth + 1)

    walk_nodes(result.root, 0)

    # Recovery net for bodies the streaming parser stopped reading part-way.
    # Runs after the parsed rows so the real parser always wins the dedup.
    for source_url, body in raw_bodies.items():
        if len(rows) >= max_urls:
            break
        # Only reach for the net when the parser demonstrably left something
        # behind. Re-scanning a body that parsed in full costs a second pass
        # over every URL and then throws the result away at the dedup — on a
        # 50,000-entry sitemap that alone was a 5x slowdown for no gain.
        block_count = len(_URL_OPEN.findall(body))
        if block_count <= parsed_by_source.get(source_url, 0):
            continue
        candidates = salvage_locs(body)
        if not candidates:
            continue
        raw_map = lastmod_by_source.get(source_url, {})
        depth = depth_by_source.get(source_url, 0)
        before = len(rows)
        for raw_url in candidates:
            if len(rows) >= max_urls:
                break
            counters["discovered"] += 1
            add(
                _salvaged_row(
                    raw_url,
                    source_sitemap=source_url,
                    depth=depth,
                    raw_map=raw_map,
                    seed_host=seed_host_name,
                    include_subdomains=include_subdomains,
                )
            )
        counters["salvaged"] += len(rows) - before

    return rows, counters


def _page_to_row(
    page: SitemapPage,
    *,
    source_sitemap: str,
    depth: int,
    raw_map: dict[str, str],
    seed_host: str,
    include_subdomains: bool,
) -> UrlRecord | None:
    raw_url = page.url
    try:
        normalized = normalize_url(raw_url)
    except (InvalidUrl, UnsupportedScheme, ValueError) as exc:
        # Emit the row with an error marker rather than dropping it silently.
        # ValueError covers a port that urlsplit itself rejects (:99999, :abc),
        # which would otherwise escape as a bare exception and take the run
        # down with one bad <loc>.
        return UrlRecord(
            url=raw_url,
            normalizedUrl=raw_url,
            sourceSitemap=source_sitemap,
            depth=depth,
            errorType=ErrorType.INVALID_INPUT,
            errorMessage=str(exc),
        )

    raw_lastmod = raw_map.get(raw_url) or raw_map.get(normalized)
    if raw_lastmod is None and page.last_modified is not None:
        # `last_modified` is a datetime on the normal parse path, but the
        # library leaves it a plain string when the value survives parsing in
        # a non-standard shape. Either way the row must survive.
        parsed = page.last_modified
        raw_lastmod = (
            parsed.isoformat() if hasattr(parsed, "isoformat") else str(parsed)
        )

    cross_domain = not is_same_site(
        normalized, seed_host, include_subdomains=include_subdomains
    )
    row = UrlRecord(
        url=raw_url,
        normalizedUrl=normalized,
        sourceSitemap=source_sitemap,
        depth=depth,
        lastmodRaw=raw_lastmod,
        lastmodIso=best_effort_iso(raw_lastmod),
        priority=_priority_to_float(page.priority),
        changeFrequency=_freq_to_str(page.change_frequency),
        hasImages=bool(page.images),
        hasNews=page.news_story is not None,
        alternateCount=len(page.alternates or ()),
        crossDomain=cross_domain,
    )
    if cross_domain:
        _mark_cross_domain(row)
    return row


def _salvaged_row(
    raw_url: str,
    *,
    source_sitemap: str,
    depth: int,
    raw_map: dict[str, str],
    seed_host: str,
    include_subdomains: bool,
) -> UrlRecord | None:
    """Row for a URL the streaming parser never reached.

    Same normalisation and the same cross-domain policy as a parsed row; the
    metadata fields stay empty, because claiming a priority the parser never
    read would be inventing data. ``<lastmod>`` is still recovered from the
    raw block where one is present.
    """
    try:
        normalized = normalize_url(raw_url)
    except (InvalidUrl, UnsupportedScheme, ValueError) as exc:
        return UrlRecord(
            url=raw_url,
            normalizedUrl=raw_url,
            sourceSitemap=source_sitemap,
            depth=depth,
            errorType=ErrorType.INVALID_INPUT,
            errorMessage=str(exc),
        )

    raw_lastmod = raw_map.get(raw_url) or raw_map.get(normalized)
    cross_domain = not is_same_site(
        normalized, seed_host, include_subdomains=include_subdomains
    )
    row = UrlRecord(
        url=raw_url,
        normalizedUrl=normalized,
        sourceSitemap=source_sitemap,
        depth=depth,
        lastmodRaw=raw_lastmod,
        lastmodIso=best_effort_iso(raw_lastmod),
        crossDomain=cross_domain,
    )
    if cross_domain:
        _mark_cross_domain(row)
    return row


def _mark_cross_domain(row: UrlRecord) -> None:
    """Marked AND excluded from probing. A sitemap that lists a CDN or a
    partner host must not make us send requests to that third party: it is not
    the host the user asked about, and doing it at scale is what gets a crawler
    flagged."""
    row.errorType = ErrorType.CROSS_DOMAIN
    row.errorMessage = "cross-domain <loc>; recorded but not probed"


def _priority_to_float(priority: Decimal | None) -> float | None:
    if priority is None:
        return None
    try:
        value = float(priority)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if 0.0 <= value <= 1.0:
        return value
    return None


def _freq_to_str(freq: object) -> str | None:
    if freq is None:
        return None
    value = getattr(freq, "value", None)
    if isinstance(value, str):
        return value
    text = str(freq)
    return text.rsplit(".", 1)[-1].lower() if text else None
