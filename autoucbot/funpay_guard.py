"""Shared, conservative FunPay rate-limit state; never bypasses access controls.

The server's Retry-After takes precedence over our minimum cooldown. Rate limits
survive restarts via the existing SQLite settings table. The local request budget
is intentionally only an operational safety limit, not a promised FunPay quota.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import threading
import time


class FunPayDeferredError(Exception):
    def __init__(self, wait_seconds: float, reason: str = "cooldown"):
        self.wait_seconds = max(1, int(wait_seconds + .999))
        self.reason = reason
        super().__init__(
            f"FunPay: запросы приостановлены на {self.wait_seconds} сек. "
            "Дождитесь восстановления доступа; не повторяйте подключение вручную."
        )


class FunPayRateLimitedError(FunPayDeferredError):
    """Exactly one response was 429; subsequent attempts must not hit the network."""
    def __init__(self, wait_seconds: float):
        super().__init__(wait_seconds, "server_429")


def retry_after_seconds(value, now=None):
    """RFC 9110 Retry-After (delta seconds or HTTP-date), without trusting infinity."""
    now = time.time() if now is None else now
    if value is None:
        return 0
    value = str(value).strip()
    if value.isdecimal():
        return min(7 * 86400, int(value))
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0, min(7 * 86400, int(when.timestamp() - now + .999)))
    except (ValueError, TypeError, OverflowError, IndexError):
        return 0


class FunPayTraffic:
    """Single-process pacing plus database-persisted stop signal for every caller."""
    MIN_GAP_SECONDS = 2.5
    MAX_REQUESTS_PER_MINUTE = 12
    FIRST_429_PAUSE = 15 * 60
    MAX_DEFAULT_PAUSE = 4 * 3600

    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._starts = deque()
        self._last_start = 0.0

    def until(self):
        return float(self.db.setting("funpay_cooldown_until", 0) or 0)

    def is_blocked(self):
        return self.until() > time.time()

    def before(self):
        """Never send during cooldown; locally defer after exhausting budget."""
        with self._lock:
            now = time.time()
            remaining = self.until() - now
            if remaining > 0:
                raise FunPayDeferredError(remaining, "cooldown")
            while self._starts and now - self._starts[0] >= 60:
                self._starts.popleft()
            if len(self._starts) >= self.MAX_REQUESTS_PER_MINUTE:
                raise FunPayDeferredError(60 - (now - self._starts[0]), "local_budget")
            delay = self.MIN_GAP_SECONDS - (now - self._last_start)
            if 0 < delay <= self.MIN_GAP_SECONDS:
                time.sleep(delay)
                now = time.time()
                remaining = self.until() - now
                if remaining > 0:
                    raise FunPayDeferredError(remaining, "cooldown")
            self._starts.append(now)
            self._last_start = now

    def limited(self, headers, path=""):
        """Record a single 429 and persist increasing pauses; no auto-retry."""
        now = time.time()
        strikes = int(self.db.setting("funpay_429_strikes", 0) or 0) + 1
        server_retry = retry_after_seconds((headers or {}).get("Retry-After"), now)
        wait = max(
            server_retry,
            min(self.MAX_DEFAULT_PAUSE, self.FIRST_429_PAUSE * (2 ** min(strikes - 1, 6))),
        )
        until = now + wait
        self.db.set("funpay_cooldown_until", until)
        self.db.set("funpay_429_strikes", strikes)
        self.db.runtime("funpay_guard", {
            "ok": False, "reason": "http_429", "until": until, "strikes": strikes,
            "server_retry_after_seconds": server_retry,
            "endpoint": path[:100] if path.startswith("/") else "unknown",
            "message": "Ожидание перед новой попыткой, не переключайте IP/UA циклически",
        })
        raise FunPayRateLimitedError(wait)

    def ok(self):
        if self.db.setting("funpay_429_strikes", 0):
            self.db.set("funpay_429_strikes", 0)
            self.db.set("funpay_cooldown_until", 0)
            self.db.runtime("funpay_guard", {"ok": True, "reason": "ready", "until": 0})
