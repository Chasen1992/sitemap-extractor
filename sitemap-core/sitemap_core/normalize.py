"""URL normalization, deduplication and cross-domain policy.

Policy, written down before the code (so it is a decision, not an accident):

**Normalization** — lowercase scheme and host, drop the default port, drop the
fragment, resolve ``.``/``..``, collapse duplicate slashes in the path, and
IDNA-encode a non-ASCII host.

**Trailing slashes are significant and are preserved.** Removing one can turn
a 200 into a 404, which produces confidently wrong data. Wrong data is worse
than a duplicate row.

**Dedup happens before status checking.** Real sitemaps repeat the same URL
across the index and child layers, and repeat it again as a fragment variant,
a mixed-case host, a default port, or a percent-escape written in the wrong
case. All of those collapse here. Deduping first typically saves 10-30% of
requests.

**``http`` and ``https``, and ``www`` and the apex, are kept distinct.** They
are different URLs and frequently serve different content, so merging them
would invent data. ``https://example.com/page`` and
``https://www.example.com/page`` are two rows unless the target redirects one
to the other — and only the status check can tell us that.

**Cross-domain ``<loc>`` entries are emitted but never probed.** Real sitemaps
list CDN and partner hosts. Probing a third party that the target merely
referenced is exactly the behaviour that gets a crawler flagged, and it is not
the host the user asked about. Such rows are marked ``crossDomain: true`` and
``errorType: cross_domain`` so they are filterable rather than silently
dropped or silently probed.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit, urlunsplit

from .ssrf import ALLOWED_SCHEMES, UnsupportedScheme

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Collapse runs of slashes in the path only, never in the scheme separator.
_MULTI_SLASH = re.compile(r"(?<!:)/{2,}")

# Any percent-escape, so it can be canonicalised.
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


def _normalize_escapes(text: str) -> str:
    """Canonicalise percent-escapes without changing the path's structure.

    An escape for an *unreserved* character (RFC 3986: ALPHA / DIGIT / "-",
    ".", "_", "~") is decoded, because ``%67`` and ``g`` name the same thing to
    every server. Anything else keeps its escape with upper-case hex, so
    ``%2f`` and ``%2F`` agree — but neither is turned into a literal ``/``.

    That last part matters: decoding ``%2F`` would merge ``/a%2Fb`` (one path
    segment containing a slash) with ``/a/b`` (two segments), which are
    different resources. Collapsing them silently would turn a real 200/404
    difference into confidently wrong data, and wrong data is worse than a
    duplicate row.
    """

    def repl(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        if char.isascii() and (char.isalnum() or char in "-._~"):
            return char
        return "%" + match.group(1).upper()

    return _ESCAPE.sub(repl, text)


def _canonical(text: str, safe: str) -> str:
    """Percent-encode non-ASCII as UTF-8, then normalise existing escapes.

    ``%`` is in ``safe`` so already-escaped input is not double-encoded. This
    makes a raw ``café`` and an escaped ``caf%C3%A9`` the same key, which is the
    duplicate noise real sitemaps actually contain.
    """
    try:
        encoded = quote(text, safe=safe + "%")
    except (UnicodeError, ValueError):
        return text
    return _normalize_escapes(encoded)


def _make_extractor():
    """Build a Public Suffix List extractor that never touches the network.

    tldextract fetches the PSL over HTTP on first use and caches it. In a
    restricted or unreliable network that is a silent hang on the first call,
    and a 50,000-URL run makes a call per row. We use the bundled snapshot
    only and reuse one instance so results are memoised.
    """
    try:
        import tldextract
    except ImportError:
        return None
    return tldextract.TLDExtract(suffix_list_urls=())


_TLD = _make_extractor()


def _resolve_path(path: str) -> str:
    """Apply RFC 3986 dot-segment removal, preserving the trailing slash.

    ``/a/b/../c/`` becomes ``/a/c/``. A trailing slash survives because it
    marks a different resource than the same path without one, and we refuse
    to guess which one the server meant.
    """
    if not path:
        return "/"
    trailing_slash = path.endswith("/")
    segments = path.split("/")
    resolved: list[str] = []
    for segment in segments:
        if segment == ".":
            continue
        if segment == "..":
            if len(resolved) > 1:
                resolved.pop()
            continue
        resolved.append(segment)
    out = "/".join(resolved) or "/"
    if not out.startswith("/"):
        out = "/" + out
    if trailing_slash and not out.endswith("/"):
        out += "/"
    return out


class InvalidUrl(Exception):
    def __init__(self, url: str, reason: str):
        super().__init__(f"{url!r}: {reason}")
        self.url = url
        self.reason = reason


def _idna_host(host: str) -> str:
    """Encode a non-ASCII host to its IDNA (punycode) form."""
    if not host:
        return host
    try:
        host.encode("ascii")
        return host.lower()
    except UnicodeEncodeError:
        pass
    try:
        # A host may mix ASCII labels with non-ASCII ones.
        labels = host.split(".")
        encoded = []
        for label in labels:
            try:
                label.encode("ascii")
                encoded.append(label.lower())
            except UnicodeEncodeError:
                encoded.append(label.encode("idna").decode("ascii"))
        return ".".join(encoded)
    except UnicodeError as exc:
        raise InvalidUrl(host, f"host cannot be IDNA-encoded: {exc}") from exc


def normalize_url(url: str) -> str:
    """Return the canonical form used as the dedup key and the emitted URL."""
    if url is None:
        raise InvalidUrl("", "url is None")
    candidate = url.strip()
    if not candidate:
        raise InvalidUrl("", "url is empty")

    # Fragments carry no server-side meaning and are pure duplicate noise.
    candidate = candidate.split("#", 1)[0]

    parts = urlsplit(candidate)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsupportedScheme(url, scheme or "<missing>")

    if not parts.netloc:
        raise InvalidUrl(url, "URL has no host")

    host = parts.hostname
    if not host:
        raise InvalidUrl(url, "URL has no host")
    host = _idna_host(host)

    port = parts.port
    try:
        if port is not None and port == _DEFAULT_PORTS[scheme]:
            netloc = host
        elif port is not None:
            netloc = f"{host}:{port}"
        else:
            netloc = host
    except ValueError as exc:
        raise InvalidUrl(url, f"invalid port: {exc}") from exc

    # userinfo is not part of sitemap loc values and should never be echoed
    if parts.username or parts.password:
        raise InvalidUrl(url, "credentials in URL are not accepted")

    path = parts.path or "/"
    # Percent-escapes are canonicalised, not decoded: %67 becomes g, %2F stays
    # an escaped slash. See _normalize_escapes.
    path = _canonical(path, "/-_.~!$&'()*+,;=:@")
    path = _MULTI_SLASH.sub("/", path)
    if not path.startswith("/"):
        path = "/" + path
    path = _resolve_path(path)

    query = _canonical(parts.query, "/-_.~!$&'()*+,;=:@/?")

    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def scheme_of(url: str) -> str:
    return (urlsplit(url).scheme or "").lower()


def is_same_site(url: str, seed_host: str, *, include_subdomains: bool) -> bool:
    """Decide whether a URL belongs to the site the user asked about.

    Uses the Public Suffix List so ``foo.co.uk`` is not treated as a subdomain
    of ``bar.co.uk``. String suffix matching gets that wrong.

    ``www`` and the apex host are one site, and ``include_subdomains`` has no
    say in it. Almost every sitemap lists the apex while the user types ``www``
    (or the other way round), so treating that pair as foreign would mark every
    row from the site's own sitemap as ``cross_domain`` and refuse to check any
    of them. The flag governs real subdomains — ``shop.example.com`` is part of
    the site when it is on, foreign when it is off.
    """
    host = host_of(url)
    if not host or not seed_host:
        return False
    if host == seed_host:
        return True

    registered = _registrable(seed_host)

    if _TLD is None or registered is None:
        # Without a usable PSL we fall back to literal suffix matching, which
        # is wrong for multi-label suffixes like ``co.uk``. Better than an
        # answer of "different site" for every URL.
        return host.endswith("." + seed_host) or seed_host.endswith("." + host)

    ext_host = _TLD(host)
    if not ext_host.suffix:
        return host.endswith("." + seed_host) or seed_host.endswith("." + host)

    if ext_host.domain != _TLD(seed_host).domain or ext_host.suffix != _TLD(seed_host).suffix:
        return False

    if {host, seed_host} == {registered, "www." + registered}:
        return True

    return include_subdomains


def _registrable(host: str) -> str | None:
    """The registrable domain of ``host``, or None without a usable PSL."""
    if _TLD is None:
        return None
    ext = _TLD(host)
    if not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def apex_host(url: str) -> str:
    """Registered-domain form, used to group requests for rate limiting."""
    host = host_of(url)
    if _TLD is None:
        return host
    ext = _TLD(host)
    if not ext.suffix:
        return host
    return f"{ext.domain}.{ext.suffix}"
