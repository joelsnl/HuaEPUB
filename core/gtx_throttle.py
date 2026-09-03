# Author: joelsnl and Anthropic Claude
"""Per-IP in-flight cap for unofficial Google/Microsoft translate endpoints."""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable, Optional, Tuple

__all__ = [
    "GtxThrottle",
    "RateLimitedError",
    "jittered_backoff_seconds",
    "parse_retry_after",
]


class RateLimitedError(Exception):
    """HTTP 429 (or equivalent) with an optional Retry-After hint."""

    def __init__(self, retry_after: float = 8.0):
        self.retry_after = float(retry_after)
        super().__init__(f"rate limited (retry after {self.retry_after:.1f}s)")


def jittered_backoff_seconds(attempt: int) -> float:
    """
    Per-worker 429/error sleep. Same 2/4/8… band as v2.6.4, spread so a
    wave does not wake as one stampede.
    """
    base = float(2 ** (max(0, int(attempt)) + 1))
    return random.uniform(base * 0.75, base * 1.5)


def parse_retry_after(
    response: Any, default: float = 8.0, cap: float = 60.0
) -> float:
    """Seconds to wait from a Retry-After header. Numeric seconds only."""
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return default
    try:
        return min(cap, max(0.5, float(raw)))
    except (TypeError, ValueError):
        return default


class GtxThrottle:
    """
    Cap unofficial gtx so one home IP is not 429'd into translating nothing.

    max_limit is the UI ceiling (200). Start at 8. A 429 halves the cap
    (floor 2, at most once per 1.5 s) and sets a global cool-until so
    *new* GETs wait — per-worker 2/4/8 s while 199 others keep going does
    not drop aggregate RPS. Successes add 1 toward the ceiling.
    """

    MIN_LIMIT = 2
    START_LIMIT = 8
    CUT_INTERVAL = 1.5
    MIN_COOL = 8.0
    MAX_COOL = 60.0

    def __init__(
        self,
        max_limit: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_change: Optional[Callable[[int, int], None]] = None,
    ):
        self.max_limit = max(1, int(max_limit or 1))
        self.min_limit = min(self.MIN_LIMIT, self.max_limit)
        self.limit = max(self.min_limit, min(self.max_limit, self.START_LIMIT))
        self.current = 0
        self.cool_until = 0.0
        self._cool_step = self.MIN_COOL
        self._last_cut = float("-inf")
        self._clock = clock
        self._on_change = on_change
        self._cv = threading.Condition()

    def _notify_change(self) -> None:
        cb = self._on_change
        if cb is not None:
            try:
                cb(self.current, self.limit)
            except Exception:
                pass

    def would_admit(self) -> bool:
        with self._cv:
            return self.current < self.limit and float(self._clock()) >= self.cool_until

    def acquire(
        self,
        should_cancel: Callable[[], bool],
        wait_paused: Callable[[], None],
        sleep_fn: Callable[[float], None],
    ) -> bool:
        while True:
            if should_cancel():
                return False
            wait_paused()
            if should_cancel():
                return False
            delay = 0.2
            with self._cv:
                if should_cancel():
                    return False
                now = float(self._clock())
                cooling = now < self.cool_until
                if not cooling and self.current < self.limit:
                    self.current += 1
                    self._notify_change()
                    return True
                if cooling:
                    delay = min(0.2, max(0.01, self.cool_until - now))
            before = float(self._clock())
            sleep_fn(delay)
            if should_cancel():
                return False
            after = float(self._clock())
            # Pytest patches time.sleep to a no-op; don't busy-spin for cool_until.
            if delay > 0.05 and after < before + (delay * 0.25):
                with self._cv:
                    if after < self.cool_until:
                        self.cool_until = after
                    self._cv.notify_all()

    def release(self) -> None:
        with self._cv:
            self.current = max(0, int(self.current) - 1)
            self._cv.notify()
            self._notify_change()

    def on_success(self) -> Optional[int]:
        """+1 toward max. Returns limit when it hits a log milestone."""
        with self._cv:
            self._cool_step = self.MIN_COOL
            if self.limit >= self.max_limit:
                return None
            self.limit += 1
            self._cv.notify_all()
            self._notify_change()
            if self.limit in (4, 8, 16, 32, 64, 128) or self.limit == self.max_limit:
                return self.limit
            return None

    def on_429(self, retry_after: float = 8.0) -> Tuple[Optional[int], float]:
        """Halve at most once per CUT_INTERVAL; extend global cool."""
        now = float(self._clock())
        wait = min(
            self.MAX_COOL,
            max(self.MIN_COOL, float(retry_after or self.MIN_COOL)),
        )
        with self._cv:
            changed = False
            if now - self._last_cut >= self.CUT_INTERVAL:
                self._last_cut = now
                if now < self.cool_until:
                    self._cool_step = min(
                        self.MAX_COOL, max(self.MIN_COOL, self._cool_step * 2)
                    )
                else:
                    self._cool_step = self.MIN_COOL
                new = max(self.min_limit, self.limit // 2)
                if new < self.limit:
                    self.limit = new
                    changed = True
            wait = max(wait, float(self._cool_step))
            self.cool_until = max(self.cool_until, now + wait)
            self._cv.notify_all()
            if changed:
                self._notify_change()
                return self.limit, wait
            return None, wait

    def wake(self) -> None:
        with self._cv:
            self._cv.notify_all()
