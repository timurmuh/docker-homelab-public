"""The circuit breaker: three consecutive failures open it for one cooldown.

The clock is injected so the cooldown is tested by arithmetic, not by sleeping.
"""

import pytest

from app.breaker import CircuitBreaker


def test_opens_after_three_consecutive_failures_and_closes_after_cooldown():
    now = [1000.0]
    b = CircuitBreaker(clock=lambda: now[0])
    assert b.allow("exa")
    for _ in range(3):
        b.record_failure("exa")
    assert not b.allow("exa")
    now[0] += 61
    assert b.allow("exa")


def test_success_resets_the_failure_count():
    b = CircuitBreaker()
    b.record_failure("exa")
    b.record_failure("exa")
    b.record_success("exa")
    b.record_failure("exa")
    assert b.allow("exa")


def test_breakers_are_independent_per_provider():
    b = CircuitBreaker()
    for _ in range(3):
        b.record_failure("exa")
    assert not b.allow("exa")
    assert b.allow("tavily")


# -- beyond the brief -------------------------------------------------------


def test_state_reports_open_and_closed():
    now = [1000.0]
    b = CircuitBreaker(clock=lambda: now[0])
    assert b.state("exa") == "closed"
    for _ in range(3):
        b.record_failure("exa")
    assert b.state("exa") == "open"
    now[0] += 61
    assert b.state("exa") == "closed"


def test_one_failure_after_the_cooldown_reopens_the_trial():
    # The first call after the cooldown is a trial: a single failure is enough
    # to re-open, so a still-dead provider costs one request per minute, not
    # three. A success in the trial closes the breaker for good.
    now = [1000.0]
    b = CircuitBreaker(clock=lambda: now[0])
    for _ in range(3):
        b.record_failure("exa")
    now[0] += 61
    assert b.allow("exa")
    b.record_failure("exa")
    assert not b.allow("exa")


def test_a_success_in_the_trial_closes_the_breaker():
    now = [1000.0]
    b = CircuitBreaker(clock=lambda: now[0])
    for _ in range(3):
        b.record_failure("exa")
    now[0] += 61
    assert b.allow("exa")
    b.record_success("exa")
    assert b.state("exa") == "closed"
    b.record_failure("exa")
    assert b.allow("exa")


# -- hard failures: a credit or credential state, not a cooldown ------------


def test_a_hard_failure_survives_the_cooldown_and_never_retries():
    now = [1000.0]
    b = CircuitBreaker(clock=lambda: now[0])
    b.record_hard_failure("exa", "no_credits")
    assert b.state("exa") == "no_credits"
    assert not b.allow("exa")

    now[0] += 10_000  # far past any cooldown
    assert b.state("exa") == "no_credits"
    assert not b.allow("exa")


def test_a_hard_failure_is_per_provider():
    b = CircuitBreaker()
    b.record_hard_failure("exa", "unauthorized")
    assert b.state("exa") == "unauthorized"
    assert b.allow("tavily")


def test_a_success_does_not_clear_a_hard_failure():
    # A hard-failed provider is never called, so a success cannot legitimately
    # arrive for it; a stray one must not quietly re-enable it.
    b = CircuitBreaker()
    b.record_hard_failure("exa", "no_credits")
    b.record_success("exa")
    assert b.state("exa") == "no_credits"


def test_an_unknown_hard_failure_reason_is_refused():
    with pytest.raises(ValueError):
        CircuitBreaker().record_hard_failure("exa", "server_error")
