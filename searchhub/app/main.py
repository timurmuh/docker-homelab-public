"""The searchhub service: one FastAPI app, built from :class:`Settings`.

``searchhub`` is a Firecrawl-v2-shaped proxy: Open WebUI and opencode call it,
and it fans out to crw/SearXNG and four paid search providers, plus a fetch
chain of four readers. This module owns the parts every route shares --
configuration, the shared HTTP client, the breaker, and the bearer check --
plus the routes: ``/v2/search`` (chain in ``app/search.py``), ``/v2/scrape``
(chain in ``app/fetch.py``), and the crw passthrough (``/v2/map``,
``/v2/crawl``) defined here below.

The app is built by :func:`create_app`, not at import time. That is deliberate:

* tests inject an ``httpx.MockTransport`` and a fake clock;
* importing this module must not require ``SEARCHHUB_API_KEY``, so the tests can
  import ``create_app`` without a secret in the environment (Task 2).

uvicorn therefore runs the factory, not a module-level app::

    uvicorn app.main:create_production_app --factory --host 0.0.0.0 --port 8080

``create_production_app`` reads the environment and fails to start if the token
is missing, which is the behaviour we want in a container and the opposite of
what we want at import time in a test run.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.breaker import CircuitBreaker
from app.fetch import (
    FetchProvider,
    build_fetch_providers,
    run_fetch,
    validate_url,
)
from app.search import (
    DEFAULT_LIMIT,
    PinnedProviderError,
    SearchProvider,
    build_search_providers,
    run_search,
)
from app.settings import Settings, load_settings

VERSION = "0.1.0"

HEALTH_PATH = "/health"
"""The one path served without a token: the container healthcheck calls it."""

CRW_MAP_PATH = "/firecrawl/v2/map"
"""crw's map face. ``/firecrawl/v2/...`` is its primary Firecrawl-v2 prefix."""

CRW_CRAWL_PATH = "/firecrawl/v2/crawl"
"""crw's crawl-start face, the same prefix."""

CRW_CRAWL_JOB_PATH = "/v2/crawl"
"""crw's *deprecated* per-job path, used by both status and cancel, on purpose.

crw 0.39.1 serves a job at both ``/firecrawl/v2/crawl/{id}`` and this alias,
with identical bodies (checked live 2026-10-30, GET and DELETE), and its own
crawl-start response hands back the alias in ``url``. A client that polls
``/v2/crawl/{id}`` is therefore forwarded to the same path shape on crw rather
than "fixed" to the ``/firecrawl/`` form: the alias is the one crw advertises,
and if crw ever drops it this constant is the single place to change.

It has "job" in the name, not "status", because GET and DELETE share it: a
future editor grepping for where a cancel goes must land here too.
"""

JOB_ID_PATTERN = re.compile(r"[0-9a-fA-F-]{8,64}")
"""What may be interpolated into a crw job path: a UUID-ish id, nothing else.

The shape matters for more than tidiness. ``urllib.parse.quote`` leaves ``.``
unencoded (it is in the always-safe set), and httpx then normalises dot
segments when it parses the outgoing URL, so an unvalidated ``%2E%2E`` or
``%2E`` would turn ``GET /v2/crawl/{id}`` into ``GET /v2`` and
``DELETE /v2/crawl/{id}`` into ``DELETE /v2/crawl`` -- crw's collection. A
non-request must not come back as a 200, and a per-job cancel must not become a
collection-wide one. crw's ids are UUIDs, so the set of ids this rejects is
empty in practice and every rejection is nonsense.
"""

logger = logging.getLogger("app.proxy")
"""The passthrough's one-line-per-request log, never carrying a URL or token."""


def _bearer_matches(authorization: str | None, api_key: str) -> bool:
    """True only for ``Authorization: Bearer <api_key>`` with an exact token.

    Compared with :func:`hmac.compare_digest` so the check does not leak the
    token's length or the position of the first wrong byte through timing.
    """
    if not authorization:
        return False
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(credentials.strip().encode(), api_key.encode())


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"detail": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _error(message: str) -> JSONResponse:
    """A 400 in the shape the rest of the API speaks, never echoing a secret."""
    return JSONResponse({"success": False, "error": message}, status_code=400)


def _upstream_failed(message: str) -> JSONResponse:
    """A 502 in the same shape: crw is down or broke, which is not an answer.

    Used only for a 5xx, a timeout or a transport error. A crw 4xx is a verdict
    about the caller's request and is passed through with crw's own body, so the
    two are never collapsed into one status.
    """
    return JSONResponse({"success": False, "error": message}, status_code=502)


def _provider_states(
    providers: tuple[SearchProvider, ...] | tuple[FetchProvider, ...],
    breaker: CircuitBreaker,
) -> list[dict[str, str]]:
    """``/health``'s view of a chain: name and state, never a key.

    States are the breaker's own words: ``ok`` for a closed breaker, ``open``
    for the cooldown, and the hard-failure reason (``no_credits`` /
    ``unauthorized``) when a provider is disabled until restart. A keyless
    provider is ``unconfigured``, not ``open``: it is skipped by design, and the
    three are different operator actions.
    """
    states = []
    for provider in providers:
        if not provider.enabled:
            state = "unconfigured"
        else:
            state = breaker.state(provider.name)
            if state == "closed":
                state = "ok"
        states.append({"name": provider.name, "state": state})
    return states


def _configure_logging() -> None:
    """Make the per-request INFO lines visible under uvicorn's log config.

    uvicorn configures its own loggers and leaves the root logger at its
    default, so a library logger at INFO is discarded by the last-resort
    handler (WARNING and above only). One handler per service logger is enough;
    ``propagate = False`` keeps a configured root from printing a line twice.
    Called only from :func:`create_production_app`, so tests keep capturing
    through pytest's root handler. All three service loggers are configured
    here: a proxy line is exactly as load-bearing as a search or fetch line.
    """
    for name in ("app.search", "app.fetch", "app.proxy"):
        service_logger = logging.getLogger(name)
        if service_logger.handlers:
            continue
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        service_logger.addHandler(handler)
        service_logger.setLevel(logging.INFO)
        service_logger.propagate = False


def create_app(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the app for ``settings``.

    ``transport`` is ``None`` in production (httpx's own transport) and an
    ``httpx.MockTransport`` in tests. ``clock`` is :func:`time.monotonic` in
    production and a fake in tests; later tasks spend their budgets against it.
    """
    if not settings.api_key:
        raise RuntimeError("SEARCHHUB_API_KEY is required")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            # `finally` matters: an exception escaping the server loop must not
            # leave the pooled connections open.
            await app.state.client.aclose()

    app = FastAPI(title="searchhub", version=VERSION, lifespan=lifespan)

    app.state.settings = settings
    app.state.clock = clock
    # One client for the app's whole life: connection pooling is the point, and
    # per-request clients would add a TLS handshake to every provider call.
    #
    # The client's timeout is the *larger* of the two chain ceilings, because it
    # is only the inner bound: each chain cancels a call itself at its own
    # ceiling (`asyncio.wait_for`) and classifies why. Leaving the client at the
    # search-sized 15 s would make httpx fire first for a fetch, turning a
    # legitimate 17-30 s crw render into a transport error -- which the chain
    # would record as a provider failure.
    app.state.client = httpx.AsyncClient(
        timeout=max(settings.provider_timeout_s, settings.fetch_provider_timeout_s),
        transport=transport,
    )
    # The search chain is built once, here, so a typo in SEARCH_ORDER stops the
    # process at startup (ValueError naming it) instead of answering from a
    # silently different order. A keyless provider stays in the tuple, disabled.
    # `/health` reads this tuple and the breaker live, so it shows the order the
    # chain actually uses and an "open" breaker without a restart.
    search_providers = build_search_providers(settings)
    app.state.search_providers = search_providers
    # The fetch chain is built the same way, for the same reason: a typo in
    # FETCH_ORDER stops the process at startup instead of answering from a
    # silently different order. One breaker serves both chains -- they share
    # provider names (exa, tavily) and hit the same accounts, so a 402
    # discovered by a search disables that account's fetches too, which is the
    # honest reading of a credit state.
    fetch_providers = build_fetch_providers(settings)
    app.state.fetch_providers = fetch_providers
    # One breaker for the app's life: it is the memory that stops a dead
    # upstream from costing every request its timeout. Same clock as the
    # budgets, so the cooldown is testable with a fake.
    app.state.breaker = CircuitBreaker(clock=clock)

    @app.middleware("http")
    async def require_bearer(request: Request, call_next) -> Response:
        # Auth is middleware, not a per-route dependency, so a route added in a
        # later task cannot forget its lock, and an unauthenticated caller
        # cannot map the surface: an unknown path is 401 here, only 404 once
        # authenticated.
        if request.url.path == HEALTH_PATH:
            return await call_next(request)
        if not _bearer_matches(request.headers.get("Authorization"), settings.api_key):
            return _unauthorized()
        return await call_next(request)

    @app.get(HEALTH_PATH)
    async def health() -> dict[str, Any]:
        # Unauthenticated on purpose, so it must never echo a configuration
        # value: provider names and states only, never a key. The state is read
        # live, so an open breaker shows up without a restart.
        return {
            "status": "ok",
            "version": VERSION,
            "search": {"providers": _provider_states(search_providers, app.state.breaker)},
            "fetch": {"providers": _provider_states(fetch_providers, app.state.breaker)},
        }

    @app.post("/v2/search")
    async def search(request: Request) -> Response:
        """One search through the chain, in Firecrawl's v2 response shape.

        The body is read by hand rather than through a model so that every
        rejection uses the API's own ``{"success": false, "error": ...}`` shape
        instead of FastAPI's 422. ``?provider=<name>`` pins the chain to one
        link for diagnosis; it is a query parameter only and is never echoed in
        the response body.
        """
        try:
            body = await request.json()
        except ValueError:
            return _error("request body must be JSON")
        if not isinstance(body, dict):
            return _error("request body must be a JSON object")

        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            return _error("query is required")

        limit = body.get("limit", DEFAULT_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int):
            return _error("limit must be an integer")

        timeout = body.get("timeout")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        ):
            return _error("timeout must be a number of milliseconds")

        # `ignoreInvalidURLs` is accepted for Firecrawl compatibility and needs
        # no work here: the adapters only ever return entries with a URL.
        only = request.query_params.get("provider") or None

        # The caller's timeout is milliseconds; its presence replaces the
        # budget even when it is tiny, which is what makes a 1 ms diagnosis
        # call cheap and deterministic. It is capped at `search_budget_s`:
        # the budget is documented as the request ceiling, and a caller must
        # not be able to hold a connection (and an upstream) for longer.
        now = clock()
        if timeout is None:
            deadline = now + settings.search_budget_s
        else:
            deadline = now + min(float(timeout) / 1000.0, settings.search_budget_s)

        try:
            hits, _ = await run_search(
                app.state.client,
                search_providers,
                app.state.breaker,
                query,
                limit,
                deadline,
                only,
                clock=clock,
                provider_timeout_s=settings.provider_timeout_s,
            )
        except PinnedProviderError as exc:
            # A pinned provider was asked directly and did not answer: 502, so
            # "broken" cannot be mistaken for "found nothing".
            return JSONResponse(
                {"success": False, "error": f"{exc.name}: {exc.reason}"},
                status_code=502,
            )
        except ValueError as exc:
            return _error(str(exc))

        return JSONResponse({"success": True, "data": {"web": hits}})

    @app.post("/v2/scrape")
    async def scrape(request: Request) -> Response:
        """One fetch through the chain, in Firecrawl's v2 response shape.

        The body is read by hand for the same reason as ``/v2/search``: every
        rejection must use ``{"success": false, "error": ...}``, not FastAPI's
        422. ``validate_url`` runs before the chain, so a ``file://`` or a
        metadata-endpoint URL is a 400 that costs no upstream call and leaves no
        ``scrape provider=`` line in the log.

        The endings are deliberate:

        * a page the chain accepts -> ``200`` with a non-blank ``markdown``;
        * nothing fetched (every reader errored), or every candidate was a wall
          -> ``502``. Open WebUI calls ``raise_for_status()`` and reports an
          extraction error, which is what we want; a ``200`` with a CAPTCHA body
          would be silently swallowed by the model;
        * a best-effort stub under the length floor -> ``200``, because a short
          real page is still a page.

        ``?provider=<name>`` pins the chain to one reader for diagnosis, and any
        non-answer becomes ``502`` naming the reader (see ``app.fetch``).
        """
        try:
            body = await request.json()
        except ValueError:
            return _error("request body must be JSON")
        if not isinstance(body, dict):
            return _error("request body must be a JSON object")

        raw_url = body.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            return _error("url is required")
        try:
            target = validate_url(raw_url)
        except ValueError as exc:
            # Returns before the fetch chain is touched: no provider call, no
            # INFO line, so an absent `scrape provider=` line is the evidence.
            return _error(str(exc))

        timeout = body.get("timeout")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        ):
            return _error("timeout must be a number of milliseconds")

        # `formats`, `skipTlsVerification` and `removeBase64Images` are accepted
        # for Firecrawl compatibility: every reader here returns markdown and
        # manages its own TLS, so none of them has any work to do.

        only = request.query_params.get("provider") or None
        now = clock()
        if timeout is None:
            deadline = now + settings.scrape_budget_s
        else:
            # Milliseconds, capped at the documented ceiling exactly as search
            # caps it: a caller may ask for less wall clock, never more.
            deadline = now + min(float(timeout) / 1000.0, settings.scrape_budget_s)

        try:
            fetched, _ = await run_fetch(
                app.state.client,
                fetch_providers,
                app.state.breaker,
                target,
                deadline,
                only,
                clock=clock,
                provider_timeout_s=settings.fetch_provider_timeout_s,
                min_content_chars=settings.min_content_chars,
            )
        except PinnedProviderError as exc:
            # A pinned reader was asked directly and did not answer: 502, so
            # "broken", "walled" and "truncated" cannot look like content.
            return JSONResponse(
                {"success": False, "error": f"{exc.name}: {exc.reason}"},
                status_code=502,
            )
        except ValueError as exc:
            return _error(str(exc))

        if fetched is None or not fetched.markdown.strip():
            # Every reader failed, or every candidate was a wall. The second is
            # the case this route exists for: 502, never a poisoned 200.
            return JSONResponse(
                {
                    "success": False,
                    "error": "no provider could fetch the page: every reader "
                    "failed or returned a blocked page",
                },
                status_code=502,
            )

        return JSONResponse(
            {
                "success": True,
                "data": {
                    "markdown": fetched.markdown,
                    "metadata": {
                        "statusCode": fetched.status_code,
                        "sourceURL": fetched.source_url,
                        "title": fetched.title,
                        # No reader in the chain returns a meta description;
                        # the key is in the contract, so it is present and empty
                        # rather than absent.
                        "description": "",
                    },
                },
            }
        )

    # -- map and crawl: crw only, so a passthrough rather than a chain --------
    #
    # crw is the only implementation of both, so there is nothing to fall back
    # to and nothing to transform: forwarding the caller's body and returning
    # crw's own body and status is the whole contract. The one rule that is not
    # "pass it through" is the failure ending, because "crw is down" must not
    # look like "this site has no links": every 5xx, timeout and transport
    # error is a 502 in this service's own error shape (`_upstream_failed`).

    def _failed(route: str, started: float, reason: str) -> JSONResponse:
        logger.warning(
            "proxy route=%s status=502 upstream=%s ms=%d",
            route,
            reason,
            int((clock() - started) * 1000),
        )
        return _upstream_failed(f"crw: {reason}")

    def _rejected(
        route: str, started: float, reason: str, message: str
    ) -> JSONResponse:
        """A local 400, logged like every other ending: no upstream call happens.

        The reason is a token (``invalid_job_id``, ``empty_body``, ...); the
        caller's body and id never reach the log.
        """
        logger.warning(
            "proxy route=%s status=400 reason=%s ms=%d",
            route,
            reason,
            int((clock() - started) * 1000),
        )
        return _error(message)

    async def _proxy(
        request: Request,
        route: str,
        method: str,
        path: str,
        *,
        use_scrape_timeout: bool,
    ) -> Response:
        """Forward one call to crw and return its body and status unchanged.

        ``use_scrape_timeout`` is true for the two POSTs: a map walks the same
        renderer ladder as a scrape, so its per-read httpx timeout is
        ``scrape_budget_s`` (75 s) rather than ``provider_timeout_s`` (15 s).
        This is a *per-read* timeout, not the wall-clock deadline ``/v2/scrape``
        uses: a slow-but-steady map is not cut off at 75 s, an idle one is. The
        status and cancel calls answer in milliseconds and keep the 15 s
        ceiling explicitly, because the shared client's default is the larger
        fetch ceiling.

        The caller's ``Authorization`` is deliberately not sent: crw has no
        auth, and this service's token is not crw's business.
        """
        started = clock()
        body: bytes | None = None
        if method == "POST":
            # Read the raw bytes, not a model: the body is forwarded unchanged,
            # and an empty or non-object body is rejected with this service's
            # error shape instead of being handed to crw half-understood.
            body = await request.body()
            if not body:
                return _rejected(
                    route, started, "empty_body", "request body must be JSON"
                )
            try:
                parsed = json.loads(body)
            except ValueError:
                return _rejected(
                    route, started, "malformed_json", "request body must be JSON"
                )
            if not isinstance(parsed, dict):
                return _rejected(
                    route,
                    started,
                    "non_object_body",
                    "request body must be a JSON object",
                )

        headers = {"content-type": "application/json"} if body is not None else {}
        # `timeout=None` would *disable* httpx's timeout, so the override is
        # always passed. The two POSTs get the scrape budget; status and cancel
        # get the search-sized ceiling, because the shared client's own default
        # is the *larger* fetch ceiling (see `create_app`).
        request_options = {
            "timeout": (
                settings.scrape_budget_s
                if use_scrape_timeout
                else settings.provider_timeout_s
            )
        }

        try:
            upstream = await app.state.client.request(
                method,
                settings.crw_url.rstrip("/") + path,
                content=body,
                headers=headers,
                **request_options,
            )
        except httpx.TimeoutException:
            return _failed(route, started, "timeout")
        except httpx.HTTPError as exc:
            # Class name only: the exception message can carry the internal URL.
            return _failed(route, started, type(exc).__name__)

        if upstream.status_code >= 500:
            # crw's 5xx body can name an engine or a target; it never reaches
            # the caller, and the log carries the status only.
            return _failed(route, started, f"status {upstream.status_code}")

        logger.info(
            "proxy route=%s status=%d ms=%d",
            route,
            upstream.status_code,
            int((clock() - started) * 1000),
        )
        # Raw bytes plus crw's own content type: a "passed through unchanged"
        # body must not be re-serialised, and crw's 400 for a malformed job id
        # is `text/plain`, not JSON.
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    async def _proxy_job(
        request: Request, route: str, method: str, job_id: str
    ) -> Response:
        started = clock()
        # Reject before interpolation: `..`, `.` and anything else outside the
        # UUID-ish shape would be normalised out of the outgoing path by httpx
        # (see JOB_ID_PATTERN), turning a per-job call into a collection one.
        if not JOB_ID_PATTERN.fullmatch(job_id):
            return _rejected(route, started, "invalid_job_id", "invalid job id")
        # Still quoted: the pattern already excludes unsafe characters, so this
        # is belt-and-braces rather than the guarantee.
        segment = quote(job_id, safe="")
        return await _proxy(
            request,
            route,
            method,
            f"{CRW_CRAWL_JOB_PATH}/{segment}",
            use_scrape_timeout=False,
        )

    @app.post("/v2/map")
    async def map_site(request: Request) -> Response:
        """crw's site map, passed through.

        ``{"url": ..., "limit": ...}`` in, crw's ``{"success": true,
        "links": [...]}`` out. crw rejects a bad URL or a limit it cannot honour
        with its own 4xx, returned unchanged; a 5xx or a dead crw is a 502.
        """
        return await _proxy(
            request, "map", "POST", CRW_MAP_PATH, use_scrape_timeout=True
        )

    @app.post("/v2/crawl")
    async def start_crawl(request: Request) -> Response:
        """Start a crw crawl and return its job envelope unchanged.

        crw is asynchronous: this answers in milliseconds with ``{"success":
        true, "id": ..., "url": ...}`` and the pages arrive later from the
        status route below. The ``url`` is passed through as crw wrote it,
        including its internal host, because rewriting another service's body
        is exactly what this route does not do.
        """
        return await _proxy(
            request, "crawl", "POST", CRW_CRAWL_PATH, use_scrape_timeout=True
        )

    @app.get("/v2/crawl/{job_id}")
    async def crawl_status(request: Request, job_id: str) -> Response:
        """Poll a crawl job, forwarded to crw's (deprecated) ``/v2/crawl/{id}``.

        See :data:`CRW_CRAWL_JOB_PATH` for why the alias and not the
        ``/firecrawl/`` form: crw advertises the alias in its own crawl response,
        and both answer identically today. A non-UUID id is this service's 400
        and never reaches crw (see :data:`JOB_ID_PATTERN`).
        """
        return await _proxy_job(request, "crawl-status", "GET", job_id)

    @app.delete("/v2/crawl/{job_id}")
    async def cancel_crawl(request: Request, job_id: str) -> Response:
        """Cancel a crawl job. crw supports this; it is verified, not assumed.

        Live on 2026-10-30: ``DELETE /firecrawl/v2/crawl/{id}`` and the alias
        both answer ``200 {"status": "cancelled", "success": true}``, and a
        later GET reports ``status: "cancelled"``. So the route exists rather
        than 405ing; a job that is already finished is crw's 400, passed
        through with crw's body.
        """
        return await _proxy_job(request, "crawl-cancel", "DELETE", job_id)

    return app


def create_production_app() -> FastAPI:
    """uvicorn's entry point: ``app.main:create_production_app --factory``."""
    _configure_logging()
    return create_app(load_settings())
