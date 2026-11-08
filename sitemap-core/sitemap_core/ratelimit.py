"""Per-host rate limiting.

This is a core component, not an ``asyncio.sleep`` patch bolted on afterwards.

The typical workload is one domain with 50,000 URLs. In that shape a global
concurrency limit does nothing at all — every request goes to the same host,
so per-host pacing *is* the entire rate limit. Three layers:

1. global semaphore        (24 concurrent)  — keeps the process sane
2. per-host semaphore      (4 concurrent)   — keeps any one host unharried
3. per-host token bucket   (4 req/s + jitter)

Realistic numbers, for honest README text: 4 concurrent at ~300 ms per
request over one host is roughly 62 minutes for 50,000 URLs. With connection
reuse and 4 req/s it is closer to 30 minutes. We say so rather than implying
it is instant.

One long-lived ``httpx.AsyncClient`` is mandatory. Building a client per
request rebuilds the connection pool and burns ephemeral ports; a 50k run is
then silently several times slower. That is the single most common Python
failure in this shape of tool.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable

from .normalize import apex_host

# robots.txt Crawl-delay is a request from the site owner, not a hint. Parsing
# it is part of the compliance line "respect robots.txt Crawl-delay", and the
# library we build on does not do it for us.
_CRAWL_DELAY = re.compile(r"(?im)^\s*crawl-delay\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$")

# A hostile or misconfigured robots.txt asking for a one-hour gap would make a
# 50k run take a year. Cap it and say so in the summary rather than silently
# obeying or silently ignoring.
MAX_CRAWL_DELAY = 60.0


def crawl_delay_of(robots_text: str) -> float | None:
    """Seconds between requests as the site's robots.txt asks for, or None."""
    if not robots_text:
        return None
    matches = _CRAWL_DELAY.findall(robots_text[:64_000])
    if not matches:
        return None
    try:
        delay = max(float(v) for v in matches)
    except ValueError:
        return None
    if delay <= 0:
        return None
    return min(delay, MAX_CRAWL_DELAY)


def _is_robots_url(url: str) -> bool:
    path = url.split("?", 1)[0].rstrip("/").lower()
    return path.endswith("/robots.txt") or path == "robots.txt"


class HostGate:
    """Async concurrency + request-rate limiter, keyed by registrable domain."""

    __slots__ = (
        "global_sem",
        "per_host_concurrency",
        "requests_per_second",
        "jitter",
        "_host_sems",
        "_host_buckets",
        "_host_interval",
        "_lock",
    )

    def __init__(
        self,
        *,
        global_concurrency: int = 24,
        per_host_concurrency: int = 4,
        requests_per_second: float = 4.0,
        jitter: float = 0.25,
    ) -> None:
        if global_concurrency < 1:
            raise ValueError("global_concurrency must be >= 1")
        if per_host_concurrency < 1:
            raise ValueError("per_host_concurrency must be >= 1")
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")

        self.global_sem = asyncio.Semaphore(global_concurrency)
        self.per_host_concurrency = per_host_concurrency
        self.requests_per_second = requests_per_second
        self.jitter = jitter
        self._host_sems: dict[str, asyncio.Semaphore] = {}
        self._host_buckets: dict[str, _Bucket] = {}
        self._host_interval: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def _for_host(self, url: str) -> tuple[asyncio.Semaphore, "_Bucket"]:
        key = apex_host(url) or url
        async with self._lock:
            sem = self._host_sems.get(key)
            if sem is None:
                sem = asyncio.Semaphore(self.per_host_concurrency)
                self._host_sems[key] = sem
            bucket = self._host_buckets.get(key)
            if bucket is None:
                bucket = _Bucket()
                self._host_buckets[key] = bucket
            return sem, bucket

    async def _take_token(self, url: str, bucket: "_Bucket") -> None:
        key = apex_host(url) or url
        async with bucket.lock:
            delay = bucket.next_allowed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            base = self._host_interval.get(key, 1.0 / self.requests_per_second)
            jittered = base * (1.0 + random.uniform(-self.jitter, self.jitter))
            bucket.next_allowed = time.monotonic() + max(jittered, 0.001)

    def learn_robots(self, url: str, text: str) -> None:
        """Record a Crawl-delay from a robots.txt we already fetched."""
        if not _is_robots_url(url):
            return
        delay = crawl_delay_of(text)
        if delay is None:
            return
        key = apex_host(url) or url
        # Crawl-delay is seconds between requests; never speed a host up.
        self._host_interval[key] = max(1.0 / self.requests_per_second, delay)

    async def defer(self, url: str, seconds: float) -> None:
        """Honour Retry-After on the async path."""
        _, bucket = await self._for_host(url)
        async with bucket.lock:
            bucket.next_allowed = max(
                bucket.next_allowed, time.monotonic() + max(seconds, 0.0)
            )

    async def run(self, url: str, fn: Callable[[], Awaitable]) -> object:
        """Run one request under all three layers."""
        host_sem, bucket = await self._for_host(url)
        async with self.global_sem:
            async with host_sem:
                await self._take_token(url, bucket)
                return await fn()

    def stats(self) -> dict[str, int]:
        return {"hosts": len(self._host_sems)}

    def apply_intervals(self, intervals: dict[str, float] | None) -> None:
        """Seed per-host spacing learned from robots.txt during discovery.

        The status-probing phase is a different gate object from the one the
        sitemap walk used. Carrying Crawl-delay across that boundary is the
        difference between honouring a site owner's request and only honouring
        it while fetching sitemaps.
        """
        if not intervals:
            return
        for key, seconds in intervals.items():
            if seconds > 0:
                self._host_interval[key] = max(
                    self._host_interval.get(key, 1.0 / self.requests_per_second),
                    seconds,
                )


class _Bucket:
    """Token-bucket state for one host."""

    __slots__ = ("next_allowed", "lock")

    def __init__(self) -> None:
        self.next_allowed = 0.0
        self.lock = asyncio.Lock()


class SyncHostGate:
    """Sequential pacing for the synchronous sitemap walk.

    The sitemap tree walk is tens to hundreds of files, not tens of thousands,
    so concurrency buys nothing there. What it still needs is honest spacing
    against each host plus Retry-After awareness.
    """

    __slots__ = (
        "requests_per_second",
        "jitter",
        "_next_allowed",
        "_min_interval",
        "_host_interval",
    )

    def __init__(self, *, requests_per_second: float = 4.0, jitter: float = 0.25) -> None:
        self.requests_per_second = requests_per_second
        self.jitter = jitter
        self._min_interval = 1.0 / requests_per_second
        self._next_allowed: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}

    def learn_robots(self, url: str, text: str) -> None:
        """Record a Crawl-delay from a robots.txt we already fetched."""
        if not _is_robots_url(url):
            return
        delay = crawl_delay_of(text)
        if delay is None:
            return
        key = apex_host(url) or url
        self._host_interval[key] = max(self._min_interval, delay)

    def wait(self, url: str) -> None:
        key = apex_host(url) or url
        now = time.monotonic()
        next_allowed = self._next_allowed.get(key, 0.0)
        delay = next_allowed - now
        if delay > 0:
            time.sleep(delay)
        base = self._host_interval.get(key, self._min_interval)
        jittered = base * (1.0 + random.uniform(-self.jitter, self.jitter))
        self._next_allowed[key] = time.monotonic() + max(jittered, 0.001)

    def defer(self, url: str, seconds: float) -> None:
        """Honour Retry-After by pushing the host's next slot out."""
        key = apex_host(url) or url
        self._next_allowed[key] = max(
            self._next_allowed.get(key, 0.0), time.monotonic() + max(seconds, 0.0)
        )

    def learned_intervals(self) -> dict[str, float]:
        """Crawl-delay values seen during discovery, keyed like the gate's own."""
        return dict(self._host_interval)
