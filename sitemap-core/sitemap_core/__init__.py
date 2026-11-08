"""sitemap-core: shared sitemap discovery, parsing and status probing.

Used by both the public ``sitemap-extractor`` Actor and the change-monitor
product. One implementation, one test corpus, one output schema — so the
monitoring product consumes a parser that is already exercised in production
rather than a rewritten copy.

Parsing, discovery, recursion, cycle protection, tolerant XML and gzip are
provided by ``ultimate-sitemap-parser`` (GPL-3.0). This package adds the parts
that tool deliberately leaves to its caller: work budgets, per-host rate
limiting, SSRF refusal, content sniffing, the failure taxonomy, and status
probing.
"""

from .budget import Budget
from .discover import WalkResult, looks_like_sitemap_url, walk
from .models import (
    BudgetExhausted,
    DEFAULT_MAX_URLS,
    ErrorType,
    FatalError,
    RunSummary,
    SitemapRecord,
    UrlRecord,
)
from .normalize import host_of, is_same_site, normalize_url
from .parse import best_effort_iso, collect_rows, extract_raw_lastmods, salvage_locs
from .pipeline import (
    EXIT_FATAL,
    EXIT_OK,
    EXIT_UNEXPECTED,
    ExtractionResult,
    extract,
    run,
)
from .ratelimit import HostGate, SyncHostGate
from .sniff import ContentKind, SniffResult, gunzip_guarded, sniff
from .ssrf import SsrfBlocked, UnsupportedScheme, is_blocked_ip, resolve_and_check
from .statuscheck import StatusChecker, probe_rows
from .transport import USER_AGENT, CompliantWebClient, as_utf8, decode_body

__version__ = "1.0.0"

__all__ = [
    "Budget",
    "BudgetExhausted",
    "CompliantWebClient",
    "ContentKind",
    "DEFAULT_MAX_URLS",
    "ErrorType",
    "EXIT_FATAL",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "ExtractionResult",
    "FatalError",
    "HostGate",
    "RunSummary",
    "SitemapRecord",
    "SniffResult",
    "SsrfBlocked",
    "StatusChecker",
    "SyncHostGate",
    "UnsupportedScheme",
    "USER_AGENT",
    "UrlRecord",
    "WalkResult",
    "as_utf8",
    "best_effort_iso",
    "collect_rows",
    "decode_body",
    "extract",
    "extract_raw_lastmods",
    "gunzip_guarded",
    "host_of",
    "is_blocked_ip",
    "is_same_site",
    "looks_like_sitemap_url",
    "normalize_url",
    "probe_rows",
    "resolve_and_check",
    "run",
    "salvage_locs",
    "sniff",
    "walk",
    "__version__",
]
