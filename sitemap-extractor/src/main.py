"""Apify Actor entry point — the boundary layer and nothing more.

Every decision about discovery, parsing, budgets, rate limiting, SSRF refusal
and status probing lives in ``sitemap_core``. This file translates Actor input
into arguments, turns finished batches into dataset records, and maps the
documented exit codes onto Apify's success and failure. Keeping it thin is what
makes "same parser, same tests, same output schema" true for the change-monitor
product that shares the package.

It calls ``sitemap_core.run``, not ``sitemap_core.extract``, on purpose. ``run``
is the only entry point that turns a fast-fail into a documented exit code; a
boundary layer that calls ``extract`` has to remember to do that mapping itself,
and the one that forgets produces an Actor that reports success on invalid
input.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from apify import Actor

from sitemap_core import (
    DEFAULT_MAX_URLS,
    EXIT_FATAL,
    EXIT_OK,
    EXIT_UNEXPECTED,
    ExtractionResult,
    UrlRecord,
    run,
)

log = logging.getLogger(__name__)

# The event charged once per dataset item. Rows are pushed without naming it, so
# the SDK charges it as the synthetic `apify-default-dataset-item` itself — this
# constant exists so the charging code, the input schema and the Pricing section
# of the README cannot drift apart, and so the charge can be read back and
# reported.
CHARGE_EVENT = "apify-default-dataset-item"

# Where "want to monitor these URLs?" points. The monitoring product does not
# exist yet (it is a day-14 deliverable), so this has to be countable rather
# than merely clickable — the plan asks for a cross-entry *with instrumentation*
# and a conversion figure from day one, and a bare /issues/new link provides
# neither. A monitor request is indistinguishable from a bug report, and there
# is no denominator to divide by.
#
# Counting recipe, so day 14 arrives with numbers instead of impressions:
#
#   numerator    issues in Chasen1992/sitemap-extractor whose title is
#                "Monitor request", or which carry the monitor-request label
#   denominator  Apify API -> GET /v2/acts/<id> -> stats.totalUsers30Days
#                (and stats.totalRuns for a looser version)
#   target       >= 10% of artifact users start a monitor within 30 days
#
# The marker is in the TITLE and the label is a second chance. GitHub applies
# `title` no matter who opens the issue; `labels` is silently dropped for
# openers without push access. Counting either catches the request, so losing
# one still leaves the other. The label exists on the repository for this.
#
# The body is a QUESTION, deliberately not an answer. A pre-filled sentence
# would inflate the count with clicks and hand back my wording rather than the
# user's, and the monitoring product's listing is supposed to be written in real
# users' own words. Friction here is the filter.
#
# README.md carries a second copy of this link and drifts from it on its own —
# the same defect class as `maxUrls`, which shipped with three values in three
# files. Both are compared by tests/test_actor_contract.py.
MONITOR_REQUEST_TITLE = "Monitor request"
MONITOR_REQUEST_LABEL = "monitor-request"
SUPPORT_URL = (
    "https://github.com/Chasen1992/sitemap-extractor/issues/new"
    "?title=Monitor+request"
    "&labels=monitor-request"
    "&body=What+would+you+want+the+monitor+to+tell+you%2C+and+how+often%3F"
)

# Key-value store records written alongside the dataset.
SUMMARY_KEY = "SUMMARY"
SITEMAPS_KEY = "SITEMAPS"
NEXT_STEP_KEY = "NEXT_STEP"


class _DatasetSink:
    """Pushes rows to the default dataset, in order, without blocking the run.

    ``sitemap_core`` hands finished batches to a synchronous callback from
    inside its own coroutine, so that callback cannot await the dataset write.
    Queuing the batch and draining it from one background task gives three
    things at once: row order is preserved, the incremental flush that makes a
    half-finished run survivable is preserved, and the event loop is never
    blocked while a write is in flight.
    """

    def __init__(self, *, charge_cap_usd: Decimal | None) -> None:
        self._charge_cap_usd = charge_cap_usd
        self._queue: asyncio.Queue[list[dict[str, Any]] | None] = asyncio.Queue()
        self._drainer: asyncio.Task[None] | None = None
        self.pushed = 0
        self.dropped_for_charge_limit = 0
        self.charge_limit_reached = False
        # Batches the platform refused. Counted because the exception that
        # reports them is logged and swallowed — without a counter, a run that
        # lost every batch is indistinguishable in the summary from one that
        # produced none, which is how the data-loss defect stayed invisible for
        # three live runs.
        self.push_failures = 0
        self.rows_push_failed = 0

    def start(self) -> None:
        self._drainer = asyncio.get_event_loop().create_task(self._drain())

    def submit(self, batch: list[UrlRecord]) -> None:
        """Sync callback handed to ``sitemap_core`` as ``on_rows``."""
        if batch:
            self._queue.put_nowait([row.to_dict() for row in batch])

    async def finish(self) -> None:
        await self._queue.put(None)
        if self._drainer is not None:
            await self._drainer

    # ---- internals ----------------------------------------------

    async def _drain(self) -> None:
        while True:
            batch = await self._queue.get()
            try:
                if batch is None:
                    return
                await self._push(batch)
            except Exception:
                # A failed dataset write must not take the run down and must
                # not silently lose the rest of the batches behind it. But it
                # must not vanish either: the rows in this batch are gone, and
                # counting them is what turns an unreadable run into a
                # diagnosable one.
                self.push_failures += 1
                if batch:
                    self.rows_push_failed += len(batch)
                log.exception("pushing a batch to the dataset failed")
            finally:
                self._queue.task_done()

    async def _push(self, batch: list[dict[str, Any]]) -> None:
        take = self._admissible(len(batch))
        if take == 0:
            self.charge_limit_reached = True
            self.dropped_for_charge_limit += len(batch)
            return

        # Push only what will actually be charged. The SDK's own push path for
        # the synthetic event pushes every item and lets the client charge what
        # it can, so without this cut a run could ship rows it was never paid
        # for — a silent leak in both directions.
        if take < len(batch):
            self.dropped_for_charge_limit += len(batch) - take
            self.charge_limit_reached = True

        await Actor.push_data(batch[:take])
        self.pushed += take

        if take < len(batch):
            # The limit has been reached; further batches are refused by
            # _admissible, so stop draining rather than queueing refusals.
            self.charge_limit_reached = True

    def _admissible(self, want: int) -> int:
        """How many of ``want`` rows may still be pushed and charged."""
        if want <= 0:
            return 0

        allowed = want

        # The platform's own ceiling, which is `minimalMaxTotalChargeUsd` on our
        # pricing record or `maxTotalChargeUsd` on the caller's run.
        try:
            charging = Actor.get_charging_manager()
            allowed = min(
                allowed,
                charging.compute_push_data_limit(
                    items_count=want,
                    event_name=CHARGE_EVENT,
                    is_default_dataset=True,
                ),
            )
        except Exception:
            # No charging manager (local run) or non-pay-per-event pricing:
            # there is nothing to cap against, and refusing to push would be
            # the wrong answer.
            charging = None

        # The ceiling the user typed into the input, if they typed one.
        if self._charge_cap_usd is not None and charging is not None:
            try:
                spent = charging.calculate_total_charged_amount()
                price = (charging.get_pricing_info().per_event_prices or {}).get(CHARGE_EVENT)
                if price:
                    affordable = int((self._charge_cap_usd - spent) / price)
                    allowed = min(allowed, max(0, affordable))
                elif spent >= self._charge_cap_usd:
                    allowed = 0
            except Exception:
                pass

        return allowed


def _as_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _start_urls(raw: Any) -> list[str]:
    """Accept both ``["https://…"]`` and Apify's ``[{"url": "https://…"}]``."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("startUrls must be a list of URLs or of {url} objects")

    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            url = item.get("url") or item.get("userData", {}).get("url")
            if isinstance(url, str) and url.strip():
                out.append(url.strip())
        # Anything else is dropped here rather than turned into a fatal error:
        # one malformed entry out of ten should not cost the other nine.
    return out


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    number = _as_number(value)
    return default if number is None else int(number)


def _as_decimal(value: Any) -> Decimal | None:
    number = _as_number(value)
    if number is None or number <= 0:
        return None
    try:
        return Decimal(str(number))
    except (InvalidOperation, ValueError):
        return None


async def _proxy_url_for(actor_input: dict[str, Any]) -> str | None:
    """Resolve exactly one proxy URL for the whole run, if one was asked for.

    The check on ``proxyConfiguration`` is not an optimisation. Asking the SDK
    for a proxy configuration when the user requested none still builds an
    Apify Proxy configuration and then fails the run for a missing proxy
    password — so an ordinary no-proxy run would die on a setting nobody chose.

    And deliberately not a rotation. A tool that swaps proxies when a target
    answers 403 is not checking status codes, it is evading them — which is the
    compliance line this product is built on. A single fixed proxy is offered
    for a stated geo or access reason and nothing else.
    """
    requested = actor_input.get("proxyConfiguration")
    if not requested:
        return None

    try:
        configuration = await Actor.create_proxy_configuration(
            actor_proxy_input=requested
        )
        if configuration is None:
            return None
        return await configuration.new_url()
    except Exception:
        # A proxy the user asked for but cannot have is worth a log line and a
        # run without it, not a dead run. The summary says the run was direct.
        log.exception("resolving the proxy URL failed; continuing without one")
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeliveryVerdict(NamedTuple):
    """What the dataset accepting some, all or none of the rows means."""

    failed: bool
    fatal: bool
    reason: str | None
    truncate_reason: str | None


def _delivery_verdict(
    *,
    produced: int,
    delivered: int,
    lost: int,
    lost_batches: int,
    cut_on_purpose: bool,
) -> DeliveryVerdict:
    """Decide what the run owes the user for what the dataset accepted.

    Split out of ``main`` because the distinction it encodes is the one that
    was wrong for three live runs, and it is easy to be wrong again: a run cut
    at ``maxTotalChargeUsd`` delivered nothing **on purpose** and must stay
    exit 0, while a run whose batches the platform refused delivered nothing
    **by accident** and must not. The observed state is identical — zero rows
    in the dataset, rows counted in the summary — and the verdicts are
    opposite. That symmetry is why this is a function and not a condition.

    ``cut_on_purpose`` covers the charge limit only. The request and byte
    budgets never cause this shape: they stop the pipeline before it emits the
    rows, so ``produced`` is already the smaller number.
    """
    if lost_batches > 0:
        # Rows the platform refused are lost whatever else happened, so this
        # comes first: a run that was also cut at the charge limit still lost
        # these, and "we stopped on purpose" is not an account of them.
        if delivered == 0 and not cut_on_purpose:
            return DeliveryVerdict(
                failed=True,
                fatal=True,
                reason=(
                    f"{produced} URLs were found and not one reached the "
                    "dataset: every batch was refused by the platform. "
                    "Nothing was delivered. This is a failure, not a "
                    "truncation. The log above holds the rejection."
                ),
                truncate_reason=None,
            )
        return DeliveryVerdict(
            failed=True,
            fatal=False,
            reason=(
                f"{lost} rows were refused by the dataset and are gone "
                f"({lost_batches} batch(es) failed to write); {delivered} were "
                "delivered. Those rows were lost, not cut — nothing chose to "
                "stop there. See the log for the rejection."
            ),
            truncate_reason="push_failed",
        )

    if produced > 0 and delivered == 0 and not cut_on_purpose:
        # Nothing was refused and nothing arrived. Unexplained, and still a
        # run that promised rows and delivered none.
        return DeliveryVerdict(
            failed=True,
            fatal=True,
            reason=(
                f"{produced} URLs were found and the dataset has none of "
                "them. Nothing reported a refusal, so the cause is not known "
                "— but a run that promised rows and delivered none has not "
                "succeeded. The log above is where to look."
            ),
            truncate_reason=None,
        )

    return DeliveryVerdict(False, False, None, None)


def _failed_result(reason: str, exit_code: int) -> ExtractionResult:
    """A result carrying only a failure, for the paths that never reach `run`.

    The summary exists so a run always has a record of what happened. An Actor
    that dies without writing one is exactly the silent failure the design
    forbids, and the fastest way to get there is to build the result before the
    pipeline has been entered.
    """
    result = ExtractionResult()
    result.exit_code = exit_code
    result.summary.ok = False
    result.summary.fatal = True
    result.summary.fatalReason = reason
    result.summary.startedAt = _utc_now()
    result.summary.finishedAt = _utc_now()
    return result


async def _write_records(result: ExtractionResult, sink: _DatasetSink) -> None:
    summary = result.summary.to_dict()
    summary["rowsPushed"] = sink.pushed
    summary["rowsDroppedForChargeLimit"] = sink.dropped_for_charge_limit
    summary["rowsPushFailed"] = sink.rows_push_failed
    summary["pushFailures"] = sink.push_failures
    await Actor.set_value(SUMMARY_KEY, summary)
    await Actor.set_value(SITEMAPS_KEY, result.sitemaps)
    await Actor.set_value(
        NEXT_STEP_KEY,
        {
            "title": "Want these URLs watched for changes?",
            "body": (
                "A monitor that re-checks this URL list and reports what moved "
                "is being built. If that is something you would use, open the "
                "link in this record and say so — the requests decide what gets "
                "built first."
            ),
            "url": SUPPORT_URL,
            "urlsOffered": summary.get("urlsEmitted", 0),
        },
    )


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        if not isinstance(actor_input, dict):
            actor_input = {}

        sink = _DatasetSink(charge_cap_usd=_as_decimal(actor_input.get("maxTotalChargeUsd")))
        sink.start()

        try:
            start_urls = _start_urls(actor_input.get("startUrls"))
        except ValueError as exc:
            start_urls = []
            input_error = str(exc)
        else:
            input_error = None

        result: ExtractionResult
        try:
            if input_error is not None:
                # Fast-fail: invalid input is a documented non-zero exit and a
                # clear message, never a data row.
                result = _failed_result(f"invalid startUrls: {input_error}", EXIT_FATAL)
            else:
                # `run`, not `extract`: it is the only entry point that maps a
                # fast-fail onto a documented exit code.
                result = await run(
                    start_urls=start_urls,
                    max_urls=_as_int(actor_input.get("maxUrls"), DEFAULT_MAX_URLS),
                    check_status_codes=_as_bool(actor_input.get("checkStatusCodes"), True),
                    follow_sitemap_index=_as_bool(actor_input.get("followSitemapIndex"), True),
                    max_sitemap_depth=_as_int(actor_input.get("maxSitemapDepth"), 3),
                    include_subdomains=_as_bool(actor_input.get("includeSubdomains"), False),
                    max_requests_total=_as_int(actor_input.get("maxRequestsTotal"), 20_000),
                    proxy_url=await _proxy_url_for(actor_input),
                    on_rows=sink.submit,
                )
        except Exception as exc:
            # Fallback category. This layer can fail before the pipeline is ever
            # entered — on input shaping, on the proxy, on the platform — and a
            # failure out there still owes the user a summary and a documented
            # exit code rather than a bare traceback and an exit of zero.
            log.exception("unexpected failure in the Actor boundary layer")
            result = _failed_result(f"unexpected: {exc}", EXIT_UNEXPECTED)
        finally:
            # Whatever happened, every batch already handed over gets its write.
            await sink.finish()

        if sink.charge_limit_reached:
            result.summary.notes.append(
                f"stopped at the charge limit: {sink.pushed} rows pushed, "
                f"{sink.dropped_for_charge_limit} not emitted because emitting "
                "them would have exceeded the charge limit. The dataset is "
                "incomplete by design — it was cut, not lost."
            )

        # --- delivery accounting ------------------------------------------
        # What the dataset accepted, judged against what the run produced.
        # See `_delivery_verdict` for why this cannot be one condition.
        verdict = _delivery_verdict(
            produced=result.summary.urlsEmitted,
            delivered=sink.pushed,
            lost=sink.rows_push_failed,
            lost_batches=sink.push_failures,
            cut_on_purpose=sink.charge_limit_reached,
        )
        if verdict.failed:
            result.summary.ok = False
            result.summary.fatal = verdict.fatal
            result.summary.fatalReason = verdict.reason
            result.exit_code = EXIT_FATAL
            if verdict.truncate_reason:
                result.summary.truncated = True
                if not result.summary.truncateReason:
                    result.summary.truncateReason = verdict.truncate_reason

        try:
            await _write_records(result, sink)
        except Exception:
            # Losing the summary is the one failure that makes every other
            # failure undiagnosable, so it gets its own guard. The exit code is
            # still set below.
            log.exception("writing the run records failed")

        Actor.exit_code = result.exit_code
        summary = result.summary
        if result.exit_code != EXIT_OK:
            Actor.status_message = summary.fatalReason or "run failed"
        elif summary.errorBreakdown.get("no_sitemap") and not summary.sitemapsAttempted:
            # "0 URLs from 0/0 sitemaps" is accurate and useless. The question
            # a user is actually asking is whether their site has a sitemap at
            # all, and the answer is one sentence.
            Actor.status_message = "no sitemap found at this site"
        else:
            # The number a user reads here is the number of rows they will find
            # in the dataset — what was delivered, not what the parser produced.
            # Those agree on a healthy run and were wildly different on every
            # live run so far (200 claimed, 0 delivered) because nothing
            # checked that the writes had landed. When they disagree, show both:
            # one number alone would hide the gap that matters.
            delivered = sink.pushed
            where = (
                f"from {summary.sitemapsSucceeded}/{summary.sitemapsAttempted} "
                "sitemaps"
            )
            if delivered == summary.urlsEmitted:
                status = f"{delivered} URLs {where}"
            else:
                status = f"{delivered} of {summary.urlsEmitted} URLs delivered {where}"
            if summary.truncated:
                status += " (truncated)"
            if summary.errorBreakdown:
                # "0 URLs from 0/1 sitemaps" is accurate but reads as an empty
                # site. Naming the failure is the difference between a run that
                # looks empty and a run that says why.
                total = sum(summary.errorBreakdown.values())
                top = max(summary.errorBreakdown.items(), key=lambda kv: kv[1])
                status += f"; {total} failures, most common: {top[0]}"
            Actor.status_message = status


if __name__ == "__main__":
    asyncio.run(main())
