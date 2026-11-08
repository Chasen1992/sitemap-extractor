"""Compliant HTTP transport.

ultimate-sitemap-parser drives discovery, recursion, cycle protection and
tolerant parsing. It reaches the network only through ``AbstractWebClient``,
so everything policy-related belongs in this one class:

* request and decompressed-byte budgets
* per-host pacing
* SSRF refusal
* content sniffing (so an HTML challenge page is not parsed as a text sitemap)
* gzip bomb / bad gzip rejection with a real errorType
* an honest User-Agent
* raw-body capture, bounded, so ``<lastmod>`` can be kept verbatim

``get()`` never raises on connection errors — the interface requires errors to
be reported through the response object so one bad sitemap cannot kill a run.

Known limit, stated rather than hidden: the SSRF check resolves the host and
refuses blocked ranges, but the request itself is then made by httpx, which
resolves again. That leaves the classic DNS-rebinding window open. Pinning the
connection to the verified address would break TLS SNI handling for little
practical gain at this threat model, so we do not pretend to close it.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from http import HTTPStatus
from urllib.parse import urljoin

import httpx
from usp.web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientResponse,
    AbstractWebClientSuccessResponse,
    WebClientErrorResponse,
)

from .budget import Budget, _GzipBomb, _TooLarge
from .models import ErrorType
from .normalize import apex_host
from .ratelimit import SyncHostGate
from .sniff import ContentKind, GunzipError, gunzip_guarded, sniff
from .ssrf import SsrfBlocked, UnsupportedScheme, _DnsFailure, resolve_and_check

log = logging.getLogger(__name__)

# Honest identification. No browser impersonation: pretending to be a browser
# is precisely the compliance smell we refuse to copy from the competitor.
USER_AGENT = (
    "sitemap-extractor/1.0 "
    "(+https://github.com/apify-actors/sitemap-extractor; "
    "reads public sitemaps; respects robots.txt and Retry-After)"
)

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 15.0

# Redirects are followed by hand, one hop at a time, so every hop is re-checked
# against the SSRF guard and charged to its own host's pacing. Letting httpx
# follow them internally would make an open redirect on a public host a way to
# reach internal addresses we had just refused.
MAX_REDIRECT_HOPS = 5

# Statuses where one more attempt is a reasonable bet. 4xx is never in here:
# a 404 is an answer, and retrying it five times is how a 200-request budget
# turns into 1,000 requests.
TRANSIENT_STATUSES = frozenset({500, 502, 504})

# Bounded raw capture for <lastmod> fidelity. Older sitemap files fall out of
# the map first; a missing capture degrades to lastmodRaw=None and never fails.
MAX_CAPTURED_SITEMAPS = 512
MAX_CAPTURED_BYTES = 32 * 1024 * 1024

# Charset. A sitemap served as ISO-8859-1 with a matching XML prolog is rare
# but real, and decoding those bytes as UTF-8 mangles every non-ASCII path in
# the output — quietly wrong data, which is worse than a loud failure.
_XML_PROLOG = re.compile(
    rb"^\s*<\?xml([^>]*?)encoding\s*=\s*[\"'][\w.:+-]+[\"']([^>]*?)\?>",
    re.IGNORECASE,
)
# The same declaration read as a name, for deciding what the bytes mean. Kept
# separate from _XML_PROLOG because that one captures the text around
# "encoding=" in order to rewrite it.
_XML_PROLOG_ENCODING = re.compile(
    rb"^\s*<\?xml[^>]*?encoding\s*=\s*[\"']([\w.:+-]+)[\"']", re.IGNORECASE
)
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:+-]+)[\"']?", re.IGNORECASE)

# Charsets whose byte-to-codepoint mapping is already identical to UTF-8 for
# every value they can represent. Transcoding these would only risk mangling
# bytes they were never entitled to hold, so the body passes through untouched.
_PASSTHROUGH = frozenset({
    "utf-8", "utf8", "u8", "unicode-1-1-utf-8",
    "ascii", "us-ascii",
})


def _is_passthrough(name: str) -> bool:
    return name.lower().replace("_", "-").replace(" ", "") in _PASSTHROUGH


def _charset_of(data: bytes, content_type: str | None) -> str:
    """The charset this body claims to be in, defaulting to UTF-8.

    The XML prolog wins over the HTTP header. It is a statement about the
    document itself, whereas a CDN that labels every response ``ISO-8859-1``
    is common, and so is a proxy that strips the header entirely. When the two
    disagree, the document is the one that knows what its own bytes mean.
    """
    match = _XML_PROLOG_ENCODING.match(data[:256])
    if match:
        return match.group(1).decode("ascii", "ignore").strip() or "utf-8"
    match = _CHARSET.search(content_type or "")
    if match:
        return match.group(1).strip()
    return "utf-8"


def decode_body(data: bytes, content_type: str | None) -> str:
    """Decode a body to text, honouring a BOM and the declared charset.

    Never raises: an unknown or lying charset name degrades to replacement
    characters rather than taking the run down with it.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    name = _charset_of(data, content_type)
    if _is_passthrough(name):
        return data.decode("utf-8", errors="replace")
    try:
        return data.decode(name, errors="replace")
    except (LookupError, ValueError, TypeError):
        return data.decode("utf-8", errors="replace")


def as_utf8(data: bytes, content_type: str | None) -> bytes:
    """Re-encode a body to UTF-8 so the XML parser reads the right characters.

    The parser is handed bytes and decodes them as UTF-8 itself, so a body that
    is really ISO-8859-1 would arrive as mojibake in every ``<loc>``. The XML
    prolog's ``encoding=`` is rewritten to match: leaving it saying
    ``ISO-8859-1`` over UTF-8 bytes would tell the parser to mis-decode the file
    we just repaired.

    Bodies already in UTF-8 (the overwhelming majority) pass through
    byte-for-byte, so this is a no-op in the normal case.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:]
    name = _charset_of(data, content_type)
    if _is_passthrough(name):
        return data
    try:
        text = data.decode(name, errors="replace")
    except (LookupError, ValueError, TypeError):
        # Unknown charset: the bytes are what they are. Hand them on unchanged
        # rather than guessing and making the data wrong on purpose.
        return data
    out = text.encode("utf-8", errors="replace")
    return _XML_PROLOG.sub(rb'<?xml\1encoding="UTF-8"\2?>', out, count=1)


class FetchedResponse(AbstractWebClientSuccessResponse):
    """Successful response handed back to ultimate-sitemap-parser.

    Subclasses the library's abstract response because the parser does an
    ``assert isinstance(response, AbstractWebClientSuccessResponse)``.
    """

    def __init__(
        self,
        status: int,
        reason: str,
        headers: dict[str, str],
        data: bytes,
        url: str,
    ) -> None:
        self._status = status
        self._reason = reason
        self._headers = {k.lower(): v for k, v in headers.items()}
        self._data = data
        self._url = url

    def status_code(self) -> int:
        return self._status

    def status_message(self) -> str:
        return self._reason or HTTPStatus(self._status).phrase

    def header(self, case_insensitive_name: str) -> str | None:
        return self._headers.get(case_insensitive_name.lower())

    def raw_data(self) -> bytes:
        return self._data

    def url(self) -> str:
        return self._url


class FailedResponse(WebClientErrorResponse):
    """Error response. Carries the taxonomy so callers can filter.

    The library branches on ``isinstance(response, WebClientErrorResponse)``,
    so this must inherit from it.
    """

    def __init__(self, message: str, retryable: bool, error_type: ErrorType) -> None:
        super().__init__(message=message, retryable=retryable)
        self.error_type = error_type


class FetchOutcome:
    """Result of one fetch attempt, independent of the USP interface."""

    __slots__ = ("ok", "status", "reason", "headers", "data", "url", "error_type", "error_message")

    def __init__(
        self,
        *,
        ok: bool,
        url: str,
        status: int = 0,
        reason: str = "",
        headers: dict[str, str] | None = None,
        data: bytes = b"",
        error_type: ErrorType | None = None,
        error_message: str | None = None,
    ) -> None:
        self.ok = ok
        self.url = url
        self.status = status
        self.reason = reason
        self.headers = headers or {}
        self.data = data
        self.error_type = error_type
        self.error_message = error_message


class _RawCapture:
    """Bounded store of raw sitemap text, for verbatim ``<lastmod>``."""

    __slots__ = ("_items", "_bytes")

    def __init__(self) -> None:
        self._items: OrderedDict[str, str] = OrderedDict()
        self._bytes = 0

    def put(self, url: str, text: str) -> None:
        size = len(text)
        if size > MAX_CAPTURED_BYTES // 4:
            return
        if url in self._items:
            old = self._items.pop(url)
            self._bytes -= len(old)
        while (
            self._items
            and (self._bytes + size > MAX_CAPTURED_BYTES or len(self._items) >= MAX_CAPTURED_SITEMAPS)
        ):
            _, evicted = self._items.popitem(last=False)
            self._bytes -= len(evicted)
        self._items[url] = text
        self._bytes += size

    def get(self, url: str) -> str | None:
        return self._items.get(url)

    def urls(self) -> list[str]:
        return list(self._items.keys())


class CompliantWebClient(AbstractWebClient):
    """Synchronous client implementing ``usp.web_client.AbstractWebClient``.

    The sitemap tree walk is tens-to-hundreds of files, so it runs
    sequentially with honest per-host spacing. Concurrency is reserved for the
    50k-URL status prober, where it actually matters.
    """

    def __init__(
        self,
        *,
        budget: Budget,
        gate: SyncHostGate | None = None,
        client: httpx.Client | None = None,
        raw_capture: _RawCapture | None = None,
        permit_networks: tuple[str, ...] = (),
        proxy_url: str | None = None,
    ) -> None:
        self.budget = budget
        self.gate = gate or SyncHostGate()
        self.raw_capture = raw_capture if raw_capture is not None else _RawCapture()
        # Only the test corpus passes this. pipeline.extract never does, so
        # production always refuses private and metadata ranges.
        self.permit_networks = permit_networks
        self._max_response_data_length: int | None = None
        self._owns_client = client is None
        # A single fixed proxy, supplied by the caller for a stated geo/access
        # reason. There is no rotation and no pool: a tool that swaps proxies
        # when it gets a 403 is not checking status codes, it is evading them.
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            # Redirects are followed by hand in fetch(); see MAX_REDIRECT_HOPS.
            follow_redirects=False,
            proxy=proxy_url,
        )
        # Per-URL failure taxonomy, read by discover/parse after the walk.
        self.failures: dict[str, tuple[ErrorType, str]] = {}
        self.host_response_profile: dict[str, list[int]] = {}
        # Original URL -> final URL after redirects, for the summary.
        self.redirects: dict[str, str] = {}

    # ---- USP interface ---------------------------------------------

    def set_max_response_data_length(self, max_response_data_length: int | None) -> None:
        self._max_response_data_length = max_response_data_length

    def get(self, url: str) -> AbstractWebClientResponse:
        outcome = self.fetch(url)
        if outcome.ok:
            return FetchedResponse(
                outcome.status, outcome.reason, outcome.headers, outcome.data, outcome.url
            )
        error_type = outcome.error_type or ErrorType.UNEXPECTED
        self.failures[url] = (error_type, outcome.error_message or "")
        # Retry only what is worth retrying. ultimate-sitemap-parser re-GETs
        # while `retryable()` is true, up to five times, so marking a 404
        # retryable multiplies every missing sitemap into five requests.
        retryable = error_type is ErrorType.TIMEOUT or (
            error_type is ErrorType.HTTP_ERROR and outcome.status in TRANSIENT_STATUSES
        )
        return FailedResponse(outcome.error_message or error_type.value, retryable, error_type)

    # ---- the real work ---------------------------------------------

    def fetch(self, url: str) -> FetchOutcome:
        """Fetch one URL under every guard, following redirects by hand.

        Never raises. Each hop is resolved, checked and paced separately: a
        redirect is a new request to a new host, not a continuation of the
        first one.
        """
        current = url
        for hop in range(MAX_REDIRECT_HOPS + 1):
            try:
                self.budget.check()
            except Exception as exc:
                return self._fail(url, ErrorType.SKIPPED_BUDGET, _at_hop(url, current, str(exc)))

            # Re-check every hop. An open redirect on a public host must not be
            # a tunnel to 169.254.169.254 that the first check already refused.
            try:
                resolve_and_check(current, permit=self.permit_networks)
            except SsrfBlocked as exc:
                return self._fail(url, ErrorType.SSRF_BLOCKED, _at_hop(url, current, exc.reason))
            except UnsupportedScheme as exc:
                return self._fail(url, ErrorType.UNSUPPORTED_SCHEME, _at_hop(url, current, str(exc)))
            except _DnsFailure as exc:
                return self._fail(url, ErrorType.DNS, _at_hop(url, current, exc.reason))
            except Exception as exc:
                return self._fail(
                    url, ErrorType.UNEXPECTED, _at_hop(url, current, f"preflight failed: {exc}")
                )

            try:
                self.budget.spend_request()
            except Exception as exc:
                return self._fail(url, ErrorType.SKIPPED_BUDGET, _at_hop(url, current, str(exc)))

            # Paced against the host actually being contacted this hop.
            self.gate.wait(current)

            try:
                with self._client.stream("GET", current) as response:
                    location = _redirect_location(response)
                    if location is not None:
                        if hop == MAX_REDIRECT_HOPS:
                            return self._fail(
                                url,
                                ErrorType.REDIRECT_LOOP,
                                f"more than {MAX_REDIRECT_HOPS} redirects",
                                status=response.status_code,
                            )
                        current = urljoin(current, location)
                        continue
                    return self._consume(url, current, response)
            except httpx.TimeoutException as exc:
                # The request went out. A read timeout is still work done.
                return self._fail(url, ErrorType.TIMEOUT, _at_hop(url, current, f"timeout: {exc}"))
            except httpx.ConnectError as exc:
                # Nothing was transmitted, so the token is given back. This is
                # the only path where refunding is honest.
                self.budget.refund_request()
                text = str(exc).lower()
                error_type = ErrorType.TLS if "ssl" in text or "tls" in text else ErrorType.DNS
                return self._fail(url, error_type, _at_hop(url, current, str(exc)))
            except httpx.HTTPError as exc:
                return self._fail(
                    url, ErrorType.HTTP_ERROR, _at_hop(url, current, str(exc))
                )
            except Exception as exc:
                return self._fail(
                    url,
                    ErrorType.UNEXPECTED,
                    _at_hop(url, current, f"unexpected transport error: {exc}"),
                )

        return self._fail(url, ErrorType.REDIRECT_LOOP, f"more than {MAX_REDIRECT_HOPS} redirects")

    def _consume(self, url: str, final_url: str, response: httpx.Response) -> FetchOutcome:
        status = response.status_code
        headers = {k: v for k, v in response.headers.items()}
        host = apex_host(final_url) or final_url
        self.host_response_profile.setdefault(host, []).append(status)
        if final_url != url:
            self.redirects[url] = final_url

        if status == 429:
            retry_after = response.headers.get("Retry-After", "")
            self.gate.defer(final_url, _parse_retry_after(retry_after))
            return self._fail(
                url,
                ErrorType.RATE_LIMITED,
                f"429 Too Many Requests (Retry-After: {retry_after or 'unset'})",
                status=status,
            )

        if status in (403, 401, 429, 451, 503):
            return self._fail(
                url,
                ErrorType.BLOCKED,
                f"{status} — target appears to be refusing automated access; not bypassing",
                status=status,
            )

        if status >= 400:
            return self._fail(
                url,
                ErrorType.HTTP_ERROR,
                f"{status} {response.reason_phrase}",
                status=status,
            )

        # Stream with a hard cap. Reading a 3 GB body to truncate it afterwards
        # is exactly the crash we promised would not happen.
        cap = self.budget.max_bytes_per_file
        if self._max_response_data_length:
            cap = min(cap, self._max_response_data_length)

        declared = _content_length(response.headers)
        if declared is not None:
            try:
                self.budget.guard_file_size(declared)
            except _TooLarge as exc:
                return self._fail(url, ErrorType.TOO_LARGE, str(exc), status=status)

        chunks: list[bytes] = []
        observed = 0
        try:
            for chunk in response.iter_bytes():
                observed += len(chunk)
                if observed > cap:
                    return self._fail(
                        url,
                        ErrorType.TOO_LARGE,
                        f"response exceeded {cap} bytes on the wire",
                        status=status,
                    )
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            return self._fail(url, ErrorType.TRUNCATED, f"body read failed: {exc}", status=status)

        data = b"".join(chunks)

        sniffed = sniff(
            data[:8192],
            content_type=response.headers.get("Content-Type"),
            url=final_url,
            content_encoding=response.headers.get("Content-Encoding"),
            declared_length=declared,
            observed_length=observed,
        )

        if sniffed.error_type is not None:
            return self._fail(
                url,
                sniffed.error_type,
                sniffed.detail or sniffed.error_type.value,
                status=status,
            )

        if sniffed.kind is ContentKind.EMPTY:
            return self._fail(url, ErrorType.EMPTY_BODY, "empty body", status=status)

        if sniffed.needs_gunzip:
            try:
                self.budget.guard_gzip_ratio(len(data), _peek_gzip_size(data))
            except _GzipBomb as exc:
                return self._fail(url, ErrorType.GZIP_BOMB, str(exc), status=status)
            try:
                decompressed = gunzip_guarded(
                    data,
                    max_output_bytes=self.budget.max_decompressed_bytes,
                    max_ratio=self.budget.max_gzip_ratio,
                )
            except GunzipError as exc:
                return self._fail(url, exc.kind, exc.message, status=status)
            content_type = response.headers.get("Content-Type")
            body = as_utf8(decompressed, content_type)
            try:
                # Charged on the bytes we will hold rather than the bytes that
                # came off the wire: re-encoding a latin-1 body grows it, and
                # this budget exists to bound the memory a run can occupy.
                self.budget.spend_bytes(len(body))
            except Exception as exc:
                return self._fail(url, ErrorType.SKIPPED_BUDGET, str(exc), status=status)
            self._capture(url, body)
            self._capture(final_url, body)
            # Hand USP the *decompressed* bytes. Its own gunzip runs at most
            # once and only when the path or content-type says gzip, so a
            # double-gzipped body would come out still compressed and be parsed
            # as mojibake. It sees plain bytes now and, on a .gz name, fails its
            # gunzip and falls back to using them as-is — which is correct.
            # They are also UTF-8 by now: the parser decodes them that way
            # itself, so a body that was really ISO-8859-1 has been re-encoded
            # and its XML prolog updated to say so.
            return FetchOutcome(
                ok=True,
                url=url,
                status=status,
                reason=response.reason_phrase,
                headers=_without_content_encoding(headers),
                data=body,
            )

        content_type = response.headers.get("Content-Type")
        body = as_utf8(data, content_type)
        try:
            self.budget.spend_bytes(len(body))
        except Exception as exc:
            return self._fail(url, ErrorType.SKIPPED_BUDGET, str(exc), status=status)

        self._capture(url, body)
        self._capture(final_url, body)
        return FetchOutcome(
            ok=True,
            url=url,
            status=status,
            reason=response.reason_phrase,
            headers=headers,
            data=body,
        )

    def _capture(self, url: str, data: bytes) -> None:
        try:
            # One decode rule for the whole tool, not a second hand-rolled one
            # here. The body has already been normalised to UTF-8 by as_utf8.
            text = decode_body(data, None)
        except Exception:
            return
        # robots.txt is fetched on the discovery path. Its Crawl-delay is a
        # request from the site owner, so learn it and slow this host down for
        # the rest of the run rather than ignoring it and calling the tool
        # compliant.
        try:
            self.gate.learn_robots(url, text)
        except Exception:
            pass
        try:
            self.raw_capture.put(url, text)
        except Exception:
            pass

    def _fail(
        self,
        url: str,
        error_type: ErrorType,
        message: str,
        *,
        status: int = 0,
    ) -> FetchOutcome:
        log.debug("fetch failed %s %s: %s", error_type.value, url, message)
        self.failures[url] = (error_type, message)
        return FetchOutcome(
            ok=False,
            url=url,
            status=status,
            error_type=error_type,
            error_message=message,
        )

    def close(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass

    def __enter__(self) -> "CompliantWebClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _content_length(headers: httpx.Headers) -> int | None:
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _redirect_location(response: httpx.Response) -> str | None:
    """Return the redirect target, or None if this is a final response.

    301/302/303/307/308 only, and only when a Location header is present.
    """
    if response.status_code not in (301, 302, 303, 307, 308):
        return None
    return response.headers.get("Location")


def _without_content_encoding(headers: dict[str, str]) -> dict[str, str]:
    """Drop encoding headers from what we hand the parser.

    We already decompressed the body. Leaving ``Content-Encoding: gzip`` in
    place would tell the parser to expect compressed bytes and get plain ones.
    """
    return {k: v for k, v in headers.items() if k.lower() not in ("content-encoding", "content-length")}


def _at_hop(original: str, current: str, message: str) -> str:
    """Name the hop a failure happened on when it is not the original URL."""
    if current == original:
        return message
    return f"{message} (at {current})"


def _parse_retry_after(value: str) -> float:
    """Seconds to wait, honouring both the delay-seconds and HTTP-date forms.

    The date form is what many CDNs send. Treating it as unparseable and
    waiting 5 s instead is not "honouring Retry-After", it is ignoring it with
    extra steps.
    """
    value = (value or "").strip()
    if not value:
        return 5.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except Exception:
            return 5.0
    return max(0.0, min(seconds, 86400.0))


def _peek_gzip_size(data: bytes) -> int:
    """Best-effort uncompressed size from the gzip ISIZE trailer.

    Only a hint — ISIZE is mod 2^32 — which is why the real enforcement is the
    streaming guard in ``gunzip_guarded``. This exists so an obvious bomb is
    refused before any allocation.
    """
    if len(data) < 4:
        return 0
    return int.from_bytes(data[-4:], "little")
