"""Sliding-window rate budget for a single (account, action) pair.

Separate from the orchestrator's follow-slot budget, which BLOCKS until a
slot frees. That's right for cold-start's follow loop, which has nothing
else to do while it waits, but wrong for an action chosen inside the
per-post decision loop: blocking there would stall a whole activation for
minutes at a time. So this budget is non-blocking -- the caller asks, gets
a yes/no, and on "no" the decision is downgraded to a logged no_action
instead of the agent sitting idle.

Sized from live data rather than a documented platform limit: see the
retweet-reliability entry in ROADMAP.md's Phase 4 follow-ups. The platform's
own refusal messages proved unreliable (one reported a daily limit that
demonstrably wasn't reached), so these caps are a conservative pacing
choice, not a measured ceiling.
"""

import threading
import time

from utils.duration import parse_duration


class ActionRateBudget:
    """Allows at most `max_per_hour` consumptions in any rolling hour, and
    never two closer together than `min_interval_seconds`.

    Both constraints matter independently: in the 2026-09-09 live data the
    clearest refusal correlate was burst density (5 attempts inside 5
    minutes) rather than hourly volume, which an hourly cap alone would
    happily permit.
    """

    WINDOW_SECONDS = 3600

    def __init__(self, max_per_hour=None, min_interval_seconds=0):
        self.max_per_hour = max_per_hour
        self.min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._timestamps = []

    def try_consume(self) -> tuple:
        """Consumes one slot if allowed.

        Returns (allowed, retry_after_seconds). `retry_after_seconds` is how
        long until a slot would next free up -- for logging, since nothing
        here waits.
        """
        with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < self.WINDOW_SECONDS]

            if self.min_interval_seconds and self._timestamps:
                since_last = now - self._timestamps[-1]
                if since_last < self.min_interval_seconds:
                    return False, self.min_interval_seconds - since_last

            if self.max_per_hour is not None and len(self._timestamps) >= self.max_per_hour:
                return False, self.WINDOW_SECONDS - (now - self._timestamps[0])

            self._timestamps.append(now)
            return True, 0.0


def build_budgets(action_rate_limits) -> dict:
    """Turns one account's `sockpuppet_config.action_rate_limits` config
    into {action: ActionRateBudget}. Raises on a malformed entry so a typo
    fails at startup rather than silently leaving an action uncapped.
    """
    budgets = {}
    for action, limits in (action_rate_limits or {}).items():
        if not isinstance(limits, dict):
            raise ValueError(
                f"action_rate_limits.{action} must be a mapping with max_per_hour and/or "
                f"min_interval; got {limits!r}."
            )
        max_per_hour = limits.get("max_per_hour")
        min_interval = limits.get("min_interval")
        if max_per_hour is None and min_interval is None:
            raise ValueError(
                f"action_rate_limits.{action} must set at least one of "
                f"max_per_hour/min_interval."
            )
        budgets[action] = ActionRateBudget(
            max_per_hour=int(max_per_hour) if max_per_hour is not None else None,
            min_interval_seconds=parse_duration(min_interval) if min_interval is not None else 0,
        )
    return budgets
