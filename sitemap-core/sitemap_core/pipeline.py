"""End-to-end extraction pipeline.

Order of operations is itself a design decision:

1. validate input        -> fast-fail with a clear error, never a data row
2. discover + parse      -> sitemap files, through the guarded transport
3. dedup                 -> before status checking, saving 10-30% of requests
4. status probe          -> concurrency + per-host pacing, HEAD with GET fallback
5. emit + summary        -> flush in batches as rows become final; summary
                            written even when fatal

Failure taxonomy, so "never fails the whole run" is checkable rather than a
slogan:

* **fast-fail**   invalid input, platform error -> non-zero exit, clear message
* **single-point** one sitemap or URL fails -> an ``errorType`` row, run continues
* **budget**      request/byte/time budget spent -> exit 0, ``truncated: true``,
                  untouched URLs marked ``skipped_budget``
* **output cap**  ``maxUrls`` reached -> exit 0, ``truncated: true``, the rest
                  of the discovery named in ``notes``
* **fallback**    anything unforeseen -> top-level handler writes the summary,
                  flushes what it has, exits with a documented code

``truncated`` means one thing to the person reading the summary: *the rows you
have are not the whole answer*. Both the budget and ``maxUrls`` can cause that,
so both set it, and ``truncateReason`` says which — ``requests``/``bytes``/
``seconds`` for a budget, ``max_urls`` for the output cap. A budget reason wins
when both apply, because a spent budget leaves rows in the output that were
never checked at all.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .budget import Budget
from .discover import WalkResult, walk
from .models import (
    BudgetExhausted,
    DEFAULT_MAX_URLS,
    ErrorType,
    FatalError,
    RunSummary,
    SitemapRecord,
    SYNTHETIC_KINDS,
    UrlRecord,
)
from .normalize import InvalidUrl, normalize_url
from .parse import collect_rows
from .ratelimit import HostGate, SyncHostGate
from .ssrf import SsrfBlocked, UnsupportedScheme
from .statuscheck import StatusChecker
from .transport import CompliantWebClient

log = logging.getLogger(__name__)

# Documented exit codes. The Actor surfaces these; tests assert on them.
EXIT_OK = 0
EXIT_FATAL = 2
EXIT_UNEXPECTED = 3

# Rows are pushed to the dataset in batches, not one call per row and not in a
# single call at the end. Per-row would be 50,000 network round trips; a single
# call loses everything if the run is killed at minute 29 of 30.
FLUSH_BATCH = 2_000


class ExtractionResult:
    def __init__(self) -> None:
        self.rows: list[UrlRecord] = []
        self.summary: RunSummary = RunSummary()
        self.sitemaps: list[dict[str, Any]] = []
        self.exit_code = EXIT_OK


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flush(
    on_rows: Callable[[list[UrlRecord]], None] | None, batch: list[UrlRecord]
) -> None:
    """Push one batch. A broken sink must not take the run down with it."""
    if on_rows is None or not batch:
        return
    try:
        on_rows(batch)
    except Exception:
        log.warning("on_rows callback raised", exc_info=True)


async def extract(
    *,
    start_urls: list[str],
    max_urls: int = DEFAULT_MAX_URLS,
    check_status_codes: bool = True,
    follow_sitemap_index: bool = True,
    max_sitemap_depth: int = 3,
    include_subdomains: bool = False,
    max_requests_total: int = 20_000,
    max_decompressed_bytes: int = 512 * 1024 * 1024,
    max_bytes_per_file: int = 64 * 1024 * 1024,
    max_seconds: float | None = 1800.0,
    requests_per_second: float = 4.0,
    proxy_url: str | None = None,
    on_rows: Callable[[list[UrlRecord]], None] | None = None,
) -> ExtractionResult:
    """Run the full extraction. Raises FatalError only for invalid input.

    ``on_rows`` is called once per batch of finalised rows (see
    ``FLUSH_BATCH``), never once per row and never once with the whole run at
    the end. Each batch has already been status-probed when it is handed over,
    so what lands in the dataset is complete — and a run killed part-way keeps
    every batch it finished.

    ``proxy_url`` is one fixed proxy for the whole run, supplied by the caller
    for a stated geo or access reason. It is never rotated and never picked by
    this code in response to a block.

    There is deliberately no way to relax the SSRF policy from here. The
    transport accepts a ``permit_networks`` list, and the tests use it, but
    this entry point will not forward one: it is the Actor-facing boundary, and
    a switch that turns off the check would be one careless parameter away from
    pointing a public tool at the host's own metadata service.
    """
    result = ExtractionResult()
    summary = result.summary
    summary.startedAt = _utc_now()
    started = time.monotonic()

    # --- 1. validate: fast-fail, never disguised as data rows ---------
    if not start_urls:
        raise FatalError("startUrls is empty — provide at least one domain or sitemap URL")
    if max_urls < 1:
        raise FatalError("maxUrls must be >= 1")
    if max_sitemap_depth < 0:
        raise FatalError("maxSitemapDepth must be >= 0")
    if max_requests_total < 1:
        raise FatalError("maxRequestsTotal must be >= 1")

    normalized_seeds: list[str] = []
    for raw in start_urls:
        try:
            normalized_seeds.append(normalize_url(raw))
        except SsrfBlocked as exc:
            raise FatalError(f"refused startUrl {raw!r}: {exc.reason}") from exc
        except (InvalidUrl, UnsupportedScheme, ValueError) as exc:
            raise FatalError(f"invalid startUrl {raw!r}: {exc}") from exc

    summary.seedsGiven = len(normalized_seeds)

    budget = Budget(
        max_requests=max_requests_total,
        max_decompressed_bytes=max_decompressed_bytes,
        max_bytes_per_file=max_bytes_per_file,
        max_seconds=max_seconds,
    )
    summary.requestBudget = max_requests_total
    summary.decompressedByteBudget = max_decompressed_bytes

    raw_bodies: dict[str, str] = {}
    all_rows: list[UrlRecord] = []
    sitemap_dicts: list[dict[str, Any]] = []
    error_breakdown: dict[str, int] = {}
    # robots.txt Crawl-delay seen during discovery, carried into the probing
    # phase so a site owner's request is honoured across the whole run and not
    # only while sitemap files are being fetched.
    host_intervals: dict[str, float] = {}
    # Set when maxUrls stopped the output. Applied at the end, and only if no
    # budget reason is already recorded: a spent budget is the worse of the two,
    # since it leaves rows in the output that were never checked at all.
    capped_at_max_urls = False

    try:
        # --- 2. discover + parse --------------------------------------
        sync_gate = SyncHostGate(requests_per_second=requests_per_second)
        with CompliantWebClient(budget=budget, gate=sync_gate, proxy_url=proxy_url) as client:
            for seed in normalized_seeds:
                try:
                    walk_result: WalkResult = walk(
                        seed,
                        budget=budget,
                        client=client,
                        max_depth=max_sitemap_depth,
                        follow_index=follow_sitemap_index,
                    )
                except BudgetExhausted as exc:
                    summary.truncated = True
                    summary.truncateReason = exc.kind
                    break
                except Exception as exc:
                    # Single-point failure. A seed that dies must not look like
                    # "this site has no sitemap": that is the silent failure the
                    # plan calls out. Record it as a sitemap-level error and move
                    # on to the next seed.
                    log.warning("walk raised for %s: %s", seed, exc)
                    record = SitemapRecord(
                        url=seed,
                        kind="invalid",
                        ok=False,
                        errorType=ErrorType.UNEXPECTED,
                        errorMessage=f"walk raised: {exc}",
                    )
                    sitemap_dicts.append(record.to_dict())
                    error_breakdown[record.errorType.value] = (
                        error_breakdown.get(record.errorType.value, 0) + 1
                    )
                    summary.sitemapsAttempted += 1
                    continue

                # Only real sitemap files count toward these. The discovery
                # scaffolding (the website node, the robots.txt node) is
                # reported alongside them for provenance, but was never fetched
                # as a sitemap and is not one.
                real = [s for s in walk_result.sitemaps if s.kind not in SYNTHETIC_KINDS]
                summary.sitemapsAttempted += len(real)
                summary.sitemapsSucceeded += sum(1 for s in real if s.ok)
                for record in walk_result.sitemaps:
                    sitemap_dicts.append(record.to_dict())
                    if record.errorType is not None:
                        error_breakdown[record.errorType.value] = (
                            error_breakdown.get(record.errorType.value, 0) + 1
                        )

                # Recover raw bodies for verbatim <lastmod>.
                for sitemap_url in client.raw_capture.urls():
                    body = client.raw_capture.get(sitemap_url)
                    if body and sitemap_url not in raw_bodies:
                        raw_bodies[sitemap_url] = body

                remaining_slots = max(0, max_urls - len(all_rows))
                if remaining_slots == 0:
                    capped_at_max_urls = True
                    summary.notes.append(
                        f"maxUrls={max_urls} already reached; later seeds were not collected"
                    )
                    continue
                rows, counters = collect_rows(
                    walk_result,
                    seed_url=seed,
                    raw_bodies=raw_bodies,
                    include_subdomains=include_subdomains,
                    max_urls=remaining_slots,
                )
                all_rows.extend(rows)
                summary.urlsDiscovered += counters["discovered"]
                summary.urlsDeduped += counters["deduped"]
                if counters["truncated_at_max_urls"]:
                    capped_at_max_urls = True
                    summary.notes.append(
                        f"stopped emitting at maxUrls={max_urls}; "
                        f"{counters['truncated_at_max_urls']} more were found"
                    )

            host_intervals = sync_gate.learned_intervals()
            if host_intervals:
                summary.notes.append(
                    "robots.txt Crawl-delay honoured for "
                    + ", ".join(sorted(host_intervals))
                )

        # --- 3. status probe + incremental flush -----------------------
        # Each batch is probed and then pushed, so a run killed part-way leaves
        # every finished batch in the dataset instead of losing all of it. Once
        # the budget is spent the later batches still go through the checker:
        # it marks them `skipped_budget`, which is the honest answer, rather
        # than leaving them looking simply unchecked.
        if check_status_codes and all_rows:
            probe_gate = HostGate(requests_per_second=requests_per_second)
            probe_gate.apply_intervals(host_intervals)
            async with StatusChecker(
                budget=budget, gate=probe_gate, proxy_url=proxy_url
            ) as checker:
                for start in range(0, len(all_rows), FLUSH_BATCH):
                    batch = all_rows[start : start + FLUSH_BATCH]
                    try:
                        await checker.check_all(batch)
                    except BudgetExhausted as exc:
                        summary.truncated = True
                        summary.truncateReason = summary.truncateReason or exc.kind
                    _flush(on_rows, batch)
        else:
            for start in range(0, len(all_rows), FLUSH_BATCH):
                _flush(on_rows, all_rows[start : start + FLUSH_BATCH])

    except BudgetExhausted as exc:
        summary.truncated = True
        summary.truncateReason = exc.kind
    except FatalError:
        raise
    except Exception as exc:
        # Fallback category: never lose the run's output.
        log.exception("unexpected failure during extraction")
        summary.fatal = True
        summary.fatalReason = f"unexpected: {exc}"
        result.exit_code = EXIT_UNEXPECTED

    # --- 4. emit + summarize -----------------------------------------
    for row in all_rows:
        if row.errorType is not None:
            key = row.errorType.value
            error_breakdown[key] = error_breakdown.get(key, 0) + 1

    if summary.truncated and all_rows:
        remaining = max_urls - len(all_rows)
        if remaining > 0:
            summary.notes.append(
                f"{remaining} output slots unused; the run stopped on budget, not on maxUrls"
            )

    result.rows = all_rows
    result.sitemaps = sitemap_dicts
    summary.urlsEmitted = len(all_rows)
    summary.urlsChecked = sum(1 for r in all_rows if r.checked and r.statusCode is not None)
    summary.errorBreakdown = error_breakdown
    summary.requestsUsed = budget.requests_used
    summary.decompressedBytesUsed = budget.bytes_used
    summary.elapsedSec = round(time.monotonic() - started, 3)
    summary.finishedAt = _utc_now()

    # Budget exhaustion is not an exception that reaches the handlers above:
    # transport and statuscheck catch it per work unit and mark the row
    # `skipped_budget` so one spent budget does not end the run. The run-level
    # flag therefore has to be derived from the budget itself, or `truncated`
    # would silently stay false on exactly the runs that were truncated.
    if not summary.truncated and budget.exhausted:
        summary.truncated = True
        summary.truncateReason = summary.truncateReason or budget.exhausted_kind
    if summary.truncated and summary.truncateReason is None:
        summary.truncateReason = budget.exhausted_kind or "requests"

    # maxUrls is the last of the caps to be recorded, and only when nothing
    # worse already did it. Before this line a run stopped by maxUrls reported
    # `truncated: false` beside a note saying how many URLs were left behind —
    # the flag actively contradicted the note, and `ok: true, truncated: false`
    # reads as "this is the complete list" to anyone who does not go digging in
    # `notes`. It never is.
    if capped_at_max_urls and not summary.truncated:
        summary.truncated = True
        summary.truncateReason = "max_urls"

    summary.ok = not summary.fatal
    summary.possibleBlocks = _detect_blocking(budget, all_rows)
    if summary.possibleBlocks:
        # Plan requirement: say this in the summary instead of emitting 40,000
        # misleading "dead link" rows. The wording matters — it offers the
        # operator a legitimate option (a proxy for geo/access reasons) while
        # stating plainly that this tool does not rotate proxies to get around
        # a block.
        summary.notes.append(
            "target appears to be refusing automated requests on "
            + ", ".join(sorted(summary.possibleBlocks))
            + "; if this is a geo/access requirement, consider proxyConfiguration — "
            "this tool does not rotate proxies or attempt to bypass a block"
        )

    return result


async def run(**kwargs: Any) -> ExtractionResult:
    """Run ``extract`` and turn a fast-fail into the documented exit code.

    Invalid input is a non-zero exit with a clear message, never a data row —
    but that is only true if somebody actually maps the exception to a code.
    ``EXIT_FATAL`` existed and nothing ever set it. This is the one place the
    Actor should call, so the mapping cannot be forgotten at the boundary.
    """
    try:
        return await extract(**kwargs)
    except FatalError as exc:
        result = ExtractionResult()
        result.exit_code = EXIT_FATAL
        result.summary.ok = False
        result.summary.fatal = True
        result.summary.fatalReason = f"invalid input: {exc}"
        result.summary.finishedAt = _utc_now()
        return result


def _detect_blocking(budget: Budget, rows: list[UrlRecord]) -> list[str]:
    """Flag hosts where most responses look like deliberate blocking.

    Plan requirement: do not emit 40,000 misleading "dead link" rows when the
    target is refusing automation. Say so in the summary instead.
    """
    flagged: list[str] = []
    by_host: dict[str, list[UrlRecord]] = {}
    for row in rows:
        host = row.normalizedUrl.split("/", 3)[2] if "//" in row.normalizedUrl else ""
        if not host:
            continue
        by_host.setdefault(host, []).append(row)

    for host, host_rows in by_host.items():
        if len(host_rows) < 5:
            continue
        blocked = sum(
            1
            for r in host_rows
            if r.errorType in (ErrorType.BLOCKED, ErrorType.HTML_CHALLENGE, ErrorType.RATE_LIMITED)
        )
        if blocked / len(host_rows) > 0.8:
            flagged.append(host)
    return flagged
