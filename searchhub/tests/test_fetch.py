"""The fetch chain: four readers, the two rejection classes, and `/v2/scrape`.

The transport is ``httpx.MockTransport`` with one handler that answers per host
and records every request, so "which provider was called" is an assertion about
the request list, not about a mocked function call -- the same shape
``test_search.py`` uses.

The heart of the file is the split between a **wall** (a signature matched: the
text is poisoned and can never be returned) and **too short** (nothing matched,
the text is under ``MIN_CONTENT_CHARS``: usable as a best-effort answer). A test
that only checked "short text is rejected" would pass while still shipping a
CAPTCHA, so both halves are asserted separately.
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
from app.fetch import build_fetch_providers, run_fetch, validate_url
from app.main import create_app
from app.settings import Settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

URL = "https://example.com/a"

ARTICLE = "article body " * 125  # 1,625 characters: content
LONG = "a" * 1500
STUB = "s" * 100  # too short, nothing matches
BEST_EFFORT = "best effort body " * 9  # 153 characters: too short, real text

WALL = "Just a moment... " + "x" * 296  # 312 characters, matches "just a moment"
CF_WALL = "Attention Required! Cloudflare " + "y" * 573  # 600 characters
COOKIE_ARTICLE = "We value your privacy\n\n" + "z" * 3980  # 4,000 characters

HOSTS = {
    "crw": "crw",
    "jina": "r.jina.ai",
    "tavily": "api.tavily.com",
    "exa": "api.exa.ai",
}
"""Short provider name -> the host ``Upstream`` keys its answers by."""


def crw(text: str, status: int = 200, title: str = "Crw title") -> dict:
    """crw's Firecrawl-shaped envelope; ``status`` is the *page's* status."""
    return {
        "success": True,
        "data": {
            "markdown": text,
            "metadata": {"statusCode": status, "sourceURL": URL, "title": title},
        },
    }


def jina(text: str, title: str = "Jina title") -> dict:
    return {"data": {"content": text, "title": title, "url": URL}}


def tavily(text: str) -> dict:
    return {"results": [{"url": URL, "raw_content": text}], "failed_results": []}


def exa(text: str) -> dict:
    return {
        "results": [{"url": URL, "text": text}],
        "statuses": [{"id": URL, "status": "success"}],
    }


CRW_OK = crw(ARTICLE)
JINA_OK = jina(ARTICLE)
TAVILY_OK = tavily(ARTICLE)
EXA_OK = exa(ARTICLE)


def settings(**kw) -> Settings:
    base = dict(
        api_key=TOKEN,
        crw_url="http://crw:3000",
        exa_api_key="k-exa",
        tavily_api_key="k-tavily",
        jina_api_key="k-jina",
    )
    base.update(kw)
    return Settings(**base)


class Upstream:
    """A MockTransport handler: answers per host and records the requests.

    Each host's answer is a JSON body, an ``httpx.Response`` or an exception.
    A *list* is a queue: each call pops one, and the last item repeats, which is
    how "fail once, then answer" is expressed.
    """

    def __init__(self, **answers):
        self.answers = {
            "crw": CRW_OK,
            "r.jina.ai": JINA_OK,
            "api.tavily.com": TAVILY_OK,
            "api.exa.ai": EXA_OK,
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

    def request(self, host: str) -> httpx.Request:
        for call in self.calls:
            if call.url.host == host:
                return call
        raise AssertionError(f"no request to {host}")

    def body(self, host: str) -> dict:
        return json.loads(self.request(host).content)


def failed(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, json={"error": "upstream said no"})


def chain_run(
    upstream: Upstream,
    *,
    configured: Settings | None = None,
    breaker: CircuitBreaker | None = None,
    url: str = URL,
    deadline: float | None = None,
    only: str | None = None,
    clock=time.monotonic,
    provider_timeout_s: float = 30.0,
):
    """Run one fetch chain against ``upstream``; returns ``(fetched, tried)``."""
    configured = configured or settings()
    providers = build_fetch_providers(configured)
    if breaker is None:
        breaker = CircuitBreaker(clock=clock)
    when = clock() + 30.0 if deadline is None else deadline

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            return await run_fetch(
                client,
                providers,
                breaker,
                url,
                when,
                only,
                clock=clock,
                provider_timeout_s=provider_timeout_s,
            )

    return asyncio.run(go())


def client_for(upstream: Upstream, **setting_kw) -> TestClient:
    return TestClient(
        create_app(settings(**setting_kw), transport=httpx.MockTransport(upstream))
    )


def scrape(client: TestClient, body: dict, **params) -> httpx.Response:
    return client.post("/v2/scrape", json=body, params=params, headers=AUTH)


def scrape_logs(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("scrape provider=")
    ]


# -- order, selection and the interface -------------------------------------


def test_the_default_order_is_crw_jina_tavily_exa():
    chain = build_fetch_providers(settings())
    assert [p.name for p in chain] == ["crw", "jina", "tavily", "exa"]
    assert all(p.enabled for p in chain)


def test_a_keyless_provider_keeps_its_place_disabled():
    chain = build_fetch_providers(settings(jina_api_key=None))
    by_name = {p.name: p for p in chain}
    assert [p.name for p in chain] == ["crw", "jina", "tavily", "exa"]
    assert by_name["jina"].enabled is False
    assert by_name["crw"].enabled is True  # no key is needed for crw


def test_fetch_order_is_parsed_trimmed_and_deduped():
    assert settings(fetch_order="jina, crw ,jina, exa").fetch_order == (
        "jina",
        "crw",
        "exa",
    )


def test_unknown_fetch_order_name_raises_naming_it():
    with pytest.raises(ValueError, match="nosuch"):
        build_fetch_providers(settings(fetch_order=("crw", "nosuch")))


def test_fetch_order_accepts_a_bare_string():
    assert settings(fetch_order="jina,crw").fetch_order == ("jina", "crw")


# -- accepting a good page --------------------------------------------------


def test_crw_wins_when_the_page_is_good():
    upstream = Upstream()
    fetched, tried = chain_run(upstream)

    assert fetched is not None
    assert fetched.provider == "crw"
    assert fetched.markdown == ARTICLE
    assert fetched.status_code == 200
    assert fetched.source_url == URL
    assert fetched.title == "Crw title"
    assert tried == ["crw"]
    assert upstream.hosts == ["crw"]  # Jina was never called


def test_the_crw_call_is_the_documented_firecrawl_shape():
    upstream = Upstream()
    chain_run(upstream)

    request = upstream.request("crw")
    assert request.url.path == "/firecrawl/v2/scrape"
    assert upstream.body("crw") == {
        "url": URL,
        "formats": ["markdown"],
        "onlyMainContent": True,
    }


def test_a_cookie_banner_over_a_real_article_is_accepted():
    # 4,000 characters whose first line is a consent phrase: over the wall cap,
    # so it is content, and the chain must not walk past it.
    upstream = Upstream(crw=crw(COOKIE_ARTICLE))
    fetched, tried = chain_run(upstream)

    assert fetched is not None
    assert fetched.provider == "crw"
    assert fetched.markdown == COOKIE_ARTICLE
    assert tried == ["crw"]
    assert upstream.hosts == ["crw"]


def test_a_challenge_title_over_a_huge_javascript_dump_is_a_wall():
    # Live capture: Jina rendered a publisher's challenge as 112,874 characters
    # of CAPTCHA plus inline JS, titled "Just a moment...". The text is over the
    # 1,000-character scan cap, so the title decides.
    dump = "var x = 1;\n" * 10000
    upstream = Upstream(
        crw=crw(WALL),
        jina=jina(dump, title="Just a moment..."),
    )
    fetched, tried = chain_run(upstream)

    assert tried[:2] == ["crw", "jina"]
    assert fetched is not None
    assert fetched.provider == "tavily"  # the dump was never returned


def test_a_challenge_title_alone_ends_the_chain_empty():
    dumped = "window.analytics = {};\n" * 8000
    upstream = Upstream(
        crw=crw(dumped, title="Just a moment..."),
        jina=failed(500),
        tavily=failed(500),
        exa=failed(500),
    )
    fetched, tried = chain_run(upstream)
    assert fetched is None
    assert tried == ["crw", "jina", "tavily", "exa"]


def test_a_real_headline_containing_a_brand_token_is_not_a_wall():
    # Only a title that *is* a signature counts: "cloudflare" inside a real
    # headline must not throw the article away.
    body = "article body " * 400  # 5,200 characters
    upstream = Upstream(
        crw=crw(body, title="Cloudflare outage takes down half the web")
    )
    fetched, _ = chain_run(upstream)

    assert fetched is not None
    assert fetched.provider == "crw"
    assert fetched.markdown == body


def test_a_page_titled_exactly_cloudflare_is_content():
    # Jina returns bare page titles, so the Wikipedia article on Cloudflare is
    # titled exactly "Cloudflare". The brand token is membership-only in
    # botwall.SIGNATURES and must never make a title a wall on its own.
    body = "Cloudflare is a web infrastructure company. " * 150  # 6,600 chars
    upstream = Upstream(crw=failed(500), jina=jina(body, title="Cloudflare"))
    fetched, _ = chain_run(upstream)

    assert fetched is not None
    assert fetched.provider == "jina"
    assert fetched.markdown == body


def test_a_page_titled_exactly_just_a_moment_is_a_wall():
    # A real article body does not save a page whose title is the challenge
    # phrase itself: the title is the page's own statement about itself.
    body = "article body " * 400  # 5,200 characters
    upstream = Upstream(
        crw=crw(body, title="Just a moment..."),
        jina=failed(500),
        tavily=failed(500),
        exa=failed(500),
    )
    fetched, tried = chain_run(upstream)

    assert fetched is None
    assert tried == ["crw", "jina", "tavily", "exa"]


# -- advancing: transport and page-level failures ---------------------------


def test_advance_on_a_404_carried_inside_a_success_envelope():
    # crw answers success:true even for an upstream 404; the page status is in
    # metadata.statusCode, so that is what decides.
    upstream = Upstream(crw=crw("404 page not found", status=404))
    fetched, tried = chain_run(upstream)

    assert upstream.hosts == ["crw", "r.jina.ai"]
    assert tried == ["crw", "jina"]
    assert fetched is not None and fetched.provider == "jina"


def test_a_long_404_body_is_never_returned_as_content():
    # Length is not enough: a 4,000-character "Page Not Found" is still a 404.
    upstream = Upstream(crw=crw("Page Not Found " * 300, status=404))
    fetched, _ = chain_run(upstream)

    assert upstream.hosts == ["crw", "r.jina.ai"]
    assert fetched is not None and fetched.provider == "jina"


def test_a_page_error_is_not_eligible_as_best_effort():
    upstream = Upstream(
        crw=crw("stub " * 30, status=404),  # 150 chars, but a 404
        jina=failed(500),
        tavily=failed(500),
        exa=failed(500),
    )
    fetched, _ = chain_run(upstream)
    assert fetched is None


def test_a_503_page_is_not_content_either():
    upstream = Upstream(crw=crw("Service Unavailable " * 300, status=503))
    fetched, _ = chain_run(upstream)
    assert fetched is not None and fetched.provider != "crw"


def test_a_page_404_is_not_a_provider_failure():
    breaker = CircuitBreaker()
    chain_run(Upstream(crw=crw("404 page not found", status=404)), breaker=breaker)
    assert breaker.state("crw") == "closed"


def test_advance_on_success_false():
    upstream = Upstream(crw={"success": False, "error": "boom"})
    fetched, _ = chain_run(upstream)

    assert upstream.hosts == ["crw", "r.jina.ai"]
    assert fetched is not None and fetched.provider == "jina"


def test_advance_on_a_short_body():
    upstream = Upstream(crw=crw(STUB))
    _, tried = chain_run(upstream)
    assert upstream.hosts == ["crw", "r.jina.ai"]
    assert tried == ["crw", "jina"]


def test_advance_on_a_bot_wall():
    upstream = Upstream(crw=crw(WALL))
    _, tried = chain_run(upstream)
    assert upstream.hosts == ["crw", "r.jina.ai"]
    assert tried[0] == "crw"


def test_a_wall_between_200_and_1000_chars_is_rejected():
    upstream = Upstream(crw=crw(CF_WALL))
    _, _ = chain_run(upstream)
    assert upstream.hosts == ["crw", "r.jina.ai"]


def test_a_quality_rejection_does_not_trip_the_breaker():
    breaker = CircuitBreaker()
    upstream = Upstream(crw=crw(WALL))
    chain_run(upstream, breaker=breaker)
    assert breaker.state("crw") == "closed"


# -- the exhausted chain: two classes behave differently --------------------


def test_all_candidates_are_walls_means_no_result():
    upstream = Upstream(
        crw=crw(WALL),
        jina=jina(WALL),
        tavily=tavily(WALL),
        exa=exa(WALL),
    )
    fetched, tried = chain_run(upstream)

    assert fetched is None
    assert tried == ["crw", "jina", "tavily", "exa"]


def test_a_length_only_rejection_is_still_usable():
    upstream = Upstream(
        crw=crw(BEST_EFFORT),
        jina=failed(500),
        tavily=failed(500),
        exa=failed(500),
    )
    fetched, _ = chain_run(upstream)

    assert fetched is not None
    assert fetched.markdown == BEST_EFFORT
    assert fetched.provider == "crw"


def test_the_longest_too_short_candidate_wins():
    upstream = Upstream(
        crw=crw("c" * 40),
        jina=jina("j" * 90),
        tavily=failed(500),
        exa=failed(500),
    )
    fetched, _ = chain_run(upstream)

    assert fetched is not None
    assert fetched.markdown == "j" * 90
    assert fetched.provider == "jina"


def test_a_captcha_is_never_returned_as_best_effort():
    # A wall is shorter than the threshold, so length alone would make it the
    # best-effort winner. The wall class must be excluded from that path.
    upstream = Upstream(
        crw=crw(WALL),  # 312 chars, poisoned
        jina=failed(500),
        tavily=failed(500),
        exa=failed(500),
    )
    fetched, _ = chain_run(upstream)
    assert fetched is None


def test_an_empty_body_is_not_a_usable_best_effort_answer():
    upstream = Upstream(
        crw=crw(""),
        jina=jina(""),
        tavily=tavily(""),
        exa=exa(""),
    )
    fetched, tried = chain_run(upstream)
    assert fetched is None
    assert tried == ["crw", "jina", "tavily", "exa"]


# -- Jina -------------------------------------------------------------------


def test_jina_shape_is_accepted_with_the_json_reader_headers():
    upstream = Upstream(crw=failed(500))
    fetched, _ = chain_run(upstream)

    request = upstream.request("r.jina.ai")
    assert request.headers["accept"] == "application/json"
    assert request.headers["x-return-format"] == "markdown"
    assert request.headers["authorization"] == "Bearer k-jina"
    # The target URL is the path, unslashed: https://r.jina.ai/<url>.
    assert str(request.url) == f"https://r.jina.ai/{URL}"

    assert fetched is not None
    assert fetched.provider == "jina"
    assert fetched.markdown == ARTICLE
    assert fetched.title == "Jina title"
    assert fetched.source_url == URL


def test_jina_without_a_key_is_skipped_not_called_anonymously():
    # The order is a preference, the key is the gate: a keyless provider keeps
    # its place with enabled=False (the brief's rule for both chains), so an
    # anonymous Jina call is never made. The adapter still omits the header when
    # the key is absent, so flipping `enabled` later needs no other change.
    upstream = Upstream(crw=failed(500))
    fetched, tried = chain_run(upstream, configured=settings(jina_api_key=None))
    assert tried == ["crw", "tavily"]
    assert fetched is not None and fetched.provider == "tavily"
    assert "r.jina.ai" not in upstream.hosts


def test_jina_403_falls_through_and_is_an_ordinary_failure():
    # Jina rate-limits anonymously and blocks some domains with
    # AbuseAlleviationError; a 403 is normal fall-through, not a dead key.
    upstream = Upstream(
        crw=failed(500),
        jina=httpx.Response(
            403, json={"data": None, "code": 403, "name": "AbuseAlleviationError"}
        ),
    )
    breaker = CircuitBreaker()
    fetched, tried = chain_run(upstream, breaker=breaker)

    assert tried == ["crw", "jina", "tavily"]
    assert fetched is not None and fetched.provider == "tavily"
    assert breaker.state("jina") == "closed"
    assert breaker.state("jina") != "unauthorized"


def test_jina_403_ends_the_chain_empty_when_nothing_else_is_configured():
    upstream = Upstream(
        crw=failed(500),
        jina=httpx.Response(
            403, json={"data": None, "code": 403, "name": "AbuseAlleviationError"}
        ),
    )
    configured = settings(tavily_api_key=None, exa_api_key=None)
    fetched, tried = chain_run(upstream, configured=configured)

    assert fetched is None
    assert tried == ["crw", "jina"]
    assert upstream.hosts == ["crw", "r.jina.ai"]


# -- Tavily -----------------------------------------------------------------


def test_tavily_payload_is_the_documented_one():
    upstream = Upstream(crw=failed(500), jina=failed(500))
    chain_run(upstream)

    request = upstream.request("api.tavily.com")
    assert request.url.path == "/extract"
    assert request.headers["authorization"] == "Bearer k-tavily"
    assert upstream.body("api.tavily.com") == {
        "urls": [URL],
        "format": "markdown",
    }


def test_tavilys_split_success_and_failure_is_honoured():
    # 200 with results: [] and a populated failed_results is a failure, not an
    # empty success.
    upstream = Upstream(
        crw=failed(500),
        jina=failed(500),
        tavily={
            "results": [],
            "failed_results": [{"url": URL, "error": "Failed to fetch url"}],
        },
    )
    fetched, _ = chain_run(upstream)

    assert [call.url.host for call in upstream.calls] == [
        "crw",
        "r.jina.ai",
        "api.tavily.com",
        "api.exa.ai",
    ]
    assert fetched is not None and fetched.provider == "exa"


def test_tavily_empty_results_without_a_reason_is_also_a_failure():
    upstream = Upstream(
        crw=failed(500),
        jina=failed(500),
        tavily={"results": [], "failed_results": []},
        exa=failed(500),
    )
    fetched, tried = chain_run(upstream)
    assert fetched is None
    assert tried == ["crw", "jina", "tavily", "exa"]


def test_tavilys_per_url_failure_does_not_trip_the_shared_breaker():
    # Live 2026-10-30: three scrapes of URLs the extract endpoint could not read
    # opened Tavily's breaker, and the search chain lost a healthy provider.
    breaker = CircuitBreaker()
    upstream = Upstream(tavily={"results": [], "failed_results": [{"url": URL}]})
    configured = settings(fetch_order=("tavily",))
    for _ in range(3):
        chain_run(upstream, breaker=breaker, configured=configured)
    assert breaker.state("tavily") == "closed"


def test_a_tavily_contract_change_records_a_failure():
    # {"detail": ...} instead of results[] is malformed, not a page verdict: an
    # endpoint that stays broken must become visible in /health.
    breaker = CircuitBreaker()
    upstream = Upstream(tavily={"detail": "Not Found"})
    configured = settings(fetch_order=("tavily",))
    for _ in range(3):
        chain_run(upstream, breaker=breaker, configured=configured)
    assert breaker.state("tavily") == "open"


# -- Exa --------------------------------------------------------------------


def test_exa_payload_is_the_documented_one():
    upstream = Upstream(crw=failed(500), jina=failed(500), tavily=failed(500))
    fetched, _ = chain_run(upstream)

    request = upstream.request("api.exa.ai")
    assert request.url.path == "/contents"
    assert request.headers["authorization"] == "Bearer k-exa"
    assert upstream.body("api.exa.ai") == {"urls": [URL], "text": True}
    assert fetched is not None
    assert fetched.provider == "exa"
    assert fetched.markdown == ARTICLE


def test_exas_statuses_array_is_honoured():
    upstream = Upstream(
        crw=failed(500),
        jina=failed(500),
        tavily=failed(500),
        exa={
            "results": [],
            "statuses": [{"id": URL, "status": "error", "error": "not found"}],
        },
    )
    fetched, tried = chain_run(upstream)
    assert fetched is None
    assert tried == ["crw", "jina", "tavily", "exa"]


def test_exas_per_url_failure_does_not_trip_the_breaker():
    breaker = CircuitBreaker()
    upstream = Upstream(
        exa={"results": [], "statuses": [{"id": URL, "status": "error"}]}
    )
    configured = settings(fetch_order=("exa",))
    for _ in range(3):
        chain_run(upstream, breaker=breaker, configured=configured)
    assert breaker.state("exa") == "closed"


def test_exas_results_without_text_are_a_failure():
    upstream = Upstream(
        crw=failed(500),
        jina=failed(500),
        tavily=failed(500),
        exa={
            "results": [{"url": URL, "text": None}],
            "statuses": [{"id": URL, "status": "success"}],
        },
    )
    fetched, _ = chain_run(upstream)
    assert fetched is None


# -- transport failures, hard failures and rate limits ----------------------


def test_a_malformed_body_still_trips_the_breaker():
    # The distinction that matters: a well-formed "URL failed" does not count,
    # a body that is missing the contract's fields does.
    breaker = CircuitBreaker()
    upstream = Upstream(jina={"data": None, "code": 422})
    configured = settings(fetch_order=("jina",))
    for _ in range(3):
        chain_run(upstream, breaker=breaker, configured=configured)
    assert breaker.state("jina") == "open"


def test_a_transport_error_advances_and_counts_against_the_provider():
    breaker = CircuitBreaker()
    upstream = Upstream(crw=httpx.ConnectError("nope"))
    fetched, _ = chain_run(upstream, breaker=breaker)

    assert fetched is not None and fetched.provider == "jina"
    assert breaker.state("crw") == "closed"  # one failure is not three
    breaker.record_failure("crw")
    breaker.record_failure("crw")
    assert breaker.state("crw") == "open"


def test_a_402_is_a_hard_failure_and_the_chain_skips_it_next_time():
    breaker = CircuitBreaker()
    upstream = Upstream(crw=failed(402))
    chain_run(upstream, breaker=breaker)
    assert breaker.state("crw") == "no_credits"

    before = len(upstream.calls)
    chain_run(upstream, breaker=breaker)
    assert len(upstream.calls) - before == 1  # jina only; crw skipped
    assert upstream.hosts[before:] == ["r.jina.ai"]


def test_a_401_is_unauthorized_and_not_retried():
    breaker = CircuitBreaker()
    chain_run(
        Upstream(crw=failed(500), jina=failed(500), tavily=failed(401)),
        breaker=breaker,
    )
    assert breaker.state("tavily") == "unauthorized"


def test_a_429_advances_without_recording_a_failure(caplog):
    caplog.set_level(logging.INFO)
    breaker = CircuitBreaker()
    upstream = Upstream(crw=failed(429))
    fetched, _ = chain_run(upstream, breaker=breaker)

    assert fetched is not None and fetched.provider == "jina"
    assert breaker.state("crw") == "closed"
    lines = scrape_logs(caplog)
    assert len(lines) == 1
    assert "provider=jina" in lines[0]
    assert "rate_limited" in lines[0]


def test_the_deadline_cut_short_advances_without_tripping_the_breaker():
    upstream = Upstream()

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return upstream(request)

    breaker = CircuitBreaker()
    providers = build_fetch_providers(settings())

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await run_fetch(
                client, providers, breaker, URL, time.monotonic() + 0.05
            )

    fetched, tried = asyncio.run(go())
    # The request's own deadline cut the call: no answer, no breaker failure.
    assert fetched is None
    assert tried[0] == "crw"
    assert breaker.state("crw") == "closed"


def test_our_own_per_provider_ceiling_is_not_a_provider_failure(caplog):
    # The cap is this service's policy, not a verdict on the reader: a fetch cut
    # short by it advances and says `cap_truncated`, and the breaker stays closed
    # so a slow-but-working crw is not removed from both chains for a minute.
    upstream = Upstream()

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return upstream(request)

    caplog.set_level(logging.INFO)
    breaker = CircuitBreaker()
    providers = build_fetch_providers(settings())

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await run_fetch(
                client,
                providers,
                breaker,
                URL,
                time.monotonic() + 30.0,
                provider_timeout_s=0.05,
            )

    fetched, tried = asyncio.run(go())

    assert fetched is None
    assert tried == ["crw", "jina", "tavily", "exa"]
    for name in tried:
        assert breaker.state(name) == "closed"
    lines = scrape_logs(caplog)
    assert len(lines) == 1
    assert "cap_truncated" in lines[0]
    assert "budget_truncated" not in lines[0]


def test_an_already_passed_deadline_makes_no_upstream_call():
    upstream = Upstream()
    fetched, tried = chain_run(upstream, deadline=999.0, clock=lambda: 1000.0)

    assert (fetched, tried) == (None, [])
    assert upstream.calls == []


# -- validate_url -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "file:///etc/passwd",
        "ftp://x/",
        "http://127.0.0.1:8080/",
        "http://127.0.0.1./",
        "http://localhost/",
        "http://localhost./",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http:///path",
        "HTTP://127.0.0.1/",
        "http://0.0.0.0/",
        "http://192.168.1.10/admin",
        "http://172.16.0.1/",
    ],
)
def test_validate_url_rejects_non_http_and_private_literals(bad):
    with pytest.raises(ValueError):
        validate_url(bad)


@pytest.mark.parametrize(
    "good",
    [
        "https://example.com/a?b=c",
        "http://example.com/",
        "https://8.8.8.8/",
        "https://user:pass@example.com/x",
    ],
)
def test_validate_url_accepts_public_http_urls(good):
    assert validate_url(good) == good


def test_validate_url_never_resolves_dns():
    # A public *name* is crw's job to refuse, and resolving here would let a
    # caller make this service do DNS work before any fetch.
    assert validate_url("https://definitely-not-resolvable.invalid/x")


# -- `/v2/scrape`: the route ------------------------------------------------


def test_scrape_answers_a_good_page_with_markdown_and_metadata():
    upstream = Upstream()
    response = scrape(client_for(upstream), {"url": URL, "formats": ["markdown"]})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["markdown"] == ARTICLE
    assert body["data"]["markdown"].strip()
    assert set(body["data"]) == {"markdown", "metadata"}
    assert set(body["data"]["metadata"]) == {
        "statusCode",
        "sourceURL",
        "title",
        "description",
    }
    assert body["data"]["metadata"]["statusCode"] == 200
    assert body["data"]["metadata"]["sourceURL"] == URL
    assert body["data"]["metadata"]["title"] == "Crw title"


def test_the_route_reports_the_winning_pages_status_not_the_losers():
    # crw's 404 arrives inside success:true; the accepted answer is Jina's, so
    # metadata.statusCode must be the winner's 200.
    upstream = Upstream(crw=crw("404 page not found", status=404), jina=JINA_OK)
    response = scrape(client_for(upstream), {"url": URL})

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["markdown"] == ARTICLE
    assert body["data"]["metadata"]["statusCode"] == 200
    assert upstream.hosts == ["crw", "r.jina.ai"]


def test_scrape_requires_a_token():
    upstream = Upstream()
    response = client_for(upstream).post("/v2/scrape", json={"url": URL})
    assert response.status_code == 401
    assert upstream.calls == []


def test_scrape_requires_a_url():
    upstream = Upstream()
    client = client_for(upstream)
    for body in ({}, {"url": ""}, {"url": 5}, {"url": None}):
        response = scrape(client, body)
        assert response.status_code == 400
        assert response.json()["success"] is False
    assert upstream.calls == []


def test_scrape_rejects_a_non_json_body():
    upstream = Upstream()
    response = client_for(upstream).post(
        "/v2/scrape", content=b"not json", headers=AUTH
    )
    assert response.status_code == 400
    assert upstream.calls == []


def test_a_rejected_url_is_a_400_with_no_upstream_call():
    upstream = Upstream()
    client = client_for(upstream)

    for bad in (
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/",
    ):
        response = scrape(client, {"url": bad})
        assert response.status_code == 400
        assert response.json()["success"] is False
        assert "error" in response.json()
    assert upstream.calls == []


def test_a_rejected_url_logs_nothing(caplog):
    caplog.set_level(logging.INFO)
    scrape(client_for(Upstream()), {"url": "http://169.254.169.254/latest/meta-data/"})
    # httpx logs its own "HTTP Request: POST /v2/scrape", so filter on the
    # service line rather than on the word.
    assert scrape_logs(caplog) == []


def test_every_provider_erroring_is_a_502():
    upstream = Upstream(
        crw=failed(500), jina=failed(500), tavily=failed(500), exa=failed(500)
    )
    response = scrape(client_for(upstream), {"url": URL})

    assert response.status_code == 502
    body = response.json()
    assert body["success"] is False
    assert "error" in body


def test_every_provider_walled_is_a_502_never_content():
    upstream = Upstream(
        crw=crw(WALL), jina=jina(CF_WALL), tavily=tavily(WALL), exa=exa(WALL)
    )
    response = scrape(client_for(upstream), {"url": URL})

    assert response.status_code == 502
    assert "just a moment" not in response.text
    assert "Attention Required" not in response.text


def test_a_best_effort_short_page_is_200_with_non_blank_markdown():
    upstream = Upstream(
        crw=crw(BEST_EFFORT), jina=failed(500), tavily=failed(500), exa=failed(500)
    )
    response = scrape(client_for(upstream), {"url": URL})

    assert response.status_code == 200
    assert response.json()["data"]["markdown"] == BEST_EFFORT


def test_scrape_ignores_the_firecrawl_compatibility_fields():
    upstream = Upstream()
    response = scrape(
        client_for(upstream),
        {
            "url": URL,
            "formats": ["markdown"],
            "skipTlsVerification": True,
            "removeBase64Images": False,
            "timeout": 20000,
        },
    )
    assert response.status_code == 200


def test_scrape_rejects_a_non_numeric_timeout():
    upstream = Upstream()
    response = scrape(client_for(upstream), {"url": URL, "timeout": "soon"})
    assert response.status_code == 400
    assert upstream.calls == []


def test_a_caller_timeout_is_capped_at_the_scrape_budget(monkeypatch):
    recorded: list[float] = []
    real = asyncio.wait_for

    async def spy(awaitable, timeout):
        recorded.append(timeout)
        return await real(awaitable, timeout)

    monkeypatch.setattr(
        "app.fetch.asyncio",
        types.SimpleNamespace(wait_for=spy, CancelledError=asyncio.CancelledError),
    )
    client = client_for(Upstream(), scrape_budget_s=2.0)

    assert scrape(client, {"url": URL, "timeout": 600000}).status_code == 200
    assert scrape(client, {"url": URL, "timeout": 1000}).status_code == 200

    assert recorded[0] == pytest.approx(2.0, abs=0.1)
    assert recorded[1] == pytest.approx(1.0, abs=0.1)


# -- `/v2/scrape?provider=` -------------------------------------------------


def test_a_pinned_provider_answers_alone():
    upstream = Upstream()
    response = scrape(client_for(upstream), {"url": URL}, provider="jina")

    assert response.status_code == 200
    assert response.json()["data"]["markdown"] == ARTICLE
    assert upstream.hosts == ["r.jina.ai"]


def test_pinning_an_unknown_fetch_provider_is_a_400_naming_it():
    upstream = Upstream()
    response = scrape(client_for(upstream), {"url": URL}, provider="nosuch")

    assert response.status_code == 400
    assert "nosuch" in response.json()["error"]
    assert upstream.calls == []


def test_pinning_an_unconfigured_fetch_provider_is_a_400():
    upstream = Upstream()
    response = scrape(
        client_for(upstream, jina_api_key=None), {"url": URL}, provider="jina"
    )
    assert response.status_code == 400
    assert "jina" in response.json()["error"]
    assert upstream.calls == []


def test_pinning_a_fetch_provider_outside_the_order_is_a_400():
    upstream = Upstream()
    response = scrape(
        client_for(upstream, fetch_order="crw"), {"url": URL}, provider="jina"
    )
    assert response.status_code == 400
    assert "jina" in response.json()["error"]
    assert upstream.calls == []


def test_pinning_a_provider_with_an_open_breaker_is_a_400():
    upstream = Upstream()
    app = create_app(settings(), transport=httpx.MockTransport(upstream))
    for _ in range(3):
        app.state.breaker.record_failure("jina")

    response = scrape(TestClient(app), {"url": URL}, provider="jina")
    assert response.status_code == 400
    assert "jina" in response.json()["error"]
    assert upstream.calls == []


def test_a_pinned_wall_is_a_502_and_never_content():
    response = scrape(client_for(Upstream(crw=crw(WALL))), {"url": URL}, provider="crw")

    assert response.status_code == 502
    error = response.json()["error"]
    assert error.startswith("crw: ")
    assert "just a moment" in error
    assert WALL not in response.text


def test_a_pinned_too_short_page_is_a_502_not_best_effort():
    response = scrape(
        client_for(Upstream(crw=crw(BEST_EFFORT))), {"url": URL}, provider="crw"
    )
    assert response.status_code == 502
    assert response.json()["error"].startswith("crw: ")


def test_a_pinned_500_is_a_502_naming_the_provider():
    response = scrape(
        client_for(Upstream(crw=failed(500))), {"url": URL}, provider="crw"
    )
    assert response.status_code == 502
    assert response.json()["error"] == "crw: status 500"


def test_a_pinned_page_404_is_a_502_not_a_body():
    response = scrape(
        client_for(Upstream(crw=crw("Page Not Found " * 300, status=404))),
        {"url": URL},
        provider="crw",
    )
    assert response.status_code == 502
    assert response.json()["error"] == "crw: status 404"


def test_a_pinned_402_is_a_502_no_credits_then_a_400():
    app = create_app(settings(), transport=httpx.MockTransport(Upstream(crw=failed(402))))
    client = TestClient(app)

    first = scrape(client, {"url": URL}, provider="crw")
    assert first.status_code == 502
    assert first.json()["error"] == "crw: no_credits"

    second = scrape(client, {"url": URL}, provider="crw")
    assert second.status_code == 400
    assert "no_credits" in second.json()["error"]


def test_a_pinned_tavily_url_failure_is_a_502_no_content():
    response = scrape(
        client_for(
            Upstream(tavily={"results": [], "failed_results": [{"url": URL}]})
        ),
        {"url": URL},
        provider="tavily",
    )
    assert response.status_code == 502
    assert response.json()["error"] == "tavily: no content"


def test_a_pinned_budget_truncation_is_a_502():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=CRW_OK)

    client = TestClient(
        create_app(settings(), transport=httpx.MockTransport(slow))
    )
    response = scrape(client, {"url": URL, "timeout": 50}, provider="crw")

    assert response.status_code == 502
    assert response.json()["error"] == "crw: budget_truncated"


@pytest.mark.parametrize("timeout", [0, -100])
def test_a_pinned_zero_deadline_is_a_budget_truncation(timeout):
    # "Your deadline was already gone" is not "the reader failed": a pinned
    # diagnosis must say which one happened.
    upstream = Upstream()
    app = create_app(
        settings(), transport=httpx.MockTransport(upstream), clock=lambda: 100.0
    )
    response = scrape(
        TestClient(app), {"url": URL, "timeout": timeout}, provider="crw"
    )

    assert response.status_code == 502
    assert response.json()["error"] == "crw: budget_truncated"
    assert upstream.calls == []


def test_a_pinned_failure_is_logged_with_the_provider_and_reason(caplog):
    caplog.set_level(logging.INFO)
    scrape(client_for(Upstream(crw=failed(500))), {"url": URL}, provider="crw")

    lines = scrape_logs(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("scrape provider=crw ")
    assert "error=status 500" in lines[0]


# -- the one log line -------------------------------------------------------


def test_one_info_line_per_scrape_naming_provider_and_length(caplog):
    caplog.set_level(logging.INFO)
    scrape(client_for(Upstream()), {"url": URL})

    lines = scrape_logs(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("scrape provider=crw ")
    assert "wall=none" in lines[0]
    assert f"chars={len(ARTICLE)}" in lines[0]
    assert " ms=" in lines[0]


def test_the_log_line_names_the_wall_a_later_provider_survived(caplog):
    caplog.set_level(logging.INFO)
    scrape(client_for(Upstream(crw=crw(WALL))), {"url": URL})

    lines = scrape_logs(caplog)
    assert len(lines) == 1
    assert "provider=jina" in lines[0]
    assert "wall=just a moment" in lines[0]


def test_all_walls_are_logged_as_provider_none_with_the_signature(caplog):
    caplog.set_level(logging.INFO)
    scrape(
        client_for(
            Upstream(
                crw=crw(WALL), jina=jina(WALL), tavily=tavily(WALL), exa=exa(WALL)
            )
        ),
        {"url": URL},
    )

    lines = scrape_logs(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("scrape provider=none ")
    assert "wall=just a moment" in lines[0]


def test_a_best_effort_answer_says_so_in_the_line(caplog):
    caplog.set_level(logging.INFO)
    scrape(
        client_for(
            Upstream(
                crw=crw(BEST_EFFORT),
                jina=failed(500),
                tavily=failed(500),
                exa=failed(500),
            )
        ),
        {"url": URL},
    )

    lines = scrape_logs(caplog)
    assert len(lines) == 1
    assert "provider=crw" in lines[0]
    assert "best_effort" in lines[0]
    assert f"chars={len(BEST_EFFORT)}" in lines[0]


def test_a_wall_rejection_logs_one_warning_naming_the_provider(caplog):
    caplog.set_level(logging.INFO)
    scrape(client_for(Upstream(crw=crw(WALL))), {"url": URL})

    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any("provider=crw" in line and "wall=just a moment" in line for line in warnings)
    assert all("\n" not in line for line in warnings)


def test_the_scrape_log_line_never_carries_the_url(caplog):
    caplog.set_level(logging.INFO)
    scrape(client_for(Upstream()), {"url": f"{URL}?secret=1"})
    assert all(URL not in line for line in scrape_logs(caplog))


# -- `/health` sees the fetch chain ----------------------------------------


def test_health_names_the_fetch_chain():
    body = TestClient(create_app(settings(jina_api_key=None))).get("/health").json()
    states = {p["name"]: p["state"] for p in body["fetch"]["providers"]}
    assert states == {
        "crw": "ok",
        "jina": "unconfigured",
        "tavily": "ok",
        "exa": "ok",
    }

