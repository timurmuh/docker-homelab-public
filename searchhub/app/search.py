"""The search chain: four provider adapters and one ordered walk over them.

Each adapter is small and returns the same shape -- a list of
``{"url", "title", "description"}`` -- so :func:`run_search` never needs to know
which provider answered. Endpoints, headers and payload keys were verified
against each provider's own documentation on 2026-10-30; the table in
``searchhub/README.md`` records them, including the four details that are easy
to get wrong:

* Exa takes ``numResults``, and a bare search returns no snippet: the text only
  appears because the body asks for ``contents: {"highlights": true}``;
* Tavily moved the key from the body to ``Authorization: Bearer``;
* Brave wants a *plain* ``X-Subscription-Token`` (no ``Bearer``) and omits the
  ``web`` key entirely when it has nothing;
* SearXNG is reached through crw's Firecrawl-compatible endpoint, which rejects
  ``limit > 20`` with a 400, so :func:`run_search` clamps before any provider
  sees the number.

Advance rules, in one place because they are the policy of the service:

* first provider with at least one hit wins; the rest are not called;
* an error, a non-2xx or a broken body records a breaker failure and advances;
* **zero results** and **429** advance *without* touching the breaker: a
  nonsense query must not disable a provider, and rate limiting is flow
  control, not breakage. A 429 also marks the request ``rate_limited`` in the
  one log line, so the cause is visible;
* a call cut short by *this request's* deadline (rather than by the provider's
  own timeout) advances without a failure and marks the line
  ``budget_truncated``: the request's budget is not evidence about the provider;
* **401 and 402** are hard failures -- :meth:`CircuitBreaker.record_hard_failure`
  disables the provider until the process restarts, and `/health` reports the
  reason (``unauthorized`` / ``no_credits``) instead of a generic ``open``.

Pinned mode (``only``) is a diagnosis tool, so it tells the truth: if the one
provider that was asked raises, :class:`PinnedProviderError` is raised and the
route answers 502, rather than disguising the failure as the empty list a
successful no-results answer produces.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NoReturn

import httpx

from app.breaker import HARD_FAILURES, CircuitBreaker
from app.settings import DEFAULT_PROVIDER_TIMEOUT_S, Settings

logger = logging.getLogger(__name__)

DEFAULT_LIMIT: int = 8
"""Results per provider when the caller does not ask (Firecrawl's default)."""

MAX_LIMIT: int = 20
"""Hard ceiling for a provider call.

crw rejects ``limit > 20`` on ``/firecrawl/v2/search`` with a 400 (verified
2026-10-30), and a single walk calls every provider with the same number, so
the clamp has to be the lowest ceiling rather than a per-provider one.
"""

TRUNCATION_TOLERANCE_S: float = 0.05
"""Slack before a call counts as cut short by the request's own deadline.

A caller may ask for exactly ``provider_timeout_s`` of wall clock. By the time
the chain reads the clock to compute what is left, a few hundred microseconds
have passed, so a strict ``remaining < provider_timeout_s`` would classify every
such request as truncated and (pinned) answer 502 for a provider that had its
full budget. 50 ms is far above that jitter and far below any provider budget
that could matter, so truncation means *meaningfully* less than the provider's
own timeout, not a rounding error.
"""

PROVIDER_NAMES: tuple[str, ...] = ("exa", "tavily", "brave", "searxng")
"""Every search adapter this module knows, configured or not.

Distinct from ``Settings.search_order`` on purpose: the order names a
preference, this tuple names what the code can actually do. ``jina`` is absent
because it is a fetch provider, so ``?provider=jina`` is an unknown name here.
"""

EXA_URL: str = "https://api.exa.ai/search"
TAVILY_URL: str = "https://api.tavily.com/search"
BRAVE_URL: str = "https://api.search.brave.com/res/v1/web/search"

_TAG = re.compile(r"<[^>]+>")
"""Brave returns its snippets with inline markup such as ``<strong>``."""

_HARD_STATUS: dict[int, str] = {402: "no_credits", 401: "unauthorized"}
"""HTTP statuses that disable a provider until restart (see ``app.breaker``).

402 is the account's credit state, 401 is a wrong key: neither improves by
waiting 60 seconds, and both need a human. 403 is deliberately absent -- a
per-query WAF block is not a dead credential.
"""

Hit = dict[str, str]


@dataclass(frozen=True)
class SearchProvider:
    """One link in the chain: a name, whether it can run, and how to run it.

    ``enabled`` is false when the provider has no key. The provider keeps its
    place in the order either way -- the order is a preference, the key is the
    gate, and neither rewrites the other.
    """

    name: str
    enabled: bool
    run: Callable[[httpx.AsyncClient, str, int], Awaitable[list[Hit]]]


class PinnedProviderError(RuntimeError):
    """The one provider a ``?provider=`` request asked for failed to answer.

    Pinned mode exists so a human -- and, later, Plan C's eval harness -- can ask
    "is this provider working?". An upstream error dressed up as
    ``200 {"web": []}`` cannot answer that question, so it is raised instead and
    the route turns it into 502. ``reason`` is a status, a hard-failure reason,
    ``rate_limited`` or an exception class name: no key, no query, no upstream
    body ever reaches it.
    """

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"{name}: {reason}")
        self.name = name
        self.reason = reason


def _hit(url: object, title: object, description: object) -> Hit | None:
    """Normalise one provider entry, or drop it when it has no usable URL.

    A hit without a ``url`` is worthless to a caller and violates the response
    contract, so it is dropped here rather than repaired with a guess.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    return {
        "url": url.strip(),
        "title": title if isinstance(title, str) else "",
        "description": description if isinstance(description, str) else "",
    }


def _object_body(response: httpx.Response, provider: str) -> dict:
    """Decode a 2xx body, refusing anything that is not a JSON object.

    A dict with an unexpected shape is treated as *zero results* by the callers
    below (Brave's absent ``web`` is the documented case). A body that is not a
    dict at all -- or not JSON -- is a broken contract, and raises so the chain
    records a failure instead of silently reporting "no results" forever.
    """
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f"{provider}: response body is not a JSON object")
    return body


def _entries(container: object) -> list[dict]:
    """The list of result dicts at ``container``, or ``[]`` for any other shape."""
    if not isinstance(container, list):
        return []
    return [item for item in container if isinstance(item, dict)]


def _exa(settings: Settings) -> SearchProvider:
    key = settings.exa_api_key

    async def run(client: httpx.AsyncClient, query: str, limit: int) -> list[Hit]:
        response = await client.post(
            EXA_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "query": query,
                "numResults": limit,
                # Without this a bare search returns titles and URLs but no
                # snippet; highlights are the free text Exa returns.
                "contents": {"highlights": True},
            },
        )
        response.raise_for_status()
        hits = []
        for item in _entries(_object_body(response, "exa").get("results")):
            highlights = item.get("highlights")
            description = ""
            if isinstance(highlights, list) and highlights:
                description = highlights[0] if isinstance(highlights[0], str) else ""
            if not description:
                description = item.get("text") if isinstance(item.get("text"), str) else ""
            hit = _hit(item.get("url"), item.get("title"), description)
            if hit is not None:
                hits.append(hit)
        return hits

    return SearchProvider(name="exa", enabled=key is not None, run=run)


def _tavily(settings: Settings) -> SearchProvider:
    key = settings.tavily_api_key

    async def run(client: httpx.AsyncClient, query: str, limit: int) -> list[Hit]:
        response = await client.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "max_results": limit, "search_depth": "basic"},
        )
        response.raise_for_status()
        hits = []
        for item in _entries(_object_body(response, "tavily").get("results")):
            hit = _hit(item.get("url"), item.get("title"), item.get("content"))
            if hit is not None:
                hits.append(hit)
        return hits

    return SearchProvider(name="tavily", enabled=key is not None, run=run)


def _brave(settings: Settings) -> SearchProvider:
    key = settings.brave_api_key

    async def run(client: httpx.AsyncClient, query: str, limit: int) -> list[Hit]:
        response = await client.get(
            BRAVE_URL,
            # A plain token, not `Bearer`: Brave's own scheme differs from the
            # other two paid APIs, and a `Bearer` prefix is rejected.
            headers={"X-Subscription-Token": key or "", "Accept": "application/json"},
            params={"q": query, "count": limit, "text_decorations": "false"},
        )
        response.raise_for_status()
        body = _object_body(response, "brave")
        web = body.get("web")
        # Brave omits `web` entirely when it has no results; that is zero, not
        # an error.
        entries = _entries(web.get("results") if isinstance(web, dict) else None)
        hits = []
        for item in entries:
            description = item.get("description")
            if isinstance(description, str):
                description = html.unescape(_TAG.sub("", description))
            hit = _hit(item.get("url"), item.get("title"), description)
            if hit is not None:
                hits.append(hit)
        return hits

    return SearchProvider(name="brave", enabled=key is not None, run=run)


def _searxng(settings: Settings) -> SearchProvider:
    url = settings.crw_url.rstrip("/") + "/firecrawl/v2/search"

    async def run(client: httpx.AsyncClient, query: str, limit: int) -> list[Hit]:
        response = await client.post(url, json={"query": query, "limit": limit})
        response.raise_for_status()
        body = _object_body(response, "searxng")
        data = body.get("data")
        entries = _entries(data.get("web") if isinstance(data, dict) else None)
        hits = []
        for item in entries:
            hit = _hit(item.get("url"), item.get("title"), item.get("description"))
            if hit is not None:
                hits.append(hit)
        return hits

    # SearXNG needs no key: it is this homelab's own crw stack on search_net,
    # so it is always configured as far as the chain is concerned.
    return SearchProvider(name="searxng", enabled=True, run=run)


_FACTORIES: dict[str, Callable[[Settings], SearchProvider]] = {
    "exa": _exa,
    "tavily": _tavily,
    "brave": _brave,
    "searxng": _searxng,
}


def build_search_providers(settings: Settings) -> tuple[SearchProvider, ...]:
    """Build the chain in ``settings.search_order``, validating every name.

    Validation lives here and not at import: a test may construct any order,
    and a typo in ``SEARCH_ORDER`` must stop startup with the name in the
    message rather than silently falling back to the default order.
    """
    providers = []
    for name in settings.search_order:
        factory = _FACTORIES.get(name)
        if factory is None:
            known = ", ".join(PROVIDER_NAMES)
            raise ValueError(
                f"unknown search provider {name!r} in SEARCH_ORDER; known providers: {known}"
            )
        providers.append(factory(settings))
    return tuple(providers)


def _clamp_limit(limit: object) -> int:
    """Coerce ``limit`` into ``1..MAX_LIMIT``, defaulting rather than raising."""
    try:
        value = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


_PROVIDER_NAME_MAX: int = 40
"""Longest caller-supplied provider name echoed into a log line or a 400 body.

The name comes from the query string, so it is attacker-controlled: control
characters would let it forge extra log lines and any length at all would let it
flood them. 40 is far above the longest real name (``searxng``, 7) and far
below anything that could hide the rest of a line.
"""


def _safe_name(name: str) -> str:
    """Make a query-string provider name safe to echo in a log line and a body.

    Control characters (newlines above all) are dropped, then the result is
    capped. The raw name is still what the chain matches on; only the echoed
    copy is sanitised, so ``?provider=exa%0A`` cannot become a working pin.

    Every echo site also quotes the value with ``repr`` (``%r`` in the log line,
    ``!r`` in the message). Capping alone left ``?provider=exa hits=999``
    reading as a second log field; quoting makes it visibly one string.
    """
    printable = "".join(ch for ch in name if ch.isprintable())
    return printable[:_PROVIDER_NAME_MAX]


def _reject(provider: str, message: str) -> NoReturn:
    """Log a rejected ``?provider=`` and raise.

    The rejection is the whole reason the parameter exists, and with
    ``--no-access-log`` an unlogged 400 would leave no trace in production.
    ``provider`` is already sanitised by :func:`_select` and is quoted with
    ``%r`` here so a name containing spaces cannot read as extra fields;
    ``message`` names the provider; neither ever carries a key or a query.
    """
    logger.warning("search pin rejected provider=%r reason=%s", provider, message)
    raise ValueError(message)


def _select(
    providers: tuple[SearchProvider, ...], breaker: CircuitBreaker, only: str | None
) -> tuple[SearchProvider, ...]:
    """The links this request may call: the whole order, or one pinned link.

    A pinned name must exist, be in the order, be configured, and be neither
    open nor hard-failed. Each rejected case logs and raises
    :class:`ValueError` naming the provider; the route turns that into a 400 so
    a diagnosis request fails loudly instead of quietly answering from a
    different link.

    Matching uses the raw name; everything echoed (log line and 400 body) uses
    the sanitised :func:`_safe_name`.
    """
    if only is None:
        return tuple(p for p in providers if p.enabled and breaker.allow(p.name))

    shown = _safe_name(only)
    if only not in PROVIDER_NAMES:
        _reject(shown, f"unknown search provider: {shown!r}")
    provider = next((p for p in providers if p.name == only), None)
    if provider is None:
        _reject(shown, f"search provider {shown!r} is not in SEARCH_ORDER")
    if not provider.enabled:
        _reject(shown, f"search provider {shown!r} is not configured")
    state = breaker.state(only)
    if state in HARD_FAILURES:
        # Pinned to a provider that is disabled until restart: 400, not 502.
        # Nothing was asked upstream, so this is a rejection, not a failure.
        _reject(shown, f"search provider {shown!r} is unavailable: {state}")
    if state != "closed":
        _reject(shown, f"search provider {shown!r} is open (circuit breaker)")
    return (provider,)


async def run_search(
    client: httpx.AsyncClient,
    providers: tuple[SearchProvider, ...],
    breaker: CircuitBreaker,
    query: str,
    limit: int,
    deadline: float,
    only: str | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    provider_timeout_s: float = DEFAULT_PROVIDER_TIMEOUT_S,
) -> tuple[list[Hit], str | None]:
    """Walk the chain and return ``(hits, answering_provider)``.

    ``deadline`` is an absolute reading of ``clock``: the caller's ``timeout``
    in milliseconds, or ``search_budget_s``. Before each call the remaining
    time is checked, so an already-expired deadline makes no upstream call at
    all. Each call gets ``min(provider_timeout_s, remaining)``: the interface
    of ``SearchProvider.run`` takes no timeout, so the bound is enforced by
    cancelling the whole adapter call with :func:`asyncio.wait_for` (httpx's
    own client timeout remains the inner bound).

    A timeout counts as a provider failure only when the provider's own timeout
    was the binding constraint. If this request's deadline cut the call short,
    the provider never had its chance, so an unpinned chain advances without
    recording anything and the log line says ``budget_truncated``. "Cut short"
    means the remaining time is below the provider timeout by more than
    :data:`TRUNCATION_TOLERANCE_S`: a caller that asks for exactly
    ``provider_timeout_s`` must not be reclassified by the microseconds it took
    to get here.

    When ``only`` pins the chain and the pinned call raises,
    :class:`PinnedProviderError` propagates (the route answers 502). That
    includes a budget truncation (``budget_truncated``): a pinned call that did
    not finish has no answer to report, and ``200 []`` would look exactly like
    "found nothing". A pinned call that succeeds with zero hits still returns
    ``([], None)`` -- that is a real "no results" answer, not a failure.

    The one INFO line per request is emitted in ``finally`` -- naming the
    provider and the error for a pinned failure, and skipped entirely when the
    client has gone away (a cancelled request is not a finished search). It
    never contains a query or a key.
    """
    started = clock()
    size = _clamp_limit(limit)
    # Selecting before the walk keeps a rejected pin to its one WARNING line: a
    # 400 is not a search, and `provider=none hits=0` would read like an empty
    # result. A pinned *failure* happens inside the try below and is logged.
    chain = _select(providers, breaker, only)
    rate_limited = False
    budget_truncated = False
    hits: list[Hit] = []
    answering: str | None = None
    failure: PinnedProviderError | None = None
    cancelled = False
    attempted = False

    try:
        for provider in chain:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            budget = min(provider_timeout_s, remaining)
            if budget <= 0:
                break
            # True when this request's deadline, not the provider's own timeout,
            # bounds the call: such a timeout says nothing about the provider.
            # The tolerance keeps a caller that asks for exactly the provider
            # timeout from being reclassified by the microseconds of jitter
            # between computing the deadline and reading the clock here.
            budget_bound = remaining < provider_timeout_s - TRUNCATION_TOLERANCE_S
            attempted = True
            try:
                found = await asyncio.wait_for(
                    provider.run(client, query, size), timeout=budget
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                hard_reason = _HARD_STATUS.get(status)
                if hard_reason is not None:
                    # A credit or credential state will not fix itself in 60 s:
                    # disable the provider until the process restarts. Logged
                    # once here; later requests skip it silently.
                    breaker.record_hard_failure(provider.name, hard_reason)
                    logger.warning(
                        "search provider=%s disabled reason=%s until restart",
                        provider.name,
                        hard_reason,
                    )
                    if only is not None:
                        raise PinnedProviderError(provider.name, hard_reason) from exc
                elif status == 429:
                    # Flow control, not breakage: advance, do not record.
                    rate_limited = True
                    if only is not None:
                        raise PinnedProviderError(provider.name, "rate_limited") from exc
                else:
                    breaker.record_failure(provider.name)
                    logger.warning(
                        "search provider=%s failed status=%d", provider.name, status
                    )
                    if only is not None:
                        raise PinnedProviderError(provider.name, f"status {status}") from exc
            except TimeoutError as exc:
                if budget_bound:
                    # The request ran out of time, not the provider: never a
                    # breaker failure. Unpinned advances; pinned must not report
                    # an answer it does not have.
                    budget_truncated = True
                    if only is not None:
                        raise PinnedProviderError(provider.name, "budget_truncated") from exc
                else:
                    breaker.record_failure(provider.name)
                    logger.warning(
                        "search provider=%s failed error=TimeoutError", provider.name
                    )
                    if only is not None:
                        raise PinnedProviderError(provider.name, "timeout") from exc
            except Exception as exc:  # noqa: BLE001 - one bad provider must not sink the chain
                breaker.record_failure(provider.name)
                # Class name only: an exception message can embed a URL, and
                # this line must never be able to carry a key.
                logger.warning(
                    "search provider=%s failed error=%s",
                    provider.name,
                    type(exc).__name__,
                )
                if only is not None:
                    raise PinnedProviderError(provider.name, type(exc).__name__) from exc
            else:
                if found:
                    hits = found
                    answering = provider.name
                    breaker.record_success(provider.name)
                    break
                # Zero results: advance without touching the breaker, so a
                # nonsense query cannot disable a provider for the next caller.
        if only is not None and answering is None and not attempted:
            # The pinned provider was never called because the caller's own
            # deadline had already passed (`timeout: 0` or negative): the same
            # answer `/v2/scrape` gives, because 200 [] would read as a real
            # "no results" verdict on a provider that was never asked.
            raise PinnedProviderError(chain[0].name, "budget_truncated")
    except asyncio.CancelledError:
        # The caller disconnected. The request never finished, so the log must
        # not claim a search happened.
        cancelled = True
        raise
    except PinnedProviderError as exc:
        failure = exc
        raise
    finally:
        if not cancelled:
            elapsed_ms = int((clock() - started) * 1000)
            if failure is not None:
                # A 502 owes the log the same honesty the body has: the provider
                # and the reason, not `provider=none`.
                logger.info(
                    "search provider=%s hits=0 error=%s ms=%d",
                    failure.name,
                    failure.reason,
                    elapsed_ms,
                )
            else:
                suffix = ""
                if rate_limited:
                    suffix += " rate_limited"
                if budget_truncated:
                    suffix += " budget_truncated"
                logger.info(
                    "search provider=%s hits=%d ms=%d%s",
                    answering or "none",
                    len(hits),
                    elapsed_ms,
                    suffix,
                )
    return hits, answering
