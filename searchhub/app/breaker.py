"""A per-provider circuit breaker for the search and fetch chains.

The ordinary rule is small on purpose: a provider that fails
:data:`DEFAULT_FAILURES` times *in a row* is skipped for
:data:`DEFAULT_COOLDOWN_S` seconds, so one dead upstream cannot make every
request pay its timeout. Then it is tried again.

A second, harsher rule exists for problems that a cooldown cannot fix:
:meth:`CircuitBreaker.record_hard_failure` disables a provider **until the
process restarts**. Only two causes earn that -- HTTP 402 ``no_credits`` (the
account has run out) and HTTP 401 ``unauthorized`` (the key is wrong) -- because
both need a human, and retrying them once a minute forever is noise, not
recovery. A 403 stays an ordinary failure: a per-query WAF block is not a dead
key.

Three outcomes are deliberately **not** breakage and must be filtered by the
caller before it ever reaches :meth:`CircuitBreaker.record_failure`:

* a 429 -- rate limiting is flow control, not a dead provider, and with four
  live keys a burst must not demote the leader for a minute;
* zero results -- a nonsense query must not disable a provider;
* a call truncated by *this request's* deadline rather than the provider's own
  timeout -- the request's budget is not evidence about the provider.

The clock is injected so the cooldown is testable without sleeping, and so the
app's fake clock in tests governs both the request deadline and the breaker.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_FAILURES: int = 3
"""Consecutive failures that open a breaker (Plan B spec, 2026-10-29)."""

DEFAULT_COOLDOWN_S: float = 60.0
"""How long an open breaker skips its provider (Plan B spec, 2026-10-29)."""

HARD_FAILURES: tuple[str, ...] = ("no_credits", "unauthorized")
"""Reasons that disable a provider until the process restarts.

``no_credits`` is HTTP 402, ``unauthorized`` is HTTP 401. Both are states of
the account or the key; a cooldown would only re-ask the same unanswerable
question.
"""


@dataclass
class CircuitBreaker:
    """Failures, open-until times and hard-disable reasons, per provider name.

    State per name:

    * **closed** -- :meth:`allow` is true, failures counted consecutively;
    * **open** -- :meth:`allow` is false until the cooldown has elapsed;
    * **half-open trial** -- after the cooldown :meth:`allow` is true again but
      the failure count is *not* reset, so one more failure re-opens the breaker
      immediately. A still-dead provider therefore costs one request per
      cooldown, not ``failures`` requests each time. :meth:`record_success`
      closes it fully;
    * **no_credits** / **unauthorized** -- a hard failure. :meth:`allow` is false
      for the life of the process: no cooldown, no trial, no transition back.
      Only a restart (or a human fixing the account) changes it.

    The trial needs no extra state: the count is already at the threshold when
    the cooldown expires, which is exactly what "one strike" means.
    """

    failures: int = DEFAULT_FAILURES
    cooldown_s: float = DEFAULT_COOLDOWN_S
    clock: Callable[[], float] = time.monotonic

    _consecutive: dict[str, int] = field(default_factory=dict, repr=False)
    _opened_at: dict[str, float] = field(default_factory=dict, repr=False)
    _hard: dict[str, str] = field(default_factory=dict, repr=False)

    def allow(self, name: str) -> bool:
        """True only while ``name`` is closed; hard failures are never allowed."""
        return self.state(name) == "closed"

    def record_success(self, name: str) -> None:
        """Zero the consecutive count and close the breaker.

        Does **not** clear a hard failure: the provider is not callable while it
        is hard-failed, so a success cannot legitimately arrive for one, and a
        stray call must not quietly re-enable it.
        """
        self._consecutive.pop(name, None)
        self._opened_at.pop(name, None)

    def record_failure(self, name: str) -> None:
        """Count a failure; open the breaker when the threshold is reached.

        At or above the threshold the open time is re-armed on every failure,
        which is what turns the first post-cooldown failure into a re-open.
        """
        count = self._consecutive.get(name, 0) + 1
        self._consecutive[name] = count
        if count >= self.failures:
            self._opened_at[name] = self.clock()

    def record_hard_failure(self, name: str, reason: str) -> None:
        """Disable ``name`` until the process restarts, with ``reason`` kept.

        ``reason`` is one of :data:`HARD_FAILURES`; anything else is a
        programming error, not a state to guess at.
        """
        if reason not in HARD_FAILURES:
            raise ValueError(f"unknown hard-failure reason {reason!r}")
        self._hard[name] = reason

    def state(self, name: str) -> str:
        """The state of ``name``: ``closed``, ``open``, or a hard failure reason."""
        hard = self._hard.get(name)
        if hard is not None:
            return hard
        opened_at = self._opened_at.get(name)
        if opened_at is not None and self.clock() - opened_at < self.cooldown_s:
            return "open"
        return "closed"
