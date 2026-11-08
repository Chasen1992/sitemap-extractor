"""Sitemap discovery.

ultimate-sitemap-parser already does the hard parts: robots.txt ``Sitemap:``
directives, well-known paths, sitemaps that appear in neither, index-of-index
recursion, and cycle protection via a parent-URL set. It accepts a
``web_client`` and a ``recurse_callback``, so the budget, depth and visited-set
guards belong in the callback rather than in a fork of the library.

The library's own hardening is load-bearing and verified in source: DOCTYPE and
ENTITY declarations raise, parameter-entity parsing is off, malformed XML is
salvaged instead of discarded, and gzip is bounded by ``max_output_bytes``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from usp.fetch_parse import SitemapFetcher
from usp.objects.sitemap import (
    AbstractIndexSitemap,
    AbstractPagesSitemap,
    AbstractSitemap,
    InvalidSitemap,
)
from usp.tree import (
    IndexRobotsTxtSitemap,
    IndexWebsiteSitemap,
    sitemap_tree_for_homepage,
)

from .budget import Budget
from .models import ErrorType, SitemapRecord, SYNTHETIC_KINDS
from .normalize import host_of, normalize_url, InvalidUrl
from .ssrf import SsrfBlocked, UnsupportedScheme
from .transport import CompliantWebClient

log = logging.getLogger(__name__)

# Well-known sitemap locations beyond robots.txt. WordPress in particular
# publishes wp-sitemap.xml and never lists it in robots.txt on some setups.
DEFAULT_KNOWN_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemapindex.xml",
    "/sitemap1.xml",
    "/wp-sitemap.xml",
    "/sitemap.xml.gz",
    "/sitemap.txt",
)

# A seed that is itself a sitemap is used directly rather than walked from
# the homepage.
SITEMAP_PATH_HINTS = (
    "sitemap.xml",
    "sitemap_index.xml",
    "sitemap-index.xml",
    "sitemapindex.xml",
    "wp-sitemap.xml",
    "sitemap.txt",
    ".xml.gz",
)


@dataclass
class WalkResult:
    """Everything the walk produced, including what it refused to do."""

    root: AbstractSitemap | None = None
    sitemaps: list[SitemapRecord] = field(default_factory=list)
    skipped_budget: int = 0
    skipped_depth: int = 0
    skipped_cycle: int = 0
    skipped_disabled: int = 0
    failure_by_url: dict[str, tuple[ErrorType, str]] = field(default_factory=dict)


def looks_like_sitemap_url(url: str) -> bool:
    lowered = url.split("?", 1)[0].lower()
    return any(hint in lowered for hint in SITEMAP_PATH_HINTS)


def _looks_like_file(url: str) -> bool:
    """A path that ends in a file extension is a document, not a homepage.

    Sitemaps do not always live at a recognisable name: ``/feeds/all.xml`` is
    still a sitemap the user is pointing at on purpose. A directory-style path
    is left to homepage discovery instead of being fetched as a sitemap.
    """
    path = urlsplit(url).path
    if not path or path.endswith("/"):
        return False
    name = path.rsplit("/", 1)[-1]
    return "." in name


def walk(
    seed_url: str,
    *,
    budget: Budget,
    client: CompliantWebClient,
    max_depth: int = 3,
    follow_index: bool = True,
    use_robots: bool = True,
    use_known_paths: bool = True,
    extra_known_paths: set[str] | None = None,
) -> WalkResult:
    """Discover and parse sitemaps for one seed.

    Never raises for a per-sitemap problem: a bad file becomes a SitemapRecord
    carrying its errorType, and the walk continues.
    """
    result = WalkResult()
    visited: set[str] = set()

    try:
        normalized = normalize_url(seed_url)
    except (InvalidUrl, UnsupportedScheme) as exc:
        result.sitemaps.append(
            SitemapRecord(
                url=seed_url,
                kind="invalid",
                ok=False,
                errorType=ErrorType.INVALID_INPUT,
                errorMessage=str(exc),
            )
        )
        return result

    known_paths = set(DEFAULT_KNOWN_PATHS)
    if extra_known_paths:
        known_paths |= extra_known_paths

    # A direct sitemap URL is fetched as a seed rather than walked from the
    # homepage, so users can point at the exact file they care about.
    direct = looks_like_sitemap_url(normalized) or _looks_like_file(normalized)

    # The seed is part of the cycle guard too. Without this, A -> B -> A is
    # only stopped by the library's parent-URL set: our own visited set never
    # sees the root, so the cycle is neither counted nor refused here.
    visited.add(normalized.split("#", 1)[0])

    def recurse_callback(url: str, level: int, parents: set[str]) -> bool:
        """Return False to refuse a child sitemap. This is the guard point."""
        if not follow_index:
            result.skipped_disabled += 1
            return False
        if level > max_depth:
            result.skipped_depth += 1
            return False
        key = url.split("#", 1)[0]
        if key in visited:
            result.skipped_cycle += 1
            return False
        try:
            budget.check()
        except Exception:
            result.skipped_budget += 1
            return False
        visited.add(key)
        return True

    callback = recurse_callback if follow_index else _refuse_all(result)

    try:
        if direct:
            # sitemap_tree_for_homepage never fetches the URL you give it as a
            # sitemap: it only uses the homepage to find robots.txt and the
            # well-known paths. Pointing it at /sitemap.xml therefore returned
            # an empty website node and zero requests. Fetch the seed itself.
            root = SitemapFetcher(
                url=normalized,
                web_client=client,
                recursion_level=0,
                parent_urls=set(),
                recurse_callback=callback,
            ).sitemap()
        else:
            root = sitemap_tree_for_homepage(
                normalized,
                web_client=client,
                use_robots=use_robots,
                use_known_paths=use_known_paths,
                extra_known_paths=known_paths,
                recurse_callback=callback,
            )
    except Exception as exc:
        # Structural failure of the walk itself. Report it; never crash the run.
        log.warning("sitemap walk failed for %s: %s", normalized, exc)
        result.sitemaps.append(
            SitemapRecord(
                url=normalized,
                kind="invalid",
                ok=False,
                errorType=ErrorType.UNEXPECTED,
                errorMessage=f"walk failed: {exc}",
            )
        )
        result.failure_by_url.update(client.failures)
        return result

    result.root = root
    result.failure_by_url.update(client.failures)
    _collect_sitemap_records(root, result, depth=0, seen=set())
    _mark_empty_discovery(result)
    return result


def _refuse_all(result: WalkResult):
    """Recurse callback for follow_index=False: refuse, and say why."""

    def _cb(url: str, level: int, parents: set[str]) -> bool:
        result.skipped_disabled += 1
        return False

    return _cb


def _collect_sitemap_records(
    node: AbstractSitemap, result: WalkResult, *, depth: int, seen: set[str]
) -> None:
    """Record every sitemap file in the tree, success or failure.

    One record per **file**, not per visit. The same file is routinely reached
    twice — robots.txt lists it and a well-known path serves it — and the tree
    then holds two nodes for it. Counting both overstates how many sitemap
    files a site publishes, which is the number the summary reports. The
    identity used to collapse them is ``normalize_url``, the same one used for
    page URLs, so a URL that arrives spelled differently (USP joins some
    robots.txt paths with a doubled slash) still lands on the same file.
    """
    record = SitemapRecord(url=node.url, depth=depth)
    if isinstance(node, InvalidSitemap):
        record.kind = "invalid"
        record.ok = False
        reason = getattr(node, "reason", "") or "invalid sitemap"
        error_type, message = result.failure_by_url.get(node.url, (ErrorType.PARSE_ERROR, reason))
        record.errorType = error_type
        record.errorMessage = message or reason
    else:
        record.ok = True
        record.kind = _kind_of(node)
        # NOTE: `sub_sitemaps` is a property returning a list, unlike the
        # `all_pages()` generator next to it. Calling it is a TypeError that
        # kills the walk after it has already succeeded.
        record.childSitemaps = len(node.sub_sitemaps)
        # Pages listed in this file. `all_pages()` on an index node walks the
        # whole subtree, so asking an index would report every URL once per
        # level of index above it — 922,814 pages on both the website node and
        # the sitemap index that actually held them.
        record.pagesFound = (
            sum(1 for _ in node.all_pages()) if isinstance(node, AbstractPagesSitemap) else 0
        )

    # One record per file. The children are still walked on a repeat: a second
    # occurrence can carry children the first did not, and dropping them would
    # lose sitemap files outright.
    key = _file_identity(node.url)
    if key not in seen:
        seen.add(key)
        result.sitemaps.append(record)

    for child in node.sub_sitemaps:
        _collect_sitemap_records(child, result, depth=depth + 1, seen=seen)


def _file_identity(url: str) -> str:
    """A comparable identity for a sitemap file.

    Falls back to the raw string when the URL is one ``normalize_url`` refuses;
    a sitemap file named by a broken URL still has to be reported, and the raw
    string is the only identity available for it.
    """
    try:
        return normalize_url(url)
    except Exception:
        return url


def _kind_of(node: AbstractSitemap) -> str:
    if isinstance(node, IndexWebsiteSitemap):
        return "website"
    if isinstance(node, IndexRobotsTxtSitemap):
        return "robots"
    if isinstance(node, AbstractIndexSitemap):
        return "sitemapindex"
    if isinstance(node, AbstractPagesSitemap):
        return _pages_kind(node)
    return "unknown"


def _mark_empty_discovery(result: WalkResult) -> None:
    """Say so when the walk found no sitemap file at all.

    Discovery always produces a tree — at minimum the synthetic website node —
    so "we looked and the site has none" and "we read 25 sitemap files" both
    arrive here as a non-empty list. The difference is whether any real sitemap
    file is in it, and without this pass the first case is reported as a
    successful sitemap index containing nothing.

    A site with no sitemap is a fact about the site, not a tool failure, and
    this is the only place that fact can be recorded. Leaving it as a success
    is how a run against a sitemap-less site reads as "0/0 URLs found",
    which is indistinguishable from an empty sitemap and from a broken run.
    """
    if any(r.ok and r.kind not in SYNTHETIC_KINDS for r in result.sitemaps):
        return
    for record in result.sitemaps:
        if record.kind not in SYNTHETIC_KINDS:
            continue
        record.ok = False
        record.errorType = ErrorType.NO_SITEMAP
        record.errorMessage = (
            "no sitemap found at this site: robots.txt lists none and no "
            "well-known sitemap path responded"
        )


def _pages_kind(node: AbstractSitemap) -> str:
    name = type(node).__name__
    if "Text" in name:
        return "text"
    if "RSS" in name:
        return "rss"
    if "Atom" in name:
        return "atom"
    if "XML" in name:
        return "urlset"
    return "pages"


def seed_host(seed_url: str) -> str:
    return host_of(seed_url)
