"""Unified output schema shared by the Actor and the change-monitor product.

One stable shape for both products is deliberate: it is the "接口形状" other
workflows will depend on, which is one of the three real moats.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any


# The shipped default for ``maxUrls``, kept in one place on purpose. It appears
# in this package's signatures, in the Actor's input schema and in the README's
# timings table, and a copy in each is a chance for one of them to be the stale
# one — the stale copy being what a user actually gets when they leave the
# field empty. The number is measured, not guessed: on a site publishing 24
# sitemap files with 3.69M URLs in them, 1 000 rows with status checking on is
# about seven minutes. The previous 10 000 was a run nobody finished.
DEFAULT_MAX_URLS = 1_000


class ErrorType(str, enum.Enum):
    """Failure taxonomy.

    Free-text errors cannot be filtered, so every non-success record carries
    one of these. Anything unanticipated maps to UNEXPECTED so a run still
    finishes and stays auditable instead of dying.
    """

    # --- transport ---
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    DNS = "dns"
    TLS = "tls"
    REDIRECT_LOOP = "redirect_loop"
    RATE_LIMITED = "rate_limited"
    # persistent 403 / challenge page -> record and walk away, never bypass
    BLOCKED = "blocked"

    # --- content ---
    PARSE_ERROR = "parse_error"
    NOT_XML = "not_xml"
    HTML_CHALLENGE = "html_challenge"
    EMPTY_BODY = "empty_body"
    TRUNCATED = "truncated"
    BAD_GZIP = "bad_gzip"
    NOT_GZIP = "not_gzip"
    GZIP_BOMB = "gzip_bomb"
    TOO_LARGE = "too_large"

    # --- policy / budget ---
    SKIPPED_BUDGET = "skipped_budget"
    SKIPPED_DEPTH = "skipped_depth"
    SKIPPED_CYCLE = "skipped_cycle"
    CROSS_DOMAIN = "cross_domain"
    SSRF_BLOCKED = "ssrf_blocked"
    UNSUPPORTED_SCHEME = "unsupported_scheme"

    # --- control ---
    INVALID_INPUT = "invalid_input"
    NOT_CHECKED = "not_checked"
    # discovery looked in robots.txt and at every well-known path and found
    # nothing. A fact about the site, and reported as one — never as a success.
    NO_SITEMAP = "no_sitemap"
    UNEXPECTED = "unexpected"


# Terminal states that count as "we tried and it failed", as opposed to
# "we deliberately did not try" (CROSS_DOMAIN / NOT_CHECKED / SKIPPED_*).
FAILURE_TYPES: frozenset[ErrorType] = frozenset(
    {
        ErrorType.HTTP_ERROR,
        ErrorType.TIMEOUT,
        ErrorType.DNS,
        ErrorType.TLS,
        ErrorType.REDIRECT_LOOP,
        ErrorType.RATE_LIMITED,
        ErrorType.BLOCKED,
        ErrorType.PARSE_ERROR,
        ErrorType.NOT_XML,
        ErrorType.HTML_CHALLENGE,
        ErrorType.EMPTY_BODY,
        ErrorType.TRUNCATED,
        ErrorType.BAD_GZIP,
        ErrorType.NOT_GZIP,
        ErrorType.GZIP_BOMB,
        ErrorType.TOO_LARGE,
        ErrorType.SSRF_BLOCKED,
        ErrorType.UNSUPPORTED_SCHEME,
        ErrorType.NO_SITEMAP,
        ErrorType.UNEXPECTED,
    }
)

SKIPPED_TYPES: frozenset[ErrorType] = frozenset(
    {
        ErrorType.SKIPPED_BUDGET,
        ErrorType.SKIPPED_DEPTH,
        ErrorType.SKIPPED_CYCLE,
        ErrorType.CROSS_DOMAIN,
        ErrorType.NOT_CHECKED,
    }
)

# SitemapRecord.kind values that are discovery scaffolding rather than sitemap
# files. The walk builds an IndexWebsiteSitemap to stand for "the site we are
# looking at" and an IndexRobotsTxtSitemap to stand for "the robots.txt we
# read"; both aggregate whatever was found beneath them. Reporting either as a
# sitemap index would claim a sitemap file exists where none does, and counting
# them as attempted inflates the tally with things never fetched as sitemaps.
SYNTHETIC_KINDS: frozenset[str] = frozenset({"website", "robots"})


@dataclass(slots=True)
class UrlRecord:
    """One emitted row: a URL discovered in a sitemap."""

    url: str
    normalizedUrl: str
    sourceSitemap: str | None = None
    depth: int = 0

    # status probing (checkStatusCodes=false -> all three stay None / NOT_CHECKED)
    checked: bool = False
    statusCode: int | None = None
    contentType: str | None = None

    # sitemap metadata. lastmodRaw is kept verbatim because <lastmod> is a zoo
    # (YYYY / YYYY-MM / +0800 without colon / RFC822 / impossible dates).
    # A date that fails to parse must never drop the row.
    lastmodRaw: str | None = None
    lastmodIso: str | None = None
    priority: float | None = None
    changeFrequency: str | None = None

    # sitemap extensions, surfaced but not interpreted
    hasImages: bool = False
    hasNews: bool = False
    alternateCount: int = 0

    crossDomain: bool = False
    errorType: ErrorType | None = None
    errorMessage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The row exactly as the dataset receives it.

        Fields that are ``None`` are **omitted**, not sent as ``null``. This is
        not tidiness — it is the fix for the worst defect this project has had.
        The dataset schema declares ``statusCode`` as ``integer``, ``contentType``
        as ``string`` and ``errorType`` as a 26-value enum, and a JSON Schema
        ``type`` does not admit ``null``. ``asdict`` kept every ``None``, so the
        platform answered ``Schema validation failed`` for **every** batch, and
        all three live runs reported 200 URLs behind an empty dataset. The
        summary counted what the parser produced; nobody counted what the
        dataset accepted.

        Dropping rather than declaring ``["integer", "null"]`` is deliberate.
        The schema's ``required`` is only ``["url", "normalizedUrl"]``, so an
        absent key is always legal; whether *this* validator accepts a nullable
        type is untested, and it has already refused things its own docs said
        were fine. Absent and null read the same in a table, and ``checked``
        already separates "not probed" from "probed and failed", so nothing is
        lost by omitting.
        """
        d = asdict(self)
        if self.errorType is not None:
            d["errorType"] = self.errorType.value
        return {key: value for key, value in d.items() if value is not None}


@dataclass(slots=True)
class SitemapRecord:
    """One sitemap file we attempted, including ones that failed.

    ``kind`` distinguishes real sitemap files from discovery scaffolding:
    ``website`` and ``robots`` are the synthetic nodes the walk builds to
    represent "the homepage we started from" and "the robots.txt we read".
    Neither is a sitemap file, and neither is counted as one.
    """

    url: str
    kind: str = "unknown"  # urlset | sitemapindex | text | rss | atom | invalid | website | robots
    ok: bool = False
    depth: int = 0
    # Pages listed in THIS file. An index file lists sitemaps, not pages, so
    # this is 0 for it — not the total across the subtree, which would count
    # every URL once per level of index above it.
    pagesFound: int = 0
    childSitemaps: int = 0
    bytesDecompressed: int = 0
    errorType: ErrorType | None = None
    errorMessage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Same rule as ``UrlRecord.to_dict``: omit ``None``, never send null.

        This one lands in the key-value store, which is not schema-validated
        today, so the defect here has not cost anything yet. It is the same
        shape as the one that emptied the dataset on every live run, and a
        record type that serialises differently from its sibling is how that
        shape comes back.
        """
        d = asdict(self)
        if self.errorType is not None:
            d["errorType"] = self.errorType.value
        return {key: value for key, value in d.items() if value is not None}


@dataclass(slots=True)
class RunSummary:
    """Run-level report. Written even when the run is truncated or fatal."""

    startedAt: str = ""
    finishedAt: str = ""
    ok: bool = False
    fatal: bool = False
    fatalReason: str | None = None

    seedsGiven: int = 0
    sitemapsAttempted: int = 0
    sitemapsSucceeded: int = 0
    urlsDiscovered: int = 0
    urlsEmitted: int = 0
    urlsDeduped: int = 0
    urlsChecked: int = 0

    # Whether the emitted rows are the whole answer. Set by a spent budget
    # ("requests"/"bytes"/"seconds") and by reaching maxUrls ("max_urls") —
    # both mean the caller did not get everything that was found. Reported
    # alongside the accounting so a truncated run can never be mistaken for a
    # complete one.
    truncated: bool = False
    truncateReason: str | None = None
    requestsUsed: int = 0
    requestBudget: int = 0
    decompressedBytesUsed: int = 0
    decompressedByteBudget: int = 0
    elapsedSec: float = 0.0

    errorBreakdown: dict[str, int] = field(default_factory=dict)
    # populated when >80% of a host's responses look like blocking
    possibleBlocks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FatalError(Exception):
    """Fast-fail category: invalid input or platform error.

    Must exit non-zero with a clear message and must NOT be disguised as
    data rows.
    """

    def __init__(self, message: str, error_type: ErrorType = ErrorType.INVALID_INPUT):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


class BudgetExhausted(Exception):
    """Budget category: request/byte/time budget ran out.

    Exit code 0, summary marks truncated=True, untouched URLs get
    SKIPPED_BUDGET. Never a silent truncation.
    """

    def __init__(self, kind: str):
        super().__init__(f"budget exhausted: {kind}")
        self.kind = kind
