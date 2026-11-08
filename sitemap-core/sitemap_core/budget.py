"""Work budgets: how much we *do*, not just how much we output.

`maxUrls` caps output rows. It does not cap work. A sitemap index may legally
reference 50,000 child sitemaps, and every one must be fetched before the
first `<url>` appears. Without independent request and byte budgets the tool
is a bomb — a 2 KB `.gz` expanding to 3 GB is exactly the kind of crash we
promise will not happen.

Thread-safe: the synchronous sitemap walk and the async status prober share
one instance.
"""

from __future__ import annotations

import threading
import time

from .models import BudgetExhausted


class Budget:
    """Hard caps on requests, decompressed bytes, per-file size and wall clock."""

    __slots__ = (
        "_lock",
        "max_requests",
        "max_decompressed_bytes",
        "max_bytes_per_file",
        "max_gzip_ratio",
        "max_seconds",
        "_requests",
        "_bytes",
        "_started",
        "_exhausted_kind",
    )

    def __init__(
        self,
        *,
        max_requests: int = 20_000,
        max_decompressed_bytes: int = 512 * 1024 * 1024,
        max_bytes_per_file: int = 64 * 1024 * 1024,
        max_gzip_ratio: int = 200,
        max_seconds: float | None = 1800.0,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if max_decompressed_bytes < 1:
            raise ValueError("max_decompressed_bytes must be >= 1")
        if max_bytes_per_file < 1:
            raise ValueError("max_bytes_per_file must be >= 1")
        if max_gzip_ratio < 1:
            raise ValueError("max_gzip_ratio must be >= 1")

        self._lock = threading.Lock()
        self.max_requests = max_requests
        self.max_decompressed_bytes = max_decompressed_bytes
        self.max_bytes_per_file = max_bytes_per_file
        self.max_gzip_ratio = max_gzip_ratio
        self.max_seconds = max_seconds

        self._requests = 0
        self._bytes = 0
        self._started = time.monotonic()
        self._exhausted_kind: str | None = None

    # ---- accounting -------------------------------------------------

    @property
    def requests_used(self) -> int:
        with self._lock:
            return self._requests

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def exhausted(self) -> bool:
        return self.exhausted_kind is not None

    @property
    def exhausted_kind(self) -> str | None:
        with self._lock:
            if self._exhausted_kind is not None:
                return self._exhausted_kind
        if self.max_seconds is not None and self.elapsed > self.max_seconds:
            with self._lock:
                self._exhausted_kind = "time"
            return "time"
        return None

    def check(self) -> None:
        """Raise BudgetExhausted if any budget is spent."""
        kind = self.exhausted_kind
        if kind is not None:
            raise BudgetExhausted(kind)

    def spend_request(self) -> None:
        """Charge one request. Raises before allowing the request."""
        self.check()
        with self._lock:
            if self._exhausted_kind is not None:
                raise BudgetExhausted(self._exhausted_kind)
            if self._requests >= self.max_requests:
                self._exhausted_kind = "requests"
                raise BudgetExhausted("requests")
            self._requests += 1

    def refund_request(self) -> None:
        """Give a request back when it was never actually sent."""
        with self._lock:
            if self._requests > 0:
                self._requests -= 1

    def spend_bytes(self, decompressed_bytes: int) -> None:
        """Charge decompressed bytes for one fetched sitemap file."""
        if decompressed_bytes < 0:
            raise ValueError("decompressed_bytes must be >= 0")
        self.check()
        with self._lock:
            if self._exhausted_kind is not None:
                raise BudgetExhausted(self._exhausted_kind)
            if self._bytes + decompressed_bytes > self.max_decompressed_bytes:
                self._exhausted_kind = "bytes"
                raise BudgetExhausted("bytes")
            self._bytes += decompressed_bytes

    # ---- per-file guards (checked before we buffer anything big) ----

    def guard_file_size(self, declared_or_observed: int) -> None:
        """Reject a single file that is too large to be worth buffering."""
        if declared_or_observed > self.max_bytes_per_file:
            raise _TooLarge(declared_or_observed, self.max_bytes_per_file)

    def guard_gzip_ratio(self, compressed: int, decompressed: int) -> None:
        """Reject a compression bomb before it allocates.

        Absolute output caps (spend_bytes / max_bytes_per_file) already prevent
        OOM; the ratio guard exists so a 2 KB file advertising 200 MB is
        rejected as suspicious rather than merely truncated.
        """
        if compressed <= 0:
            return
        if decompressed > compressed * self.max_gzip_ratio:
            raise _GzipBomb(compressed, decompressed, self.max_gzip_ratio)


class _TooLarge(Exception):
    def __init__(self, size: int, limit: int):
        super().__init__(f"file is {size} bytes, limit is {limit}")
        self.size = size
        self.limit = limit


class _GzipBomb(Exception):
    def __init__(self, compressed: int, decompressed: int, limit: int):
        super().__init__(
            f"gzip ratio {decompressed // max(compressed, 1)}:1 exceeds limit {limit}:1"
        )
        self.compressed = compressed
        self.decompressed = decompressed
        self.limit = limit
