"""Content sniffing.

Without this, "success" is full of garbage: a WAF challenge page served at
``/sitemap.xml`` returns 200 with ``text/html``, and ultimate-sitemap-parser
would fall through to its plain-text sitemap parser and emit the page's
navigation links as if they were discovered URLs. Same for a ``.gz`` that is
not gzip — the library's stated fallback is "assume it is a non-gzipped
sitemap", which is right for a renamed XML file and wrong for an HTML error
page.

So every fetched body is classified before parsing. Sniffing reads only the
first bytes and closes the stream; it never stores page bodies.
"""

from __future__ import annotations

import enum
import gzip
import io
import re
import zlib

from .models import ErrorType

GZIP_MAGIC = b"\x1f\x8b"

# Only the head is needed to classify. Keeps memory flat and means we are not
# retaining page content, in line with the "no body collection" red line.
SNIFF_HEAD_BYTES = 8192

_HTML_HEAD = re.compile(rb"<!doctype\s+html|<html[\s>]", re.IGNORECASE)
_XML_HEAD = re.compile(rb"<\?xml[\s>]|<urlset[\s>]|<sitemapindex[\s>]|<rss[\s>]|<feed[\s>]")
_BOM = b"\xef\xbb\xbf"

# A robots.txt line: `Directive: value`. Recognition is deliberately narrow —
# the name has to be a real robots directive — because the alternative ("any
# line with a colon") would pass arbitrary prose to the robots parser.
_ROBOTS_DIRECTIVE = re.compile(
    rb"^\s*(user-agent|allow|disallow|sitemap|crawl-delay|host|clean-param|"
    rb"noindex|request-rate|visit-time)\s*:",
    re.IGNORECASE,
)

# Words that mark a bot-challenge or error page rather than a sitemap.
_CHALLENGE_MARKERS = (
    b"captcha",
    b"cf-browser-verification",
    b"cloudflare",
    b"attention required",
    b"access denied",
    b"just a moment",
    b"verify you are human",
    b"enable javascript",
    b"ddos protection",
    b"request blocked",
    b"unusual traffic",
    b"security check",
)


class ContentKind(str, enum.Enum):
    XML = "xml"
    TEXT = "text"          # plain text: a sitemap.txt (one URL per line) or robots.txt
    HTML = "html"
    GZIP = "gzip"
    EMPTY = "empty"
    UNKNOWN = "unknown"


class SniffResult:
    __slots__ = ("kind", "error_type", "detail", "needs_gunzip", "head")

    def __init__(
        self,
        kind: ContentKind,
        *,
        error_type: ErrorType | None = None,
        detail: str | None = None,
        needs_gunzip: bool = False,
        head: bytes = b"",
    ) -> None:
        self.kind = kind
        self.error_type = error_type
        self.detail = detail
        self.needs_gunzip = needs_gunzip
        self.head = head

    @property
    def is_usable(self) -> bool:
        return self.error_type is None and self.kind not in (
            ContentKind.EMPTY,
            ContentKind.HTML,
            ContentKind.UNKNOWN,
        )


def _strip_bom(data: bytes) -> bytes:
    return data[3:] if data.startswith(_BOM) else data


def looks_like_challenge(head: bytes) -> bool:
    lowered = head.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def sniff(
    raw_head: bytes,
    *,
    content_type: str | None = None,
    url: str = "",
    content_encoding: str | None = None,
    declared_length: int | None = None,
    observed_length: int | None = None,
) -> SniffResult:
    """Classify a response body from its head bytes.

    Returns a SniffResult that says whether the body may proceed to parsing,
    and if not, which errorType to report. Never raises.
    """
    head = raw_head[:SNIFF_HEAD_BYTES]
    ct = (content_type or "").lower()
    ce = (content_encoding or "").lower()
    path = url.split("?", 1)[0].lower()
    # What this request was looking for. robots.txt is fetched as part of
    # discovery, and calling it "a sitemap" in an error message is wrong about
    # the request the tool itself made.
    expected = (
        "robots.txt"
        if path.endswith("/robots.txt") or path == "robots.txt"
        else "a sitemap"
    )

    if not head or observed_length == 0:
        return SniffResult(
            ContentKind.EMPTY,
            error_type=ErrorType.EMPTY_BODY,
            detail="response body is empty",
        )

    # Content-Length says more than we received -> truncated on the wire.
    if (
        declared_length is not None
        and observed_length is not None
        and observed_length > 0
        and declared_length > observed_length
    ):
        return SniffResult(
            ContentKind.UNKNOWN,
            error_type=ErrorType.TRUNCATED,
            detail=f"declared {declared_length} bytes, received {observed_length}",
        )

    # --- gzip handling, keyed on magic bytes first, extension second ---
    # The magic bytes are the only trustworthy signal here. httpx decompresses
    # transparently and leaves `Content-Encoding: gzip` on the response, so
    # this function is routinely handed an already-plain body alongside a
    # header that still claims it is compressed. Believing that header is how a
    # perfectly ordinary robots.txt ended up reported as a corrupt gzip file.
    is_gzip_magic = head.startswith(GZIP_MAGIC)
    wants_gzip = (
        "gzip" in ce
        or "gzip" in ct
        or path.endswith(".gz")
        or path.endswith(".xml.gz")
    )

    if is_gzip_magic:
        return SniffResult(
            ContentKind.GZIP,
            needs_gunzip=True,
            detail="gzip magic bytes present",
            head=head,
        )

    if wants_gzip and not is_gzip_magic:
        # Declared or named as gzip but there is no gzip magic. Could be a
        # renamed XML (harmless) or an HTML error page (garbage). Decide on
        # the actual bytes, not on the extension.
        probe = _classify_plain(_strip_bom(head), expected=expected)
        if probe.kind is ContentKind.XML:
            return SniffResult(
                ContentKind.XML, detail="named .gz but is plain XML", head=head
            )
        if probe.kind is ContentKind.TEXT and not probe.error_type:
            return SniffResult(
                ContentKind.TEXT,
                detail="named .gz but is plain text",
                head=head,
            )
        return SniffResult(
            probe.kind,
            error_type=ErrorType.NOT_GZIP,
            detail="extension/encoding says gzip but body is not gzip",
            head=head,
        )

    return _classify_plain(_strip_bom(head), head=head, expected=expected)


def _classify_plain(
    body: bytes, *, head: bytes = b"", expected: str = "a sitemap"
) -> SniffResult:
    """Classify non-gzipped bytes.

    ``expected`` names what the request was looking for and appears in the
    message that reports an HTML body. It is not decoration: ``robots.txt`` is
    never expected to be a sitemap, and telling a user "HTML document returned
    where a sitemap was expected" about a robots.txt fetch is a false statement
    about their own request. Observed on a site whose every missing path 302s
    to its own error page — 23 requests, and the log read as 23 claims the
    tool had no basis for.
    """
    stripped = body.lstrip()
    if not stripped:
        return SniffResult(
            ContentKind.EMPTY,
            error_type=ErrorType.EMPTY_BODY,
            detail="body is only whitespace",
        )

    if _XML_HEAD.match(stripped):
        return SniffResult(ContentKind.XML, head=head or body)

    if _HTML_HEAD.match(stripped):
        detail = f"HTML document returned where {expected} was expected"
        if looks_like_challenge(body):
            detail = "bot-challenge or block page detected"
            return SniffResult(
                ContentKind.HTML,
                error_type=ErrorType.HTML_CHALLENGE,
                detail=detail,
                head=head or body,
            )
        return SniffResult(
            ContentKind.HTML,
            error_type=ErrorType.NOT_XML,
            detail=detail,
            head=head or body,
        )

    # Both plain-text formats are recognised before the challenge sweep below,
    # and that order is the point. A robots.txt legitimately names paths and one
    # of them can be called `captcha`; a text sitemap can list one. Sweeping for
    # challenge markers first threw away the very file discovery depends on
    # most — robots.txt, whose `Sitemap:` lines are the primary way a site says
    # where its sitemap is. Refusing it is how a site that publishes a sitemap
    # gets reported as publishing none. An HTML page never reaches here: the
    # doctype check above has already caught it.
    first_line = _first_meaningful(stripped)

    if first_line is not None and _ROBOTS_DIRECTIVE.match(first_line):
        return SniffResult(
            ContentKind.TEXT,
            detail="robots.txt directives",
            head=head or body,
        )

    # Plain-text sitemap: one URL per line, with blank lines and "#" comments
    # allowed. Real sitemap.txt files carry comments, and the format has no
    # other way to annotate one, so requiring the very first byte to be a URL
    # rejected perfectly good files. Only the first *meaningful* line decides.
    if first_line is not None and (
        first_line.startswith(b"http://") or first_line.startswith(b"https://")
    ):
        return SniffResult(ContentKind.TEXT, head=head or body)

    if looks_like_challenge(body):
        return SniffResult(
            ContentKind.HTML,
            error_type=ErrorType.HTML_CHALLENGE,
            detail="block-page markers in non-XML body",
            head=head or body,
        )

    return SniffResult(
        ContentKind.UNKNOWN,
        error_type=ErrorType.NOT_XML,
        detail="body is neither XML, a text sitemap, nor recognizable HTML",
        head=head or body,
    )


def _first_meaningful(stripped: bytes) -> bytes | None:
    """The first line that is not blank or a ``#`` comment, stripped.

    Real sitemap.txt and robots.txt files both carry comments, and neither
    format can annotate anything without one, so the first byte of the file
    tells you nothing. The first *meaningful* line does.
    """
    for line in stripped[:2000].splitlines():
        entry = line.strip()
        if not entry or entry.startswith(b"#"):
            continue
        return entry
    return None


class GunzipError(Exception):
    def __init__(self, kind: ErrorType, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def gunzip_guarded(
    data: bytes,
    *,
    max_output_bytes: int,
    max_ratio: int,
) -> bytes:
    """Decompress with both an absolute output cap and a ratio cap.

    ultimate-sitemap-parser's own gunzip only has the absolute cap; the ratio
    cap is what rejects a 2 KB file advertising 200 MB as suspicious instead
    of merely truncating it.
    """
    if not data:
        raise GunzipError(ErrorType.EMPTY_BODY, "no data to decompress")

    compressed_len = len(data)
    if compressed_len * max_ratio < max_output_bytes:
        # Even a fully malicious ratio could not reach the absolute cap from
        # this input size; still cap output at the ratio boundary.
        ratio_cap = compressed_len * max_ratio
    else:
        ratio_cap = max_output_bytes

    limit = min(max_output_bytes, ratio_cap)

    chunks: list[bytes] = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            while True:
                chunk = gz.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise GunzipError(
                        ErrorType.GZIP_BOMB,
                        f"decompressed size exceeded {limit} bytes "
                        f"(compressed {compressed_len})",
                    )
                chunks.append(chunk)
    except GunzipError:
        raise
    except (gzip.BadGzipFile, EOFError, zlib.error, OSError) as exc:
        raise GunzipError(ErrorType.BAD_GZIP, f"not valid gzip data: {exc}") from exc

    out = b"".join(chunks)

    # A gzip member can be followed by another (double gzip from servers that
    # add Content-Encoding: gzip on top of an already-gzipped .xml.gz).
    if out.startswith(GZIP_MAGIC):
        try:
            return gunzip_guarded(
                out,
                max_output_bytes=max_output_bytes,
                max_ratio=max_ratio,
            )
        except GunzipError as exc:
            # Keep the inner kind. Remapping a nested GZIP_BOMB to BAD_GZIP
            # would tell the operator "this file is corrupt" when the truth is
            # "this file is an attack", and the two need different responses.
            if exc.kind is ErrorType.GZIP_BOMB:
                raise
            raise GunzipError(
                exc.kind, f"nested gzip member unreadable: {exc.message}"
            ) from exc

    return out
