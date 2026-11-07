"""`/v2/map` and `/v2/crawl`: an authenticated passthrough to crw.

Unlike the search and fetch halves, these routes do not have a chain, a breaker
or a quality rule: crw is the only implementation, so the route's whole job is
to forward the caller's JSON body and return crw's own body and status
unchanged. The tests therefore assert four things and nothing more:

* the **exact upstream path** (`/firecrawl/v2/map`, `/firecrawl/v2/crawl`, and
  crw's deprecated-but-live `/v2/crawl/{id}` status alias);
* the **body is byte-for-byte** the caller's, and the caller's `Authorization`
  is **not** forwarded;
* crw's **status is preserved** -- a 4xx stays a 4xx with crw's own body;
* a crw 5xx, an unreachable crw or a timeout becomes **502**
  `{"success": false, "error": ...}`, never a 200 with an empty body.

The last rule is the reason this file exists: a passthrough that pelts a caller
with an empty 200 turns "crw is down" into "the site has no links".
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CRW = "http://crw:3000"

JOB = "6f0f0f4e-5a1b-4a1c-9a5e-123456789abc"

MAP_BODY = {
    "success": True,
    "links": ["https://example.com/", "https://example.com/about"],
}
CRAWL_BODY = {"success": True, "id": JOB, "url": f"{CRW}/v2/crawl/{JOB}"}
STATUS_BODY = {
    "success": True,
    "status": "completed",
    "total": 2,
    "completed": 2,
    "blocked": 0,
    "creditsUsed": 2,
    "data": [{"markdown": "# example", "metadata": {"sourceURL": "https://example.com/"}}],
}


def make_settings(**kw):
    base = dict(
        api_key=TOKEN,
        crw_url=CRW,
        search_budget_s=35.0,
        scrape_budget_s=75.0,
        provider_timeout_s=15.0,
        min_content_chars=200,
    )
    base.update(kw)
    return Settings(**base)


def client_with(handler, **settings_kw):
    """A TestClient whose crw is ``handler``, plus the request list it saw."""
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    app = create_app(
        make_settings(**settings_kw), transport=httpx.MockTransport(record)
    )
    return TestClient(app), calls


# -- the happy path: body in, body out, crw's status ------------------------


def test_map_and_crawl_are_forwarded_to_the_firecrawl_v2_faces():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/firecrawl/v2/map":
            return httpx.Response(200, json=MAP_BODY)
        assert request.url.path == "/firecrawl/v2/crawl"
        return httpx.Response(200, json=CRAWL_BODY)

    client, calls = client_with(handler)

    mapped = client.post("/v2/map", json={"url": "https://example.com"}, headers=AUTH)
    assert mapped.status_code == 200
    assert mapped.json() == MAP_BODY
    assert calls[-1].method == "POST"
    assert str(calls[-1].url) == f"{CRW}/firecrawl/v2/map"

    started = client.post(
        "/v2/crawl",
        json={"url": "https://example.com", "limit": 3},
        headers=AUTH,
    )
    assert started.status_code == 200
    assert started.json() == CRAWL_BODY
    assert calls[-1].method == "POST"
    assert str(calls[-1].url) == f"{CRW}/firecrawl/v2/crawl"


def test_crawl_status_uses_crws_v2_alias_deliberately():
    """The status alias is crw's deprecated ``/v2/crawl/{id}``, and it is used.

    crw 0.39.1 serves the job at both ``/firecrawl/v2/crawl/{id}`` and the
    deprecated ``/v2/crawl/{id}`` alias, with identical bodies (verified live
    2026-10-30). This route forwards to the alias on purpose: it is the shape
    crw itself hands back in the crawl response's ``url``, and it is the shape
    an MCP client polls. Pinning it in a test means a later "cleanup" to the
    ``/firecrawl/`` form has to be a deliberate change, not a drive-by.
    """
    client, calls = client_with(lambda request: httpx.Response(200, json=STATUS_BODY))

    r = client.get(f"/v2/crawl/{JOB}", headers=AUTH)

    assert r.status_code == 200
    assert r.json() == STATUS_BODY
    assert calls[-1].method == "GET"
    assert str(calls[-1].url) == f"{CRW}/v2/crawl/{JOB}"


def test_crawl_status_preserves_a_failed_status_body():
    """A completed-but-failed crawl is crw's business, not this route's."""
    failed = {
        "success": False,
        "status": "failed",
        "error": "job failed: dns",
        "data": [],
    }
    client, _ = client_with(lambda request: httpx.Response(200, json=failed))

    r = client.get(f"/v2/crawl/{JOB}", headers=AUTH)

    assert r.status_code == 200
    assert r.json() == failed


def test_delete_cancels_a_job_through_crws_alias():
    """crw 0.39.1 really does support DELETE on a crawl job: it cancels it.

    Live check on 2026-10-30: ``DELETE /firecrawl/v2/crawl/{id}`` and
    ``DELETE /v2/crawl/{id}`` both answer ``200 {"status": "cancelled",
    "success": true}`` and a later GET reports ``status: "cancelled"``. So the
    route forwards it rather than 405ing.
    """
    cancelled = {
        "message": f"Crawl job {JOB} cancelled",
        "status": "cancelled",
        "success": True,
    }
    client, calls = client_with(lambda request: httpx.Response(200, json=cancelled))

    r = client.delete(f"/v2/crawl/{JOB}", headers=AUTH)

    assert r.status_code == 200
    assert r.json() == cancelled
    assert calls[-1].method == "DELETE"
    assert str(calls[-1].url) == f"{CRW}/v2/crawl/{JOB}"
    # A cancel has no body of its own; the caller's (there is none) is not
    # invented here.
    assert calls[-1].content == b""


def test_deleting_an_already_finished_job_passes_crws_400_through():
    """A second cancel is crw's 400, not rewritten and not retried.

    Live on 2026-10-30: ``DELETE`` on a finished job answers
    ``400 {"success": false, "error": "Invalid request: Crawl job already
    finished", "errorCode": "invalid_request"}``. This route keeps it as crw
    wrote it -- the job's state is crw's to report, and a 502 would claim crw
    was broken instead.
    """
    already = (
        b'{"success":false,"error":"Invalid request: Crawl job already finished",'
        b'"errorCode":"invalid_request","code":"BAD_REQUEST"}'
    )
    client, calls = client_with(
        lambda request: httpx.Response(
            400, content=already, headers={"content-type": "application/json"}
        )
    )

    r = client.delete(f"/v2/crawl/{JOB}", headers=AUTH)

    assert r.status_code == 400
    assert r.content == already
    assert calls[-1].method == "DELETE"


# -- the body is forwarded unchanged ----------------------------------------


def test_the_json_body_is_forwarded_byte_for_byte():
    raw = b'{"url":"https://example.com","formats":["markdown"],"nested":{"b":1,"a":2}}'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == raw
        return httpx.Response(200, json=MAP_BODY)

    client, calls = client_with(handler)

    r = client.post(
        "/v2/map",
        content=raw,
        headers={**AUTH, "content-type": "application/json"},
    )

    assert r.status_code == 200
    # The bytes are compared in the handler too: this second check is what
    # fails if the route starts re-serialising through a model.
    assert calls[-1].content == raw


def test_the_callers_authorization_is_not_forwarded_upstream():
    """crw has no auth. The bearer token is this service's, not crw's."""
    client, calls = client_with(lambda request: httpx.Response(200, json=MAP_BODY))

    client.post("/v2/map", json={"url": "https://example.com"}, headers=AUTH)

    header_names = {name.lower() for name in calls[-1].headers}
    assert "authorization" not in header_names
    assert TOKEN not in calls[-1].content.decode()


# -- auth is the middleware's job: all four routes, no upstream call --------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v2/map"),
        ("post", "/v2/crawl"),
        ("get", f"/v2/crawl/{JOB}"),
        ("delete", f"/v2/crawl/{JOB}"),
    ],
)
def test_every_passthrough_route_requires_the_token(method, path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called without a token")

    client, calls = client_with(handler)

    kwargs = {"json": {"url": "https://example.com"}} if method == "post" else {}
    r = getattr(client, method)(path, **kwargs)

    assert r.status_code == 401
    assert calls == []


def test_a_wrong_token_is_401_and_does_no_upstream_work():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called with a wrong token")

    client, calls = client_with(handler)

    r = client.post(
        "/v2/map",
        json={"url": "https://example.com"},
        headers={"Authorization": "Bearer nope"},
    )

    assert r.status_code == 401
    assert calls == []


# -- failure endings: 4xx preserved, 5xx and transport errors become 502 ----


def test_a_crw_400_is_returned_unchanged_with_its_own_content_type():
    """crw's rejections are already in the API's shape; do not rewrite them."""
    crw_400 = (
        b'{"success":false,"error":"Invalid request: limit must be <= 100",'
        b'"errorCode":"invalid_request","code":"BAD_REQUEST"}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, content=crw_400, headers={"content-type": "application/json"}
        )

    client, _ = client_with(handler)

    r = client.post("/v2/crawl", json={"url": "https://example.com"}, headers=AUTH)

    assert r.status_code == 400
    assert r.content == crw_400
    assert r.headers["content-type"].startswith("application/json")


def test_a_non_json_crw_error_body_is_still_passed_through():
    """crw can answer `text/plain`; that body and content type are kept.

    A valid-looking id goes upstream (the id shape is checked here, the id's
    existence is crw's job), and whatever crw writes -- including a plain-text
    error -- comes back untouched rather than being re-wrapped as JSON.
    """
    plain = b"Invalid URL: Cannot parse `id`: UUID parsing failed: invalid character"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, content=plain, headers={"content-type": "text/plain; charset=utf-8"}
        )

    client, _ = client_with(handler)

    r = client.get(f"/v2/crawl/{JOB}", headers=AUTH)

    assert r.status_code == 400
    assert r.content == plain
    assert r.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/v2/map", {"url": "https://example.com"}),
        ("post", "/v2/crawl", {"url": "https://example.com"}),
        ("get", f"/v2/crawl/{JOB}", None),
        ("delete", f"/v2/crawl/{JOB}", None),
    ],
)
def test_a_crw_5xx_becomes_502_and_never_leaks_the_upstream_body(
    method, path, body
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"success": False, "error": "SCRAPE_ALL_ENGINES_FAILED"},
        )

    client, _ = client_with(handler)

    kwargs = {"json": body} if body is not None else {}
    r = getattr(client, method)(path, headers=AUTH, **kwargs)

    assert r.status_code == 502
    assert r.json() == {"success": False, "error": "crw: status 500"}
    assert "SCRAPE_ALL_ENGINES_FAILED" not in r.text


def test_an_unreachable_crw_becomes_502_not_an_empty_200():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client, _ = client_with(handler)

    r = client.post("/v2/map", json={"url": "https://example.com"}, headers=AUTH)

    assert r.status_code == 502
    assert r.json() == {"success": False, "error": "crw: ConnectError"}


def test_a_crw_timeout_becomes_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("crw took too long")

    client, _ = client_with(handler)

    r = client.get(f"/v2/crawl/{JOB}", headers=AUTH)

    assert r.status_code == 502
    assert r.json() == {"success": False, "error": "crw: timeout"}


# -- input that is not JSON never reaches crw -------------------------------


def test_malformed_json_is_400_and_costs_no_upstream_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called for a malformed body")

    client, calls = client_with(handler)

    r = client.post(
        "/v2/map",
        content=b"{not json",
        headers={**AUTH, "content-type": "application/json"},
    )

    assert r.status_code == 400
    assert r.json() == {"success": False, "error": "request body must be JSON"}
    assert calls == []


def test_a_non_object_json_body_is_400_and_costs_no_upstream_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called for a non-object body")

    client, calls = client_with(handler)

    r = client.post("/v2/crawl", json=["https://example.com"], headers=AUTH)

    assert r.status_code == 400
    assert r.json() == {
        "success": False,
        "error": "request body must be a JSON object",
    }
    assert calls == []


def test_an_empty_post_body_is_400():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called for an empty body")

    client, calls = client_with(handler)

    r = client.post("/v2/map", headers=AUTH)

    assert r.status_code == 400
    assert calls == []


# -- the job id is validated before it becomes a path -----------------------


@pytest.mark.parametrize("method", ["get", "delete"])
@pytest.mark.parametrize(
    "job_id",
    [
        "%2E%2E",  # "..", which quote() leaves alone and httpx normalises away
        "%2E",  # ".", the same hole
        "abc%3Fx%3D1",  # a query smuggled into the path
        "nope",  # too short to be a crw id
    ],
)
def test_a_non_uuid_job_id_is_400_and_costs_no_upstream_call(method, job_id):
    """A nonsense id must be *this* service's 400, not a wrong crw 200.

    The `..` cases are the important ones: `quote()` leaves dots unencoded, and
    httpx then drops dot segments, so `GET /v2/crawl/..` used to reach crw as
    `GET /v2` (a 200 for a non-request) and `DELETE /v2/crawl/..` as
    `DELETE /v2` -- the collection. Validation happens before interpolation, so
    neither can happen.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called for an invalid job id")

    client, calls = client_with(handler)

    r = getattr(client, method)(f"/v2/crawl/{job_id}", headers=AUTH)

    assert r.status_code == 400
    assert r.json() == {"success": False, "error": "invalid job id"}
    assert calls == []


def test_a_valid_uuid_still_reaches_crw_with_one_path_segment():
    client, calls = client_with(
        lambda request: httpx.Response(200, json={"success": True, "status": "scraping"})
    )

    client.get(f"/v2/crawl/{JOB.upper()}", headers=AUTH)

    assert calls[-1].url.raw_path == f"/v2/crawl/{JOB.upper()}".encode()
    assert calls[-1].url.query == b""


def test_a_rejected_job_id_is_logged_without_the_id(caplog):
    """The local 400s are logged too: `--no-access-log` hides them otherwise."""
    client, _ = client_with(lambda request: httpx.Response(200, json={}))

    with caplog.at_level(logging.WARNING, logger="app.proxy"):
        client.delete("/v2/crawl/%2E%2E", headers=AUTH)

    assert "proxy route=crawl-cancel status=400 reason=invalid_job_id" in caplog.text
    assert ".." not in caplog.text


# -- scope: /v2/extract is deliberately absent ------------------------------


def test_extract_is_not_implemented_and_404s_only_when_authenticated():
    """Out of scope (recorded): a 401 without a token, a 404 with one."""
    client, calls = client_with(lambda request: httpx.Response(200, json={}))

    assert client.post("/v2/extract", json={"url": "https://example.com"}).status_code == 401
    r = client.post("/v2/extract", json={"url": "https://example.com"}, headers=AUTH)

    assert r.status_code == 404
    assert calls == []
