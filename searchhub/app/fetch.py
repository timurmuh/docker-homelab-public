"""The fetch chain: four readers, two rejection classes, one ordered walk.

Each adapter is small and returns the same shape -- a :class:`Fetched` -- so
:func:`run_fetch` never needs to know which reader answered. Endpoints and
payload keys were verified live on 2026-10-30 against the deployed stack; the
table in ``searchhub/README.md`` records them, including the four details that
are easy to get wrong:

* crw answers ``success: true`` with ``data.metadata.statusCode`` carrying the
  *page's* status, so an upstream 404 arrives inside a 200 envelope -- the
  envelope's ``success`` flag is never branched on;
* Jina's reader takes the target URL as its path, answers JSON only when asked
  (``Accept: application/json``), and refuses with 403/422 for a domain it will
  not read (``AbuseAlleviationError``): a normal fall-through, not a dead key;
* Tavily's extract answers ``200`` with ``results: []`` **and** a populated
  ``failed_results`` in the same body, so an empty ``results`` is a failure
  whether or not the failure list explains it;
* Exa's contents reports per-URL failure in ``statuses[]`` instead of the HTTP
  status, so a 200 can still mean "this URL did not work".

The rule this module exists for is the split between the two ways a candidate
can be rejected, because they behave differently once the chain is exhausted:

* **wall** -- :func:`app.botwall.looks_blocked` matched (by definition the text
  is under 1,000 characters), or the page's own title *is* a **phrase-level**
  mined wall signature (the bare brand token ``cloudflare`` is excluded -- see
  :data:`TITLE_SIGNATURES`). Either way it is the wall and nothing else: never
  returned once detected, not even as the best-effort answer. This is what stops
  a CAPTCHA being handed to a model as a page.
* **too short** -- no signature matched and the text is under
  ``min_content_chars`` (200). It is a real page that is merely small, so it is
  eligible as the best-effort answer when every provider fails.

:func:`run_fetch` returns the first accepted candidate; if none is accepted it
returns the longest *too-short* candidate; if every candidate was a wall it
returns ``None`` and the route answers 502. Length alone never decides: a
4,000-character article whose first line is "We value your privacy" is content
(it is over ``WALL_MAX_CHARS``, so ``looks_blocked`` cannot fire), while a
600-character "Attention Required! Cloudflare" page is a wall. A challenge page
whose body was inflated past that cap by inline JavaScript is caught by its
title instead (see ``_title_signature``).

Advance rules mirror ``app/search.py`` on purpose -- the two halves of the
service must behave the same way when a provider misbehaves:

* a transport error, a non-2xx HTTP response or a malformed body records a
  breaker failure and advances;
* **401 and 402** are hard failures (``unauthorized`` / ``no_credits``) and
  disable the provider until the process restarts;
* **429** advances *without* recording: rate limiting is flow control, not
  breakage;
* a call cut short by *this request's* deadline rather than the provider's own
  timeout advances without a failure (``budget_truncated``);
* a call cut short by *this service's* per-provider ceiling
  (``fetch_provider_timeout_s``, 30 s) also advances without a failure and marks
  the line ``cap_truncated``: that ceiling is our policy -- crw's worst measured
  page is 16.96 s -- and never a verdict on the reader's health;
* a **200 that fails either quality check** advances without touching the
  breaker. The provider is healthy, the page was not. A page-level 404 carried
  in crw's 200 envelope is in the same class: the reader worked, the URL did
  not, so it advances without a breaker failure and is never returned -- not
  even as the best-effort stub. Tavily's ``results: []`` and Exa's
  ``statuses[]`` error are the same class, which is why the adapters raise
  :class:`PageFailedError` instead of a plain ``ValueError``: a well-formed
  "this URL failed" must not demote a healthy provider. That matters doubly
  here because one breaker serves both chains, so a demoted Tavily would also
  disappear from search.

Pinned mode (``only``) is a diagnosis tool, so it tells the truth. Anything
other than an accepted answer raises :class:`app.search.PinnedProviderError`
and the route answers 502: a failed call for the same reasons search uses, a
wall as ``wall: <signature>``, and a too-short page as ``too short: <n>
chars``. A pinned caller is asking "can this reader return this page?", and
"no, it returned a CAPTCHA" is not an answer a 200 may disguise.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urlsplit

import httpx

from app.breaker import HARD_FAILURES, CircuitBreaker
from app.botwall import MIN_CONTENT_CHARS, SIGNATURES, looks_blocked

# Shared with the search chain, not copied: one set of hard-failure statuses,
# one truncation tolerance, one sanitiser for a caller-supplied provider name.
# Two copies of any of those would be two behaviours to keep in agreement.
from app.search import (
    TRUNCATION_TOLERANCE_S,
    PinnedProviderError,
    _HARD_STATUS,
    _safe_name,
)
from app.settings import DEFAULT_FETCH_PROVIDER_TIMEOUT_S, Settings

logger = logging.getLogger(__name__)

PROVIDER_NAMES: tuple[str, ...] = ("crw", "jina", "tavily", "exa")
"""Every fetch adapter this module knows, configured or not.

Distinct from ``Settings.fetch_order``: the order names a preference, this
tuple names what the code can do. ``brave`` is absent because it is a search
provider, so ``?provider=brave`` is an unknown name on this route.
"""

JINA_URL: str = "https://r.jina.ai"
TAVILY_URL: str = "https://api.tavily.com/extract"
EXA_URL: str = "https://api.exa.ai/contents"

WALL: str = "wall"
"""A candidate the signature detector matched: poisoned, never returned."""

SHORT: str = "too short"
"""A candidate under the length floor with no signature: usable best effort."""

OK: str = "ok"
"""A candidate that may be returned to the caller."""

class PageFailedError(ValueError):
    """A reader answered, but reported that the *URL* could not be fetched.

    Distinct from a malformed body on purpose, because the two must not be
    confused in the breaker. Tavily says ``200`` with ``results: []`` and a
    populated ``failed_results``; Exa says ``200`` with an ``error`` entry in
    ``statuses[]``. Both are well-formed answers about the page, not breakage of
    the provider -- exactly like crw's page-level 404. They advance the chain
    **without** recording a breaker failure.

    Live evidence for why this matters (2026-10-30): three unpinned scrapes of
    URLs the extract endpoint cannot read tripped Tavily's breaker, and because
    one breaker serves both chains the *search* chain lost a healthy provider
    for a minute. A body that is missing or the wrong shape still raises a plain
    ``ValueError`` and *is* a failure.
    """


@dataclass(frozen=True)
class Fetched:
    """One reader's answer: the markdown, the page status, and where it came from.

    ``status_code`` is the *page's* status where the provider reports one (crw's
    ``data.metadata.statusCode``) and 200 where it does not (the other three
    readers fail with an HTTP status instead of carrying the target's). It is
    what the response contract's ``metadata.statusCode`` reports.
    """

    markdown: str
    status_code: int
    source_url: str
    title: str
    provider: str


@dataclass(frozen=True)
class FetchProvider:
    """One link in the chain: a name, whether it can run, and how to run it.

    ``enabled`` is false when the provider has no key -- the same rule as the
    search chain. crw needs none and is always enabled. Jina's reader also works
    anonymously, but a blank ``JINA_API_KEY`` still disables it; if that is ever
    changed, the adapter already omits the ``Authorization`` header when there
    is no key. The provider keeps its place in the order either way -- the order
    is a preference, the key is the gate, and neither rewrites the other.
    """

    name: str
    enabled: bool
    run: Callable[[httpx.AsyncClient, str], Awaitable[Fetched]]


def _body(response: httpx.Response, provider: str) -> dict:
    """Decode a 2xx body, refusing anything that is not a JSON object.

    A body that is not a dict -- or not JSON at all -- is a broken contract, so
    it raises and the chain records a failure instead of mistaking it for an
    empty answer.
    """
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f"{provider}: response body is not a JSON object")
    return body


def _string(container: dict, key: str, provider: str, default: str = "") -> str:
    """``container[key]`` as text, or ``default`` when absent.

    Absent metadata is not a failure (titles are optional); a present but
    non-string value is, because that would mean the contract changed.
    """
    value = container.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{provider}: {key} is not a string")
    return value


def _required_text(container: dict, key: str, provider: str) -> str:
    """``container[key]`` must be text; an empty string is left to the quality checks.

    An empty page is a *short* page, not a transport failure: the reader
    answered, and the two-class split decides what may be done with it.
    """
    value = container.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{provider}: {key} is missing or not a string")
    return value


def _status_code(container: dict, provider: str) -> int:
    """The page status at ``container["statusCode"]``, defaulting to 200."""
    value = container.get("statusCode")
    if value is None:
        return 200
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{provider}: statusCode is not an integer")
    return value


def _crw(settings: Settings) -> FetchProvider:
    url = settings.crw_url.rstrip("/") + "/firecrawl/v2/scrape"

    async def run(client: httpx.AsyncClient, target: str) -> Fetched:
        response = await client.post(
            url,
            json={"url": target, "formats": ["markdown"], "onlyMainContent": True},
        )
        response.raise_for_status()
        data = _body(response, "crw").get("data")
        if not isinstance(data, dict):
            # crw answers `success: true` even for an upstream 404, so the only
            # broken case here is a missing/short envelope. The `success` flag
            # itself is never consulted: a 200-with-no-data and a
            # `success: false` both land here, and both advance the chain.
            raise ValueError("crw: response has no data object")
        metadata = data.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        source = _string(metadata, "sourceURL", "crw")
        return Fetched(
            markdown=_required_text(data, "markdown", "crw"),
            status_code=_status_code(metadata, "crw"),
            source_url=source or target,
            title=_string(metadata, "title", "crw"),
            provider="crw",
        )

    # crw is this homelab's own reader on search_net: no key, always configured.
    return FetchProvider(name="crw", enabled=True, run=run)


def _jina(settings: Settings) -> FetchProvider:
    key = settings.jina_api_key

    async def run(client: httpx.AsyncClient, target: str) -> Fetched:
        headers = {"Accept": "application/json", "X-Return-Format": "markdown"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        # The target URL is the path, unslashed: https://r.jina.ai/<target>.
        response = await client.get(f"{JINA_URL}/{target}", headers=headers)
        response.raise_for_status()
        data = _body(response, "jina").get("data")
        if not isinstance(data, dict):
            # Jina's failure envelope is `{"data": null, "code": …, "name": …}`;
            # with a 2xx it is still not an answer.
            raise ValueError("jina: response has no data object")
        return Fetched(
            markdown=_required_text(data, "content", "jina"),
            status_code=_status_code(data, "jina"),
            source_url=_string(data, "url", "jina", target),
            title=_string(data, "title", "jina"),
            provider="jina",
        )

    return FetchProvider(name="jina", enabled=key is not None, run=run)


def _tavily(settings: Settings) -> FetchProvider:
    key = settings.tavily_api_key

    async def run(client: httpx.AsyncClient, target: str) -> Fetched:
        response = await client.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"urls": [target], "format": "markdown"},
        )
        response.raise_for_status()
        body = _body(response, "tavily")
        results = body.get("results")
        if not isinstance(results, list):
            # A contract change ({"detail": ...}, a string, a missing key) is a
            # malformed body, not a page-level failure: it records a breaker
            # failure, so a provider that stays broken shows up in /health.
            raise ValueError("tavily: results is missing or not a list")
        if not results:
            # 200 + `results: []` + a populated `failed_results` is the normal
            # shape of an extract that failed *for this URL*; both are a
            # page-level failure, not a provider failure.
            raise PageFailedError("tavily: extract returned no results")
        item = results[0]
        if not isinstance(item, dict):
            raise ValueError("tavily: result is not an object")
        return Fetched(
            markdown=_required_text(item, "raw_content", "tavily"),
            status_code=200,
            source_url=_string(item, "url", "tavily", target),
            title=_string(item, "title", "tavily"),
            provider="tavily",
        )

    return FetchProvider(name="tavily", enabled=key is not None, run=run)


def _exa(settings: Settings) -> FetchProvider:
    key = settings.exa_api_key

    async def run(client: httpx.AsyncClient, target: str) -> Fetched:
        response = await client.post(
            EXA_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"urls": [target], "text": True},
        )
        response.raise_for_status()
        body = _body(response, "exa")
        statuses = body.get("statuses")
        if isinstance(statuses, list):
            for entry in statuses:
                if isinstance(entry, dict) and entry.get("status") not in (None, "success"):
                    # A per-URL failure arrives as 200 with an error status here,
                    # never as an HTTP status: page-level, so no breaker failure.
                    raise PageFailedError("exa: contents reported a per-URL failure")
        results = body.get("results")
        if not isinstance(results, list):
            # Contract change, not a page verdict: records a breaker failure.
            raise ValueError("exa: results is missing or not a list")
        if not results:
            raise PageFailedError("exa: contents returned no results")
        item = results[0]
        if not isinstance(item, dict):
            raise ValueError("exa: result is not an object")
        return Fetched(
            markdown=_required_text(item, "text", "exa"),
            status_code=200,
            source_url=_string(item, "url", "exa", target),
            title=_string(item, "title", "exa"),
            provider="exa",
        )

    return FetchProvider(name="exa", enabled=key is not None, run=run)


_FACTORIES: dict[str, Callable[[Settings], FetchProvider]] = {
    "crw": _crw,
    "jina": _jina,
    "tavily": _tavily,
    "exa": _exa,
}


def build_fetch_providers(settings: Settings) -> tuple[FetchProvider, ...]:
    """Build the chain in ``settings.fetch_order``, validating every name.

    Validation lives here and not at import: a test may construct any order, and
    a typo in ``FETCH_ORDER`` must stop startup with the name in the message
    rather than silently falling back to the default order.

    The per-provider ceiling is not baked into the providers: one value
    (``Settings.fetch_provider_timeout_s``) applies to the whole chain and
    :func:`run_fetch` enforces it, so a test can lower it without rebuilding the
    chain. The shared client's own timeout is the larger of the two chain
    ceilings (see ``app.main.create_app``), which keeps the asyncio cap the
    binding one and its classification meaningful.
    """
    providers = []
    for name in settings.fetch_order:
        factory = _FACTORIES.get(name)
        if factory is None:
            known = ", ".join(PROVIDER_NAMES)
            raise ValueError(
                f"unknown fetch provider {name!r} in FETCH_ORDER; known providers: {known}"
            )
        providers.append(factory(settings))
    return tuple(providers)


def _reject(provider: str, message: str) -> NoReturn:
    """Log a rejected ``?provider=`` and raise.

    With ``--no-access-log`` an unlogged 400 would leave no trace in production,
    so the rejection is the whole reason the parameter exists. ``provider`` is
    already sanitised by :func:`_select` and is quoted with ``%r`` here so a name
    containing spaces cannot read as extra fields; ``message`` names the
    provider; neither ever carries a key or a URL.
    """
    logger.warning("scrape pin rejected provider=%r reason=%s", provider, message)
    raise ValueError(message)


def _select(
    providers: tuple[FetchProvider, ...], breaker: CircuitBreaker, only: str | None
) -> tuple[FetchProvider, ...]:
    """The links this request may call: the whole order, or one pinned link.

    A pinned name must exist, be in the order, be configured, and be neither
    open nor hard-failed. Each rejected case logs and raises
    :class:`ValueError` naming the provider; the route turns that into a 400 so
    a diagnosis request fails loudly instead of quietly answering from a
    different link. Matching uses the raw name; everything echoed uses the
    sanitised one.
    """
    if only is None:
        return tuple(p for p in providers if p.enabled and breaker.allow(p.name))

    shown = _safe_name(only)
    if only not in PROVIDER_NAMES:
        _reject(shown, f"unknown fetch provider: {shown!r}")
    provider = next((p for p in providers if p.name == only), None)
    if provider is None:
        _reject(shown, f"fetch provider {shown!r} is not in FETCH_ORDER")
    if not provider.enabled:
        _reject(shown, f"fetch provider {shown!r} is not configured")
    state = breaker.state(only)
    if state in HARD_FAILURES:
        # Nothing was asked upstream, so this is a rejection, not a failure.
        _reject(shown, f"fetch provider {shown!r} is unavailable: {state}")
    if state != "closed":
        _reject(shown, f"fetch provider {shown!r} is open (circuit breaker)")
    return (provider,)


TITLE_SIGNATURES: tuple[str, ...] = tuple(s for s in SIGNATURES if s != "cloudflare")
"""The signatures a page *title* may be compared against.

Everything in :data:`app.botwall.SIGNATURES` except the bare brand token
``cloudflare``. Task 1 says that entry is a membership-only token -- it is in
the list so a short page carrying it is caught, and its proper fix is a
compound rule -- and a title is exactly where membership is *not* evidence:
Jina returns bare page titles (``SearXNG``, not ``SearXNG - Wikipedia``), so
``https://en.wikipedia.org/wiki/Cloudflare`` is titled exactly ``Cloudflare``.
That article is content. A phrase-level signature is different: no article is
titled exactly "Just a moment..." or "Prove your humanity".
"""


def _title_signature(title: str) -> str | None:
    """The phrase-level wall signature ``title`` *is*, or ``None``.

    ``looks_blocked`` is a substring search over page *text* with a 1,000
    character cap, because a long article may mention a wall phrase somewhere.
    A page **title** is different evidence: it is short by construction, and a
    page whose whole title is a *phrase-level* wall marker is a wall page
    however much body it carries.

    The length cap alone is not enough in the wild. In a live capture, Jina
    rendered a publisher's challenge as a **112,874-character** body --
    the CAPTCHA text plus the site's entire inline JavaScript -- titled exactly
    ``Just a moment...``. The text is far over ``WALL_MAX_CHARS``, so
    :func:`looks_blocked` correctly refuses to scan it; the title is what gives
    the page away. Without this check the chain hands that dump to the model.

    Matching is **exact** after normalising case, whitespace and trailing
    punctuation, and against :data:`TITLE_SIGNATURES` rather than the whole
    list: the brand token ``cloudflare`` is excluded, so a real article titled
    exactly ``Cloudflare`` stays content. Substring matching is never used --
    "Cloudflare outage takes down half the web" is content too.
    """
    if not title:
        return None
    normalised = " ".join(title.split()).strip(" .…!:|").lower()
    return normalised if normalised in TITLE_SIGNATURES else None


def _classify(fetched: Fetched, min_content_chars: int) -> tuple[str, str | None]:
    """Sort one candidate into :data:`OK`, :data:`WALL` or :data:`SHORT`.

    The order is load-bearing. A wall is *defined* by the detector and is
    excluded before length could make it look like a usable stub: the signature
    is looked for in the markdown first (short pages only, per
    :func:`app.botwall.looks_blocked`) and then in the page's own title against
    the phrase-level :data:`TITLE_SIGNATURES`, which catches a challenge page
    whose body was inflated past the text cap by inline JavaScript. A blank body
    is short whatever its length, so it can never be a best-effort answer. A
    4,000-character article that mentions a wall phrase is content, because
    ``looks_blocked`` refuses to scan at or above ``WALL_MAX_CHARS`` and its
    title is not a phrase-level signature.
    """
    signature = looks_blocked(fetched.markdown) or _title_signature(fetched.title)
    if signature is not None:
        return WALL, signature
    if not fetched.markdown.strip():
        return SHORT, None
    if len(fetched.markdown) < min_content_chars:
        return SHORT, None
    return OK, None


async def run_fetch(
    client: httpx.AsyncClient,
    providers: tuple[FetchProvider, ...],
    breaker: CircuitBreaker,
    url: str,
    deadline: float,
    only: str | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    provider_timeout_s: float = DEFAULT_FETCH_PROVIDER_TIMEOUT_S,
    min_content_chars: int = MIN_CONTENT_CHARS,
) -> tuple[Fetched | None, list[str]]:
    """Walk the chain and return ``(winner, tried_provider_names)``.

    ``winner`` is the first accepted candidate -- transport succeeded, page
    status under 400, no wall signature, at least ``min_content_chars``
    characters; if none is accepted, the longest **too-short** candidate (a
    150-character stub of a real page beats nothing); if every candidate was a
    wall or a page-level error, ``None`` -- that is the case the route must
    answer 502, because returning a poisoned page or a 404 is the one thing this
    chain must never do.

    ``deadline`` is an absolute reading of ``clock``: the caller's ``timeout`` in
    milliseconds, or ``scrape_budget_s``. Before each call the remaining time is
    checked, so an already-expired deadline makes no upstream call at all. Each
    call gets ``min(provider_timeout_s, remaining)``, enforced by cancelling the
    whole adapter call with :func:`asyncio.wait_for`.

    Neither kind of timeout is a provider failure. A call cut short by this
    request's deadline advances without recording anything and marks the line
    ``budget_truncated``; one cut short by this service's own per-provider
    ceiling (``provider_timeout_s``, 30 s in production) advances and marks it
    ``cap_truncated``. The ceiling is our policy rather than a verdict on the
    reader, so neither opens the breaker.

    When ``only`` pins the chain, anything but an accepted answer raises
    :class:`PinnedProviderError` -- a failed call with the status or reason, a
    wall as ``wall: <signature>``, a short page as ``too short: <n> chars``.
    The route answers 502, so a pinned diagnosis cannot mistake a CAPTCHA or a
    truncated stub for content.

    ``tried`` names the providers that were actually called, in order. It is
    empty when the deadline had already passed.
    """
    started = clock()
    # Selecting before the walk keeps a rejected pin to its one WARNING line: a
    # 400 is not a scrape, and `provider=none` would read like a failed fetch.
    chain = _select(providers, breaker, only)
    tried: list[str] = []
    best: Fetched | None = None
    winner: Fetched | None = None
    first_wall: str | None = None
    inspected_chars = 0
    rate_limited = False
    budget_truncated = False
    cap_truncated = False
    failure: PinnedProviderError | None = None
    cancelled = False

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
            budget_bound = remaining < provider_timeout_s - TRUNCATION_TOLERANCE_S
            tried.append(provider.name)
            try:
                candidate = await asyncio.wait_for(
                    provider.run(client, url), timeout=budget
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                hard_reason = _HARD_STATUS.get(status)
                if hard_reason is not None:
                    breaker.record_hard_failure(provider.name, hard_reason)
                    logger.warning(
                        "scrape provider=%s disabled reason=%s until restart",
                        provider.name,
                        hard_reason,
                    )
                    if only is not None:
                        raise PinnedProviderError(provider.name, hard_reason) from exc
                elif status == 429:
                    # Flow control, not breakage: advance, do not record. The
                    # flag becomes a suffix on the one INFO line, exactly as in
                    # the search chain.
                    rate_limited = True
                    if only is not None:
                        raise PinnedProviderError(provider.name, "rate_limited") from exc
                else:
                    breaker.record_failure(provider.name)
                    logger.warning(
                        "scrape provider=%s failed status=%d", provider.name, status
                    )
                    if only is not None:
                        raise PinnedProviderError(provider.name, f"status {status}") from exc
            except TimeoutError as exc:
                if budget_bound:
                    # The request ran out of time, not the provider: never a
                    # breaker failure. Unpinned advances; pinned has no answer.
                    budget_truncated = True
                    if only is not None:
                        raise PinnedProviderError(provider.name, "budget_truncated") from exc
                else:
                    # Our own per-provider ceiling fired (``provider_timeout_s``,
                    # 30 s for this chain). That ceiling is this service's policy
                    # -- crw's worst measured page is 16.96 s -- so a call cut
                    # short by it is not evidence that the reader is broken, and
                    # must not open the breaker or disappear from /health. The
                    # chain still advances and the line says `cap_truncated`.
                    cap_truncated = True
                    if only is not None:
                        raise PinnedProviderError(provider.name, "cap_truncated") from exc
            except PageFailedError as exc:
                # The reader answered, but said the URL could not be fetched.
                # Well-formed answer, page-level failure: advance without
                # recording, exactly like crw's page-level 404.
                logger.warning(
                    "scrape provider=%s rejected page-failed", provider.name
                )
                if only is not None:
                    raise PinnedProviderError(provider.name, "no content") from exc
            except Exception as exc:  # noqa: BLE001 - one bad reader must not sink the chain
                breaker.record_failure(provider.name)
                # Class name only: an exception message can embed a URL, and
                # this line must never be able to carry a key.
                logger.warning(
                    "scrape provider=%s failed error=%s",
                    provider.name,
                    type(exc).__name__,
                )
                if only is not None:
                    raise PinnedProviderError(provider.name, type(exc).__name__) from exc
            else:
                verdict, signature = _classify(candidate, min_content_chars)
                inspected_chars = len(candidate.markdown)
                if verdict == WALL:
                    # Remember the first wall for the log line: it is evidence
                    # that the detector fired even when a later reader answers.
                    first_wall = first_wall or signature
                    logger.warning(
                        "scrape provider=%s rejected wall=%s chars=%d",
                        provider.name,
                        signature,
                        inspected_chars,
                    )
                    if only is not None:
                        raise PinnedProviderError(
                            provider.name, f"wall: {signature}"
                        )
                elif candidate.status_code >= 400:
                    # A page-level failure carried inside a 200 envelope (crw's
                    # `data.metadata.statusCode`): the reader worked, the URL did
                    # not. Advance without a breaker failure, and never hand a
                    # 404 body to a caller -- not as content and not as the
                    # best-effort stub either.
                    logger.warning(
                        "scrape provider=%s rejected status=%d chars=%d",
                        provider.name,
                        candidate.status_code,
                        inspected_chars,
                    )
                    if only is not None:
                        raise PinnedProviderError(
                            provider.name, f"status {candidate.status_code}"
                        )
                elif verdict == OK:
                    winner = candidate
                    # Only an answer the caller may actually receive resets the
                    # provider's count; a page-level rejection says nothing about
                    # the reader's health, so it neither resets nor increments.
                    breaker.record_success(provider.name)
                    break
                else:
                    logger.warning(
                        "scrape provider=%s rejected too-short chars=%d",
                        provider.name,
                        inspected_chars,
                    )
                    # A blank body is never a usable stub, however long it is:
                    # the route would otherwise answer 200 with `markdown: ""`,
                    # which Open WebUI swallows.
                    if candidate.markdown.strip() and (
                        best is None or len(candidate.markdown) > len(best.markdown)
                    ):
                        best = candidate
                    if only is not None:
                        raise PinnedProviderError(
                            provider.name, f"too short: {inspected_chars} chars"
                        )
        if only is not None and winner is None and best is None and not tried:
            # The pinned reader was never called because the caller's own
            # deadline had already passed (`timeout: 0` or negative). Nothing
            # failed -- there was no time -- so say `budget_truncated` rather
            # than letting the route answer the generic "nothing fetched" 502.
            raise PinnedProviderError(chain[0].name, "budget_truncated")
    except asyncio.CancelledError:
        # The caller disconnected. The request never finished, so the log must
        # not claim a scrape happened.
        cancelled = True
        raise
    except PinnedProviderError as exc:
        failure = exc
        raise
    finally:
        if not cancelled:
            elapsed_ms = int((clock() - started) * 1000)
            wall = first_wall or "none"
            if failure is not None:
                # A 502 owes the log the same honesty the body has: the provider,
                # the wall (if one was seen) and the reason, not `provider=none`.
                logger.info(
                    "scrape provider=%s wall=%s chars=%d error=%s ms=%d",
                    failure.name,
                    wall,
                    inspected_chars,
                    failure.reason,
                    elapsed_ms,
                )
            else:
                if winner is not None:
                    chars = len(winner.markdown)
                elif best is not None:
                    chars = len(best.markdown)
                else:
                    chars = inspected_chars
                suffix = " best_effort" if winner is None and best is not None else ""
                if rate_limited:
                    suffix += " rate_limited"
                if budget_truncated:
                    suffix += " budget_truncated"
                if cap_truncated:
                    suffix += " cap_truncated"
                # A best-effort stub is still a provider's answer, so the line
                # names that provider: `provider=crw ... best_effort` says which
                # reader produced the text the caller received.
                if winner is not None:
                    shown = winner.provider
                elif best is not None:
                    shown = best.provider
                else:
                    shown = "none"
                logger.info(
                    "scrape provider=%s wall=%s chars=%d ms=%d%s",
                    shown,
                    wall,
                    chars,
                    elapsed_ms,
                    suffix,
                )

    return (winner if winner is not None else best), tried


_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")
"""Only real web URLs reach a reader; ``file:``, ``ftp:`` and ``data:`` do not."""


def validate_url(url: str) -> str:
    """Return ``url`` if a reader may be asked for it, else raise ``ValueError``.

    Two checks, both local:

    * the scheme is ``http`` or ``https`` -- a reader has no business with
      ``file:///etc/passwd`` or ``ftp://``;
    * a literal address host is not private, loopback, link-local, reserved,
      multicast or unspecified, and the loopback *names* are refused. This is
      what keeps ``http://169.254.169.254/latest/meta-data/`` (the cloud
      metadata endpoint) and the container network away from a reader.

    Two spelling traps are arithmetic, not DNS, and are handled here because
    each one would otherwise slip past the comparison:

    * a trailing dot is a legal FQDN form, so ``127.0.0.1.`` and ``localhost.``
      are the same hosts as without it -- the host is normalised by stripping
      it before either check;
    * a bare decimal such as ``2130706433`` *is* ``127.0.0.1`` in the legacy
      inet_aton spelling every resolver still accepts -- an all-digit host is
      converted to an integer and checked as an IPv4 address (only the 32-bit
      range; a longer number is not an IPv4 literal and is left to the
      resolver, i.e. to crw).

    **No DNS resolution.** A public *name* that resolves to a private address is
    crw's job to refuse, and resolving here would let any caller make this
    service perform DNS work before a fetch -- and would make the verdict depend
    on the resolver, so a hostname could pass validation and then resolve
    differently for the reader.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url is required")
    candidate = url.strip()
    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError("url scheme must be http or https")
    raw_host = parts.hostname
    if not raw_host:
        raise ValueError("url has no host")
    # A trailing dot is the same FQDN: "127.0.0.1." must not slip past the
    # literal check, and "localhost." must not slip past the name check.
    host = raw_host.rstrip(".").lower()
    if not host:
        raise ValueError("url has no host")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("url host is a loopback name")
    try:
        if host.isdigit():
            # Legacy inet_aton spelling: 2130706433 == 127.0.0.1. IPv4Address
            # takes the integer (and rejects anything past 32 bits), so the
            # private/loopback test below applies to the real address.
            address = ipaddress.IPv4Address(int(host))
        else:
            address = ipaddress.ip_address(host)
    except ValueError:
        # A name, not a literal: not this function's business to resolve.
        return candidate
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError("url host is a private, loopback or link-local address")
    return candidate
