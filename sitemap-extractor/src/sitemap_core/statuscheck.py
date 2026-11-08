"""URL status probing.

Transport rules, fixed rather than improvised:

* **HEAD first, fall back to GET** when the server refuses HEAD (``405`` /
  ``501``) or when HEAD produced no status at all — a connection reset or a
  protocol error carries no information about whether the URL is alive. A great
  many servers answer ``405`` to HEAD and ``200`` to GET, so HEAD-only marks
  healthy URLs dead, which is worse than not checking at all because it looks
  authoritative. **``403`` is deliberately not a fallback trigger**: it is
  usually a real answer, and re-asking with GET is how a tool starts to look
  like it is probing for a hole. **A timeout is not a trigger either** — GET to
  the same host would time out too, so the fallback would only double the cost
  of every dead URL.
* **No body is stored.** The GET fallback streams at most a sniff head and
  closes; we are checking status codes, not collecting content. This is a
  compliance line, not a performance choice.
* **Every wire attempt is charged to the budget**, including retries and
  including every redirect hop. Charging once per URL under-counts a run by the
  retry and redirect factor, which is exactly the accounting the plan forbids.
* **Retries once only**, and only for connection failures, timeouts and
  transient 5xx. 4xx never retries.
* **Respects ``429`` and ``Retry-After``** globally.
* **401 / 403 / 451 / 503 are ``blocked``, not ``http_error``** — the plan
  requires walking away from a refusing target rather than emitting thousands
  of misleading "dead link" rows.

One long-lived ``httpx.AsyncClient`` for the whole run: rebuilding the pool per
request exhausts ephemeral ports and silently multiplies the runtime on a
50,000-URL job.

Known limit, stated rather than hidden: challenge-page detection needs body
bytes, and HEAD has none. A bot-challenge served as a plain 200 to HEAD is
therefore reported as 200 unless the GET fallback ran. We do not spend a second
request on every HTML URL just to find out, because that doubles the cost of the
common case to guard the uncommon one.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urljoin

import httpx

from .budget import Budget
from .models import ErrorType, UrlRecord
from .ratelimit import HostGate
from .sniff import looks_like_challenge
from .ssrf import SsrfBlocked, UnsupportedScheme, _DnsFailure, resolve_and_check
from .transport import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    USER_AGENT,
    _parse_retry_after,
)

log = logging.getLogger(__name__)

# HEAD is refused by these and GET still works. 403 is deliberately absent, and
# so is every other 4xx: the docstring above is a promise, not a comment.
HEAD_FALLBACK_STATUSES = frozenset({405, 501})

# Statuses where one more attempt is a reasonable bet. 4xx is never in here.
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})

# 401 / 403 / 451 / 503 read as "the target is refusing automated access".
BLOCKING_STATUSES = frozenset({401, 403, 451, 503})

# Redirects are followed by hand so each hop is re-checked against the SSRF
# guard and charged to its own host's pacing. Letting httpx follow them
# internally would make an open redirect a tunnel to a refused address.
MAX_REDIRECT_HOPS = 5

# How many probes run concurrently, and how the split of the URL list is
# reported so a truncated run stays legible.
PROBE_CONCURRENCY = 24

# Bytes streamed on the GET fallback purely to recognise a challenge page. The
# stream is closed immediately after; nothing is stored.
SNIFF_BYTES = 2048


class StatusChecker:
    """Async, rate-limited status prober over a shared client."""

    def __init__(
        self,
        *,
        budget: Budget,
        gate: HostGate | None = None,
        client: httpx.AsyncClient | None = None,
        permit_networks: tuple[str, ...] = (),
        proxy_url: str | None = None,
    ) -> None:
        self.budget = budget
        self.gate = gate or HostGate()
        self.permit_networks = permit_networks
        self._owns_client = client is None
        # One fixed proxy for the whole run, if the caller supplied one. Never
        # rotated and never chosen by this code — see the transport's note.
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                # Status probing only. No HTML body is wanted or kept.
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            # Redirects are followed by hand in _send(); see MAX_REDIRECT_HOPS.
            follow_redirects=False,
            proxy=proxy_url,
        )

    async def close(self) -> None:
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception:
                pass

    async def __aenter__(self) -> "StatusChecker":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- public entry ---------------------------------------------

    async def check_all(self, rows: list[UrlRecord]) -> dict[str, int]:
        """Probe every unchecked row in place. Returns counters."""
        counters = {"checked": 0, "failed": 0, "skipped": 0}
        pending = [r for r in rows if not r.checked and r.errorType is None]
        if not pending:
            return counters

        sem = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def worker(row: UrlRecord) -> None:
            async with sem:
                try:
                    self.budget.check()
                except Exception:
                    row.checked = False
                    row.errorType = ErrorType.SKIPPED_BUDGET
                    row.errorMessage = "request budget exhausted before probing"
                    counters["skipped"] += 1
                    return
                try:
                    await self._probe_one(row)
                except Exception as exc:
                    # One row must never take the run down with it.
                    log.warning("probe raised for %s: %s", row.normalizedUrl, exc)
                    row.checked = True
                    row.errorType = ErrorType.UNEXPECTED
                    row.errorMessage = f"probe raised: {exc}"
                if row.errorType is not None and row.errorType is not ErrorType.CROSS_DOMAIN:
                    counters["failed"] += 1
                else:
                    counters["checked"] += 1

        # return_exceptions=True: a task that dies on an unforeseen error must
        # not cancel the other 49,999 probes and leave the run half-emitted.
        await asyncio.gather(*(worker(r) for r in pending), return_exceptions=True)
        return counters

    # ---- one probe ------------------------------------------------

    async def _probe_one(self, row: UrlRecord) -> None:
        url = row.normalizedUrl

        try:
            await asyncio.to_thread(resolve_and_check, url, permit=self.permit_networks)
        except SsrfBlocked as exc:
            return self._mark(row, ErrorType.SSRF_BLOCKED, exc.reason)
        except UnsupportedScheme as exc:
            return self._mark(row, ErrorType.UNSUPPORTED_SCHEME, str(exc))
        except _DnsFailure as exc:
            return self._mark(row, ErrorType.DNS, exc.reason)
        except Exception as exc:
            return self._mark(row, ErrorType.UNEXPECTED, f"preflight failed: {exc}")

        # Each attempt and each redirect hop charges itself inside _send, so
        # there is deliberately no spend_request() here.
        outcome = await self._request_with_policy(url, method="HEAD")

        # HEAD is unreliable. Fall back when the server explicitly refuses it,
        # and when HEAD produced no status at all — a connection reset or a
        # protocol error says nothing about whether the URL is alive.
        #
        # A timeout is deliberately not in that set. It *is* an answer: the host
        # is slow or will not talk to us, and a GET to the same host times out
        # too, so the fallback would double the request cost of every dead URL
        # and buy nothing. Host-level failures — DNS and TLS — are excluded for
        # the same reason: GET would fail identically.
        if outcome["status"] in HEAD_FALLBACK_STATUSES or (
            outcome["status"] == 0
            and outcome.get("error_type") in (ErrorType.HTTP_ERROR, ErrorType.UNEXPECTED)
        ):
            outcome = await self._request_with_policy(url, method="GET")

        status = outcome["status"]
        row.checked = True
        row.statusCode = status if status else None
        row.contentType = outcome.get("content_type")

        if outcome.get("error_type") is not None:
            return self._mark(row, outcome["error_type"], outcome.get("message", ""))

        if status in BLOCKING_STATUSES:
            return self._mark(
                row,
                ErrorType.BLOCKED,
                f"{status} — target appears to be refusing automated access; not bypassing",
            )

        if outcome.get("challenge"):
            return self._mark(
                row,
                ErrorType.HTML_CHALLENGE,
                "bot-challenge or block page served with a 2xx status",
            )

        if status >= 400:
            return self._mark(
                row,
                ErrorType.HTTP_ERROR,
                f"{status} {outcome.get('reason', '')}".strip(),
            )

    async def _request_with_policy(self, url: str, *, method: str) -> dict:
        """One attempt with a single retry for transient failures.

        Budget accounting happens per wire attempt inside ``_send`` (and per
        redirect hop), so a retried URL costs two tokens, not one.
        """
        last: dict = {"status": 0, "error_type": ErrorType.UNEXPECTED, "message": ""}

        for attempt in (1, 2):
            try:
                outcome = await self.gate.run(url, lambda: self._send(url, method=method))
            except Exception as exc:
                outcome = {
                    "status": 0,
                    "error_type": ErrorType.UNEXPECTED,
                    "message": f"request raised: {exc}",
                }

            if outcome.get("error_type") is None:
                return outcome

            last = outcome
            error_type = outcome["error_type"]
            status = outcome.get("status", 0)

            # 4xx is never retried. A 403 is a real answer.
            if 400 <= status < 500 and status not in (408, 429):
                return outcome

            retryable = error_type in {ErrorType.TIMEOUT, ErrorType.DNS, ErrorType.TLS} or (
                status in TRANSIENT_STATUSES
            )
            if not retryable or attempt == 2:
                return outcome

            await asyncio.sleep(min(1.5 * attempt, 5.0))

        return last

    async def _send(self, url: str, *, method: str) -> dict:
        """One request, following redirects by hand. Charges every wire hop."""
        current = url
        for hop in range(MAX_REDIRECT_HOPS + 1):
            # Re-check every hop: an open redirect on a public host must not be
            # a tunnel to 169.254.169.254 that the first check already refused.
            try:
                await asyncio.to_thread(resolve_and_check, current, permit=self.permit_networks)
            except SsrfBlocked as exc:
                return {"status": 0, "error_type": ErrorType.SSRF_BLOCKED, "message": exc.reason}
            except UnsupportedScheme as exc:
                return {
                    "status": 0,
                    "error_type": ErrorType.UNSUPPORTED_SCHEME,
                    "message": str(exc),
                }
            except _DnsFailure as exc:
                return {"status": 0, "error_type": ErrorType.DNS, "message": exc.reason}
            except Exception as exc:
                return {
                    "status": 0,
                    "error_type": ErrorType.UNEXPECTED,
                    "message": f"preflight failed: {exc}",
                }

            try:
                self.budget.spend_request()
            except Exception as exc:
                return {"status": 0, "error_type": ErrorType.SKIPPED_BUDGET, "message": str(exc)}

            try:
                request = self._client.build_request(method, current)
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                return {"status": 0, "error_type": ErrorType.TIMEOUT, "message": f"timeout: {exc}"}
            except httpx.ConnectError as exc:
                # Nothing was transmitted, so the token is given back. This is
                # the only path where refunding is honest.
                self.budget.refund_request()
                text = str(exc).lower()
                kind = ErrorType.TLS if ("ssl" in text or "tls" in text) else ErrorType.DNS
                return {"status": 0, "error_type": kind, "message": str(exc)}
            except httpx.HTTPError as exc:
                return {"status": 0, "error_type": ErrorType.HTTP_ERROR, "message": str(exc)}
            except Exception as exc:
                return {"status": 0, "error_type": ErrorType.UNEXPECTED, "message": str(exc)}

            status = response.status_code
            location = _redirect_location(response)
            if location is not None:
                try:
                    await response.aclose()
                except Exception:
                    pass
                if hop == MAX_REDIRECT_HOPS:
                    return {
                        "status": 0,
                        "error_type": ErrorType.REDIRECT_LOOP,
                        "message": f"more than {MAX_REDIRECT_HOPS} redirects",
                    }
                current = urljoin(current, location)
                continue

            content_type = response.headers.get("Content-Type")
            challenge = False

            try:
                if status == 429:
                    await self.gate.defer(
                        current, _parse_retry_after(response.headers.get("Retry-After", ""))
                    )
                elif method == "GET" and 200 <= status < 400 and _is_html(content_type):
                    # Sniff a small head and close — "嗅探后立刻关流". Nothing
                    # is stored, and only HTML 2xx can be a challenge page.
                    challenge = await _peek_challenge(response)
            finally:
                # Status probing only: drain nothing, store nothing, close the body.
                try:
                    await response.aclose()
                except Exception:
                    pass

            if status == 429:
                return {
                    "status": status,
                    "reason": response.reason_phrase,
                    "content_type": content_type,
                    "error_type": ErrorType.RATE_LIMITED,
                    "message": "429 Too Many Requests",
                }

            return {
                "status": status,
                "reason": response.reason_phrase,
                "content_type": content_type,
                "challenge": challenge,
                "error_type": None,
                "message": "",
            }

        return {
            "status": 0,
            "error_type": ErrorType.REDIRECT_LOOP,
            "message": f"more than {MAX_REDIRECT_HOPS} redirects",
        }

    @staticmethod
    def _mark(row: UrlRecord, error_type: ErrorType, message: str) -> None:
        row.checked = True
        row.errorType = error_type
        row.errorMessage = message or None


def _is_html(content_type: str | None) -> bool:
    return "html" in (content_type or "").lower()


async def _peek_challenge(response: httpx.Response) -> bool:
    """Read a bounded head of the body and decide if it is a block page.

    Closes nothing here — the caller closes the stream in its ``finally``. A
    short read is not an error: a challenge page's markers sit at the top.
    """
    head = b""
    try:
        async for chunk in response.aiter_bytes():
            head += chunk
            if len(head) >= SNIFF_BYTES:
                break
    except Exception:
        return False
    return looks_like_challenge(head[:SNIFF_BYTES])


def _redirect_location(response: httpx.Response) -> str | None:
    """Return the redirect target, or None if this is a final response.

    301/302/303/307/308 only, and only when a Location header is present.
    """
    if response.status_code not in (301, 302, 303, 307, 308):
        return None
    return response.headers.get("Location")


def probe_rows(
    rows: list[UrlRecord],
    *,
    budget: Budget,
    gate: HostGate | None = None,
    client: httpx.AsyncClient | None = None,
    permit_networks: tuple[str, ...] = (),
) -> dict[str, int]:
    """Convenience wrapper used by tests and the Actor."""

    async def _run() -> dict[str, int]:
        async with StatusChecker(
            budget=budget, gate=gate, client=client, permit_networks=permit_networks
        ) as checker:
            return await checker.check_all(rows)

    return asyncio.run(_run())
