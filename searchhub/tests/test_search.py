"""The search chain: order, advance rules, the breaker, limits and the route.

The transport is ``httpx.MockTransport`` with one handler that answers per host
and records every request, so "which provider was called" is an assertion about
the request list, not about a mocked function call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from app.breaker import CircuitBreaker
from app.main import create_app
from app.search import build_search_providers, run_search
from app.settings import Settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

HOSTS = {
    "exa": "api.exa.ai",
    "tavily": "api.tavily.com",
    "brave": "api.search.brave.com",
    "searxng": "crw",
}
"""Short provider name -> the host ``Upstream`` keys its answers by."""

EXA_HITS = [
    {
        "url": "https://exa.example/a",
        "title": "Exa result",
        "highlights": ["exa highlight"],
    }
]
EXA_OK = {"results": EXA_HITS}

TAVILY_HITS = [
    {
        "url": "https://tavily.example/a",
        "title": "Tavily result",
        "content": "tavily content",
    }
]
TAVILY_OK = {"results": TAVILY_HITS}

BRAVE_HITS = [
    {
        "url": "https://brave.example/a",
        "title": "Brave result",
        "description": "brave <strong>description</strong>",
    }
]
BRAVE_OK = {"web": {"results": BRAVE_HITS}}

SEARXNG_HITS = [
    {
        "url": "https://searx.example/a",
        "title": "SearXNG result",
        "description": "searx description",
    }
]
SEARXNG_OK = {"data": {"web": SEARXNG_HITS}}

EMPTY = {
    "exa": {"results": []},
    "tavily": {"results": []},
    "brave": {"web": {"results": []}},
    "searxng": {"data": {"web": []}},
}


def settings(**kw) -> Settings:
    base = dict(
        api_key=TOKEN,
        crw_url="http://crw:3000",
        exa_api_key="k-exa",
        tavily_api_key="k-tavily",
        brave_api_key="k-brave",
    )
    base.update(kw)
    return Settings(**base)


class Upstream:
    """A MockTransport handler: answers per host and records the requests.

    Each host's answer is a JSON body, an ``httpx.Response`` or an exception.
    A *list* is a queue: each call pops one, and the last item repeats, which is
    how "three 429s then an answer" is expressed.
    """

    def __init__(self, **answers):
        self.answers = {
            "api.exa.ai": EXA_OK,
            "api.tavily.com": TAVILY_OK,
            "api.search.brave.com": BRAVE_OK,
            "crw": SEARXNG_OK,
        }
        for name, answer in answers.items():
            self.answers[HOSTS[name]] = answer
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        answer = self.answers[request.url.host]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    @property
    def hosts(self) -> list[str]:
        return [call.url.host for call in self.calls]

    def body(self, host: str) -> dict:
        for call in self.calls:
            if call.url.host == host:
                return json.loads(call.content)
        raise AssertionError(f"no request to {host}")


def failed(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, json={"error": "upstream said no"})


def chain_run(
    upstream: Upstream,
    *,
    configured: Settings | None = None,
    breaker: CircuitBreaker | None = None,
    query: str = "searxng",
    limit: int = 5,
    deadline: float | None = None,
    only: str | None = None,
    clock=time.monotonic,
):
    """Run one chain against ``upstream``; returns ``(hits, provider)``."""
    configured = configured or settings()
    providers = build_search_providers(configured)
    if breaker is None:
        breaker = CircuitBreaker(clock=clock)
    when = clock() + 35.0 if deadline is None else deadline

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            return await run_search(
                client, providers, breaker, query, limit, when, only, clock=clock
            )

    return asyncio.run(go())


def client_for(upstream: Upstream, **setting_kw) -> TestClient:
    return TestClient(
        create_app(settings(**setting_kw), transport=httpx.MockTransport(upstream))
    )


def spy_on_wait_for(monkeypatch) -> list[float]:
    """Record the timeout each provider call is given.

    Only ``app.search``'s view of ``asyncio`` is replaced, so the event loop and
    pytest's own machinery keep the real module.
    """
    recorded: list[float] = []
    real = asyncio.wait_for

    async def spy(awaitable, timeout):
        recorded.append(timeout)
        return await real(awaitable, timeout)

    monkeypatch.setattr(
        "app.search.asyncio",
        types.SimpleNamespace(wait_for=spy, CancelledError=asyncio.CancelledError),
    )
    return recorded


# -- order and advancing ----------------------------------------------------


def test_the_leader_answers_and_nothing_else_is_called():
    upstream = Upstream()
    hits, provider = chain_run(upstream)

    assert upstream.hosts == ["api.exa.ai"]
    assert provider == "exa"
    assert hits == [
        {"url": "https://exa.example/a", "title": "Exa result", "description": "exa highlight"}
    ]


def test_exa_payload_is_the_documented_one():
    upstream = Upstream()
    chain_run(upstream)

    call = upstream.calls[0]
    assert call.method == "POST"
    assert str(call.url) == "https://api.exa.ai/search"
    assert call.headers["Authorization"] == "Bearer k-exa"
    assert upstream.body("api.exa.ai") == {
        "query": "searxng",
        "numResults": 5,
        "contents": {"highlights": True},
    }


def test_a_500_advances_to_the_next_provider():
    upstream = Upstream(exa=failed(500))
    hits, provider = chain_run(upstream)

    assert upstream.hosts == ["api.exa.ai", "api.tavily.com"]
    assert provider == "tavily"
    assert hits[0]["url"] == "https://tavily.example/a"
    assert upstream.body("api.tavily.com") == {
        "query": "searxng",
        "max_results": 5,
        "search_depth": "basic",
    }
    assert upstream.calls[1].headers["Authorization"] == "Bearer k-tavily"


def test_an_empty_200_advances_to_the_next_provider():
    upstream = Upstream(exa={"results": []})
    hits, provider = chain_run(upstream)

    assert upstream.hosts == ["api.exa.ai", "api.tavily.com"]
    assert provider == "tavily"
    assert hits[0]["url"] == "https://tavily.example/a"


def test_a_malformed_200_advances_and_no_hit_without_a_url_escapes():
    upstream = Upstream(exa={"results": [{"no_url": True}]})
    hits, provider = chain_run(upstream)

    assert upstream.hosts == ["api.exa.ai", "api.tavily.com"]
    assert provider == "tavily"
    assert hits and all(hit.get("url") for hit in hits)


def test_brave_without_a_web_key_is_zero_results_not_a_crash():
    upstream = Upstream(
        exa=EMPTY["exa"],
        tavily=EMPTY["tavily"],
        brave={"query": {}},
        searxng=EMPTY["searxng"],
    )
    hits, provider = chain_run(upstream)

    assert upstream.hosts == [
        "api.exa.ai",
        "api.tavily.com",
        "api.search.brave.com",
        "crw",
    ]
    assert (hits, provider) == ([], None)


def test_the_last_provider_is_searxng_via_crw_and_is_returned_unchanged():
    upstream = Upstream(exa=EMPTY["exa"], tavily=EMPTY["tavily"], brave=EMPTY["brave"])
    hits, provider = chain_run(upstream, query="searxng v2", limit=4)

    assert upstream.hosts == [
        "api.exa.ai",
        "api.tavily.com",
        "api.search.brave.com",
        "crw",
    ]
    call = upstream.calls[-1]
    assert call.method == "POST"
    assert str(call.url) == "http://crw:3000/firecrawl/v2/search"
    assert upstream.body("crw") == {"query": "searxng v2", "limit": 4}
    assert hits == SEARXNG_HITS
    assert provider == "searxng"


def test_brave_is_a_bare_token_get_and_its_html_is_stripped():
    upstream = Upstream(exa=EMPTY["exa"], tavily=EMPTY["tavily"])
    hits, _ = chain_run(upstream)

    call = next(c for c in upstream.calls if c.url.host == "api.search.brave.com")
    assert call.method == "GET"
    assert call.headers["X-Subscription-Token"] == "k-brave"
    assert call.url.params["q"] == "searxng"
    assert call.url.params["count"] == "5"
    assert call.url.params["text_decorations"] == "false"
    assert hits[0]["title"] == "Brave result"
    assert hits[0]["description"] == "brave description"


def test_everything_empty_is_an_empty_list_and_no_exception():
    upstream = Upstream(**EMPTY)
    assert chain_run(upstream) == ([], None)


# -- the breaker ------------------------------------------------------------


def test_ten_empty_answers_do_not_trip_the_breaker():
    upstream = Upstream(exa={"results": []})
    breaker = CircuitBreaker()
    for _ in range(10):
        chain_run(upstream, breaker=breaker)

    assert breaker.allow("exa")
    assert breaker.state("exa") == "closed"


def test_a_429_advances_without_tripping_the_breaker_and_says_so(caplog):
    upstream = Upstream(exa=failed(429))
    breaker = CircuitBreaker()
    with caplog.at_level(logging.INFO, logger="app.search"):
        for _ in range(3):
            upstream.calls.clear()
            _, provider = chain_run(upstream, breaker=breaker)
            assert upstream.hosts == ["api.exa.ai", "api.tavily.com"]
            assert provider == "tavily"

    assert breaker.allow("exa")
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("search provider=")
    ]
    assert len(lines) == 3, "exactly one INFO line per request"
    assert all("rate_limited" in line for line in lines)


def test_three_500s_open_the_breaker_and_the_next_request_skips_exa():
    upstream = Upstream(exa=failed(500))
    breaker = CircuitBreaker()
    for _ in range(3):
        chain_run(upstream, breaker=breaker)

    assert not breaker.allow("exa")

    upstream.calls.clear()
    _, provider = chain_run(upstream, breaker=breaker)
    assert upstream.hosts == ["api.tavily.com"]
    assert provider == "tavily"


def test_a_body_that_is_not_an_object_counts_as_breakage():
    # A dict with an unexpected shape is "zero results" (Brave's absent `web`);
    # a body that is not a dict at all is a broken contract, not an empty answer.
    upstream = Upstream(exa=[["not", "a", "dict"]] * 3)
    breaker = CircuitBreaker()
    for _ in range(3):
        chain_run(upstream, breaker=breaker)

    assert not breaker.allow("exa")


def test_a_body_that_is_not_json_counts_as_breakage():
    upstream = Upstream(exa=[httpx.Response(200, content=b"<html>")] * 3)
    breaker = CircuitBreaker()
    for _ in range(3):
        chain_run(upstream, breaker=breaker)

    assert not breaker.allow("exa")


# -- configuration ----------------------------------------------------------


def test_an_unconfigured_provider_is_skipped_not_failed():
    upstream = Upstream()
    hits, provider = chain_run(upstream, configured=settings(exa_api_key=None))

    assert "api.exa.ai" not in upstream.hosts
    assert upstream.hosts[0] == "api.tavily.com"
    assert provider == "tavily"
    assert hits[0]["url"] == "https://tavily.example/a"


def test_search_order_reorders_the_chain():
    upstream = Upstream()
    hits, provider = chain_run(upstream, configured=settings(search_order="searxng,exa"))

    assert upstream.hosts == ["crw"]
    assert provider == "searxng"
    assert hits == SEARXNG_HITS


def test_a_keyless_provider_is_skipped_wherever_it_sits():
    upstream = Upstream()
    _, provider = chain_run(
        upstream, configured=settings(search_order="exa,searxng", exa_api_key=None)
    )

    assert provider == "searxng"
    assert "api.exa.ai" not in upstream.hosts
    assert upstream.hosts[0] == "crw"


def test_an_unknown_search_order_name_stops_build_naming_it():
    for order in ("exa,nosuch", ("exa", "nosuch")):
        with pytest.raises(ValueError) as excinfo:
            build_search_providers(Settings(api_key="x", search_order=order))
        assert "nosuch" in str(excinfo.value)


def test_search_order_defaults_to_the_documented_four():
    names = [p.name for p in build_search_providers(settings())]
    assert names == ["exa", "tavily", "brave", "searxng"]


# -- limit and deadline -----------------------------------------------------


def test_limit_is_clamped_to_twenty_for_every_provider():
    upstream = Upstream(**EMPTY)
    chain_run(upstream, limit=100)

    assert upstream.body("api.exa.ai")["numResults"] == 20
    assert upstream.body("api.tavily.com")["max_results"] == 20
    brave = next(c for c in upstream.calls if c.url.host == "api.search.brave.com")
    assert brave.url.params["count"] == "20"
    assert upstream.body("crw")["limit"] == 20


def test_limit_is_clamped_up_to_one():
    upstream = Upstream(exa={"results": []})
    chain_run(upstream, limit=0)
    assert upstream.body("api.exa.ai")["numResults"] == 1


def test_a_elapsed_deadline_stops_the_chain_before_any_call():
    upstream = Upstream()
    hits, provider = chain_run(upstream, deadline=time.monotonic() - 1)

    assert (hits, provider) == ([], None)
    assert upstream.calls == []


# -- the route --------------------------------------------------------------


class TickingClock:
    """A clock that advances 10 ms on every reading.

    It makes the one-millisecond timeout deterministic: the deadline is already
    in the past by the time the chain reads the clock again, while the 35 s
    default would still be in the future.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 0.01
        return self.now


def test_a_route_call_returns_the_contract_shape():
    upstream = Upstream()
    response = client_for(upstream).post("/v2/search", json={"query": "searxng"}, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert set(body["data"]["web"][0]) == {"url", "title", "description"}
    assert body["data"]["web"][0]["url"] == "https://exa.example/a"
    assert upstream.body("api.exa.ai")["numResults"] == 8, "limit defaults to 8"


def test_zero_hits_on_the_route_is_a_200_with_an_empty_web_list():
    upstream = Upstream(**EMPTY)
    response = client_for(upstream).post("/v2/search", json={"query": "qqq"}, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == {"web": []}


def test_the_callers_timeout_overrides_the_budget_and_calls_nothing():
    upstream = Upstream()
    clock = TickingClock()
    app = create_app(settings(), transport=httpx.MockTransport(upstream), clock=clock)
    client = TestClient(app)

    response = client.post(
        "/v2/search", json={"query": "searxng", "timeout": 1}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"web": []}
    assert upstream.calls == [], "a 1 ms deadline must not reach a provider"

    # The same clock still allows the default 35 s budget: the difference is
    # the caller's timeout, not the clock.
    ok = client.post("/v2/search", json={"query": "searxng"}, headers=AUTH)
    assert ok.status_code == 200
    assert upstream.hosts == ["api.exa.ai"]


def test_provider_parameter_pins_the_chain_to_one_link():
    upstream = Upstream()
    response = client_for(upstream).post(
        "/v2/search?provider=tavily", json={"query": "searxng"}, headers=AUTH
    )

    assert response.status_code == 200
    assert upstream.hosts == ["api.tavily.com"]
    assert response.json()["data"]["web"][0]["url"] == "https://tavily.example/a"


@pytest.mark.parametrize("name", ["nosuch", "jina"])
def test_an_unknown_provider_parameter_is_a_400_naming_it(name):
    upstream = Upstream()
    response = client_for(upstream).post(
        f"/v2/search?provider={name}", json={"query": "searxng"}, headers=AUTH
    )

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert name in body["error"]
    assert upstream.calls == []


def test_pinning_an_unconfigured_provider_is_a_400_naming_it():
    upstream = Upstream()
    response = client_for(upstream, exa_api_key=None).post(
        "/v2/search?provider=exa", json={"query": "searxng"}, headers=AUTH
    )

    assert response.status_code == 400
    assert "exa" in response.json()["error"]
    assert upstream.calls == []


def test_pinning_an_open_breaker_is_a_400_naming_it():
    upstream = Upstream()
    client = client_for(upstream)
    breaker = client.app.state.breaker
    for _ in range(3):
        breaker.record_failure("exa")

    response = client.post(
        "/v2/search?provider=exa", json={"query": "searxng"}, headers=AUTH
    )

    assert response.status_code == 400
    assert "exa" in response.json()["error"]
    assert upstream.calls == []


# -- fix round 1: a budget truncation is not the provider's fault -----------


def test_a_budget_truncated_timeout_does_not_trip_the_breaker(caplog):
    upstream = Upstream(exa=TimeoutError("slow"))
    breaker = CircuitBreaker()
    with caplog.at_level(logging.INFO, logger="app.search"):
        for _ in range(3):
            _, provider = chain_run(
                upstream, breaker=breaker, deadline=time.monotonic() + 5.0
            )
            assert provider == "tavily"

    assert breaker.allow("exa")
    assert breaker.state("exa") == "closed"
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("search provider=")
    ]
    assert sum("budget_truncated" in line for line in lines) == 3


def test_a_timeout_at_the_providers_own_limit_does_trip_the_breaker():
    upstream = Upstream(exa=TimeoutError("slow"))
    breaker = CircuitBreaker()
    for _ in range(3):
        # The default 35 s deadline leaves the provider its full 15 s.
        chain_run(upstream, breaker=breaker)

    assert not breaker.allow("exa")
    assert breaker.state("exa") == "open"


def test_each_provider_gets_the_remaining_budget_not_a_fresh_timeout(monkeypatch):
    recorded = spy_on_wait_for(monkeypatch)
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731 - a two-line fake clock

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            now[0] += 25.0  # a slow but healthy first provider
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://t.example/1", "title": "T", "content": "c"}
                ]
            },
        )

    hits, provider = chain_run(handler, clock=clock, deadline=clock() + 35.0)

    assert provider == "tavily"
    assert hits[0]["url"] == "https://t.example/1"
    assert recorded[0] == pytest.approx(15.0), "35 s left: the provider timeout binds"
    assert recorded[1] == pytest.approx(10.0), "25 s gone: remaining must bind"


# -- fix round 1: 401/402 are hard failures, 403 is not ---------------------


def test_a_402_disables_the_provider_until_restart():
    upstream = Upstream(exa=failed(402))
    breaker = CircuitBreaker()

    _, provider = chain_run(upstream, breaker=breaker)
    assert provider == "tavily"
    assert breaker.state("exa") == "no_credits"
    assert not breaker.allow("exa")

    upstream.calls.clear()
    _, provider = chain_run(upstream, breaker=breaker)
    assert provider == "tavily"
    assert "api.exa.ai" not in upstream.hosts, "a hard-failed provider is skipped"


def test_a_401_disables_the_provider_as_unauthorized():
    upstream = Upstream(exa=failed(401))
    breaker = CircuitBreaker()

    _, provider = chain_run(upstream, breaker=breaker)
    assert provider == "tavily"
    assert breaker.state("exa") == "unauthorized"
    assert not breaker.allow("exa")


def test_a_403_stays_an_ordinary_failure():
    upstream = Upstream(exa=failed(403))
    breaker = CircuitBreaker()
    for _ in range(3):
        chain_run(upstream, breaker=breaker)

    assert breaker.state("exa") == "open"
    assert breaker.state("exa") != "no_credits"


def test_a_hard_failure_is_logged_only_once(caplog):
    upstream = Upstream(exa=failed(402))
    breaker = CircuitBreaker()
    with caplog.at_level(logging.INFO, logger="app.search"):
        for _ in range(3):
            chain_run(upstream, breaker=breaker)

    mentions = [
        record.getMessage()
        for record in caplog.records
        if "no_credits" in record.getMessage()
    ]
    assert len(mentions) == 1, "the reason is logged when it is discovered, not per request"
    assert "disabled reason=no_credits" in mentions[0]


# -- fix round 1: in pinned mode a failure is a failure ---------------------


def test_a_pinned_failure_is_a_502_naming_the_provider():
    upstream = Upstream(exa=failed(500))
    response = client_for(upstream).post(
        "/v2/search?provider=exa", json={"query": "searxng"}, headers=AUTH
    )

    assert response.status_code == 502
    body = response.json()
    assert body["success"] is False
    assert body["error"] == "exa: status 500"


def test_a_pinned_zero_result_answer_is_still_a_200_empty_list():
    upstream = Upstream(exa={"results": []})
    response = client_for(upstream).post(
        "/v2/search?provider=exa", json={"query": "searxng"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"web": []}}


def test_a_pinned_429_is_a_502_because_it_did_not_answer():
    upstream = Upstream(exa=failed(429))
    response = client_for(upstream).post(
        "/v2/search?provider=exa", json={"query": "searxng"}, headers=AUTH
    )

    assert response.status_code == 502
    assert response.json()["error"] == "exa: rate_limited"


def test_a_pinned_provider_timeout_is_a_502():
    upstream = Upstream(exa=TimeoutError("slow"))
    response = client_for(upstream).post(
        "/v2/search?provider=exa",
        # Far more than provider_timeout_s, so the provider's own 15 s is what
        # would have bounded the call (had the fake not raised immediately).
        json={"query": "searxng", "timeout": 30_000},
        headers=AUTH,
    )

    assert response.status_code == 502
    assert response.json()["error"] == "exa: timeout"


def test_a_pinned_hard_failure_is_502_then_400_with_the_reason():
    upstream = Upstream(exa=failed(402))
    client = client_for(upstream)

    first = client.post(
        "/v2/search?provider=exa", json={"query": "searxng"}, headers=AUTH
    )
    assert first.status_code == 502
    assert first.json()["error"] == "exa: no_credits"

    upstream.calls.clear()
    second = client.post(
        "/v2/search?provider=exa", json={"query": "searxng"}, headers=AUTH
    )
    assert second.status_code == 400
    assert "exa" in second.json()["error"]
    assert "no_credits" in second.json()["error"]
    assert upstream.calls == [], "a disabled provider is rejected, not called"


def test_unpinned_all_failures_are_a_200_with_an_empty_list():
    upstream = Upstream(**{name: failed(500) for name in HOSTS})
    response = client_for(upstream).post(
        "/v2/search", json={"query": "searxng"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"web": []}}


def test_all_providers_rate_limited_is_a_200_and_no_breaker_failures():
    upstream = Upstream(**{name: failed(429) for name in HOSTS})
    client = client_for(upstream)
    response = client.post("/v2/search", json={"query": "searxng"}, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"web": []}}
    for name in HOSTS:
        assert client.app.state.breaker.state(name) == "closed"
        assert client.app.state.breaker.allow(name)


# -- fix round 1: the remaining cheap gaps ---------------------------------


def test_pinning_a_known_provider_that_is_not_in_the_order_is_a_400():
    upstream = Upstream()
    response = client_for(upstream, search_order="tavily,brave").post(
        "/v2/search?provider=exa", json={"query": "searxng"}, headers=AUTH
    )

    assert response.status_code == 400
    assert "exa" in response.json()["error"]
    assert upstream.calls == []


def test_a_rejected_pin_is_logged_and_is_not_a_search(caplog):
    upstream = Upstream()
    with caplog.at_level(logging.INFO, logger="app.search"):
        response = client_for(upstream).post(
            "/v2/search?provider=nosuch", json={"query": "searxng"}, headers=AUTH
        )

    assert response.status_code == 400
    assert len(caplog.records) == 1, "one WARNING, and no INFO line for a 400"
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "rejected" in record.getMessage() and "nosuch" in record.getMessage()


def test_duplicate_search_order_names_are_deduped_in_place():
    configured = settings(search_order="exa, exa,tavily,exa")

    assert configured.search_order == ("exa", "tavily")
    assert [p.name for p in build_search_providers(configured)] == ["exa", "tavily"]


def test_a_caller_timeout_is_capped_at_the_search_budget(monkeypatch):
    recorded = spy_on_wait_for(monkeypatch)
    client = client_for(Upstream(), search_budget_s=2.0)

    capped = client.post(
        "/v2/search", json={"query": "searxng", "timeout": 600000}, headers=AUTH
    )
    assert capped.status_code == 200
    assert recorded == [pytest.approx(2.0, abs=0.01)], (
        "600 s must be cut to the 2 s budget"
    )

    recorded.clear()
    shorter = client.post(
        "/v2/search", json={"query": "searxng", "timeout": 1000}, headers=AUTH
    )
    assert shorter.status_code == 200
    assert recorded == [pytest.approx(1.0, abs=0.01)], (
        "a caller may still ask for less"
    )


# -- fix round 2: a pinned truncation is not an empty answer ----------------


def test_a_pinned_budget_truncation_is_a_502_not_an_empty_list():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=EXA_OK)

    client = TestClient(create_app(settings(), transport=httpx.MockTransport(slow)))
    response = client.post(
        "/v2/search?provider=exa",
        json={"query": "searxng", "timeout": 50},
        headers=AUTH,
    )

    assert response.status_code == 502
    assert response.json() == {"success": False, "error": "exa: budget_truncated"}
    assert client.app.state.breaker.state("exa") == "closed", "the budget, not the provider"


def test_a_pinned_call_with_an_exhausted_deadline_is_a_502_not_an_empty_list():
    # `timeout: 0` means the deadline is already gone, so the pinned provider is
    # never called. That is the same non-answer `/v2/scrape` reports, not a
    # `200 []` that would score a provider nobody asked as "found nothing".
    upstream = Upstream()
    client = client_for(upstream)

    response = client.post(
        "/v2/search?provider=exa",
        json={"query": "searxng", "timeout": 0},
        headers=AUTH,
    )

    assert response.status_code == 502
    assert response.json() == {"success": False, "error": "exa: budget_truncated"}
    assert upstream.calls == []
    assert client.app.state.breaker.state("exa") == "closed"


def test_an_unpinned_budget_truncation_advances_to_an_empty_200():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=EXA_OK)

    client = TestClient(create_app(settings(), transport=httpx.MockTransport(slow)))
    response = client.post(
        "/v2/search", json={"query": "searxng", "timeout": 50}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"web": []}}
    for name in HOSTS:
        assert client.app.state.breaker.state(name) == "closed"


def test_a_caller_timeout_equal_to_the_provider_timeout_is_not_truncation():
    # 15,000 ms equals provider_timeout_s, so the provider had its full budget;
    # the microseconds between computing the deadline and reading the clock must
    # not turn this into "budget_truncated". A ticking clock makes that
    # deterministic: two readings cost 20 ms, far inside the 50 ms tolerance.
    upstream = Upstream(exa=TimeoutError("slow"))
    clock = TickingClock()
    client = TestClient(
        create_app(settings(), transport=httpx.MockTransport(upstream), clock=clock)
    )

    for _ in range(3):
        response = client.post(
            "/v2/search?provider=exa",
            json={"query": "searxng", "timeout": 15_000},
            headers=AUTH,
        )
        assert response.status_code == 502
        assert response.json()["error"] == "exa: timeout"

    assert client.app.state.breaker.state("exa") == "open", "a provider timeout counts"


# -- fix round 2: log honesty ----------------------------------------------


def test_a_pinned_failure_line_names_the_provider_and_the_error(caplog):
    upstream = Upstream(exa=failed(500))
    with caplog.at_level(logging.INFO, logger="app.search"):
        client_for(upstream).post(
            "/v2/search?provider=exa", json={"query": "searxng"}, headers=AUTH
        )

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ]
    assert len(lines) == 1
    assert lines[0].startswith("search provider=exa hits=0 error=status 500 ms=")


def test_a_hostile_provider_name_cannot_forge_a_log_line(caplog):
    upstream = Upstream()
    hostile = "nosuch\nsearch provider=tavily hits=99" + "x" * 100
    with caplog.at_level(logging.INFO, logger="app.search"):
        response = client_for(upstream).post(
            "/v2/search",
            params={"provider": hostile},
            json={"query": "searxng"},
            headers=AUTH,
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert "\n" not in error
    assert "x" * 41 not in error, "the echoed name is capped at 40 characters"
    assert len(caplog.records) == 1, "one WARNING, no INFO line for a 400"
    message = caplog.records[0].getMessage()
    assert "\n" not in message
    assert caplog.records[0].levelno == logging.WARNING


def test_a_cancelled_request_logs_no_phantom_search(caplog):
    upstream = Upstream(exa=asyncio.CancelledError())
    with caplog.at_level(logging.INFO, logger="app.search"):
        with pytest.raises(asyncio.CancelledError):
            chain_run(upstream)

    assert not [r for r in caplog.records if "search provider=" in r.getMessage()]
