"""In-memory sliding-window rate limiting, per API key and bucket.

Buckets: "executions", "sessions", "requests" — each backed by a
configurable per-minute limit from app.config. State lives in one process;
this is deliberately simple for the single-worker v1 deployment. The limiter
is deterministic and testable: `now` can be injected.

Applied only to authenticated endpoints, via require_rate_limit(bucket).
"""

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable

from app.config import get_settings

logger = logging.getLogger("exekit.rate_limit")

WINDOW_SECONDS = 60

_BUCKET_LIMIT_ATTR: dict[str, str] = {
    "executions": "rate_limit_executions_per_minute",
    "sessions": "rate_limit_sessions_per_minute",
    "requests": "rate_limit_requests_per_minute",
}


class SlidingWindowLimiter:
    """Per-(bucket, key) sliding window. check() raises RateLimitExceeded when
    the caller would exceed `limit` events inside the window; rejected calls
    do not consume window slots."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, bucket: str, key: str, limit: int, *, now: float | None = None) -> None:
        from app.errors import RateLimitExceeded

        if limit <= 0:  # disabled bucket
            return
        now = time.monotonic() if now is None else now
        events = self._events[(bucket, key)]
        cutoff = now - WINDOW_SECONDS
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= limit:
            retry_after = max(1, int(events[0] + WINDOW_SECONDS - now) + 1)
            logger.warning(
                "rate limit hit bucket=%s key=%s limit=%d retry_after=%ds",
                bucket, key, limit, retry_after,
            )
            raise RateLimitExceeded(
                f"Rate limit exceeded for {bucket}: max {limit} per minute.",
                details={"limit": limit, "retry_after_seconds": retry_after},
            )
        events.append(now)

    def reset(self) -> None:
        self._events.clear()

    def usage(self, bucket: str, key: str) -> int:
        """Current event count in the window (observability/tests)."""
        return len(self._events[(bucket, key)])


limiter = SlidingWindowLimiter()


def get_limiter() -> SlidingWindowLimiter:
    return limiter


def limit_for(bucket: str) -> Callable[[], int]:
    attr = _BUCKET_LIMIT_ATTR[bucket]
    return lambda: getattr(get_settings(), attr)


def require_rate_limit(bucket: str):
    """FastAPI dependency factory: rate-limit an authenticated endpoint for
    the calling API key. Must be applied after authentication (it depends on
    get_current_api_key)."""
    from fastapi import Depends

    from app.dependencies import get_current_api_key

    limit_of = limit_for(bucket)

    async def dependency(api_key=Depends(get_current_api_key)):
        get_limiter().check(bucket, str(api_key.id), limit_of())
        return api_key

    return dependency
