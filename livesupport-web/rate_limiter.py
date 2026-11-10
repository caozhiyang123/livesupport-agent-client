"""Rate_Limiter module for LiveSupport Web Chat.

A minimal in-memory fixed/sliding-window request rate limiter keyed by
client identity (IP address or client-supplied header). Configured via
the ``RATE_LIMIT_MAX_REQUESTS`` / ``RATE_LIMIT_WINDOW_SECONDS``
environment variables (Req 6.1, 6.3).

Sufficient for a single-instance MVP: state is a plain in-memory
``{client_key: deque[timestamp]}`` dict, not shared across processes
or instances.
"""

import os
import threading
import time
from collections import deque

# Default request quota per window if RATE_LIMIT_MAX_REQUESTS is unset.
DEFAULT_RATE_LIMIT_MAX_REQUESTS = 10

# Default window length in seconds if RATE_LIMIT_WINDOW_SECONDS is unset.
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60


def _get_max_requests() -> int:
    """Reads RATE_LIMIT_MAX_REQUESTS from the environment, with a default."""
    value = os.environ.get("RATE_LIMIT_MAX_REQUESTS")
    if not value:
        return DEFAULT_RATE_LIMIT_MAX_REQUESTS
    return int(value)


def _get_window_seconds() -> float:
    """Reads RATE_LIMIT_WINDOW_SECONDS from the environment, with a
    default."""
    value = os.environ.get("RATE_LIMIT_WINDOW_SECONDS")
    if not value:
        return DEFAULT_RATE_LIMIT_WINDOW_SECONDS
    return float(value)


class RateLimiter:
    """In-memory sliding-window rate limiter keyed by client identity.

    For each client key, keeps a deque of request timestamps within the
    current window. A request is allowed only if fewer than
    ``max_requests`` timestamps remain in the window after evicting
    stale ones; allowed requests are then recorded (Req 6.1, 6.3).

    Not safe for multi-process/multi-instance deployments; state is
    local to this process, which is acceptable for a single-instance
    MVP per the design.
    """

    def __init__(self, max_requests: int | None = None,
                 window_seconds: float | None = None) -> None:
        self.max_requests = (
            max_requests if max_requests is not None else _get_max_requests()
        )
        self.window_seconds = (
            window_seconds
            if window_seconds is not None
            else _get_window_seconds()
        )
        self._requests: dict[str, deque] = {}
        self._lock = threading.Lock()

    def is_allowed(self, client_key: str) -> bool:
        """Returns True if `client_key` may make a request right now.

        Evicts timestamps older than the configured window, then
        admits the request (recording its timestamp) only if doing so
        would not exceed `max_requests` within the window. Callable
        before any AgentClient invocation (Req 6.3).
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds

        with self._lock:
            timestamps = self._requests.setdefault(client_key, deque())

            while timestamps and timestamps[0] < cutoff:
                timestamps.popleft()

            if len(timestamps) >= self.max_requests:
                return False

            timestamps.append(now)
            return True


# Module-level limiter instance shared by the application (Req 6.1, 6.3).
rate_limiter = RateLimiter()


def is_allowed(client_key: str) -> bool:
    """Checks whether `client_key` is allowed to make a request now,
    using the shared module-level RateLimiter instance."""
    return rate_limiter.is_allowed(client_key)


def get_client_key(
    client_ip: str | None, header_value: str | None = None
) -> str:
    """Derives the Rate_Limiter client key for a request.

    Prefers a client-supplied header value when present (e.g. a
    client-generated identifier forwarded by a proxy), falling back to
    the connecting client's IP address (Req 6.1).
    """
    if header_value:
        return header_value
    return client_ip or "unknown"
