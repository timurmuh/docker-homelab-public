import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.settings import Settings, load_settings

def make_settings(**kw):
    base = dict(api_key="test-token", crw_url="http://crw:3000",
                search_budget_s=35.0, scrape_budget_s=75.0,
                provider_timeout_s=15.0, min_content_chars=200)
    base.update(kw)
    return Settings(**base)

def test_health_needs_no_token():
    r = TestClient(create_app(make_settings())).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

def test_health_never_leaks_a_key():
    body = TestClient(create_app(make_settings(exa_api_key="secret-exa"))).get("/health").json()
    assert "secret-exa" not in str(body)
    assert isinstance(body["search"]["providers"], list)
    # The chain is named even when only one key is present: the order is a
    # preference, the key is the gate, and "unconfigured" is not "open".
    states = {p["name"]: p["state"] for p in body["search"]["providers"]}
    assert states == {
        "exa": "ok",
        "tavily": "unconfigured",
        "brave": "unconfigured",
        "searxng": "ok",
    }


def test_health_reports_an_open_breaker_as_open():
    app = create_app(make_settings(exa_api_key="secret-exa"))
    for _ in range(3):
        app.state.breaker.record_failure("exa")

    states = {
        p["name"]: p["state"]
        for p in TestClient(app).get("/health").json()["search"]["providers"]
    }
    assert states["exa"] == "open"
    assert states["tavily"] == "unconfigured"


def test_health_names_a_hard_failure_reason_instead_of_open():
    app = create_app(make_settings(exa_api_key="secret-exa"))
    app.state.breaker.record_hard_failure("exa", "no_credits")

    states = {
        p["name"]: p["state"]
        for p in TestClient(app).get("/health").json()["search"]["providers"]
    }
    assert states["exa"] == "no_credits"
    assert states["tavily"] == "unconfigured"


def test_missing_token_is_401_and_does_no_upstream_work():
    calls = []
    def handler(request):
        calls.append(request.url)
        raise AssertionError("upstream must not be called")
    import httpx
    app = create_app(make_settings(), transport=httpx.MockTransport(handler))
    r = TestClient(app).post("/v2/search", json={"query": "x"})
    assert r.status_code == 401
    assert calls == []

def test_wrong_token_is_401():
    app = create_app(make_settings())
    r = TestClient(app).post("/v2/search", json={"query": "x"},
                             headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401

def test_load_settings_refuses_an_empty_key(monkeypatch):
    monkeypatch.delenv("SEARCHHUB_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        load_settings()

def test_load_settings_treats_blank_provider_key_as_absent(monkeypatch):
    monkeypatch.setenv("SEARCHHUB_API_KEY", "t")
    monkeypatch.setenv("EXA_API_KEY", "")
    assert load_settings().exa_api_key is None


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("SEARCH_BUDGET_S", "nan"),
        ("SEARCH_BUDGET_S", "inf"),
        ("SEARCH_BUDGET_S", "-1"),
        ("SCRAPE_BUDGET_S", "nan"),
        ("PROVIDER_TIMEOUT_S", "inf"),
        ("FETCH_PROVIDER_TIMEOUT_S", "nan"),
        ("FETCH_PROVIDER_TIMEOUT_S", "0"),
        ("MIN_CONTENT_CHARS", "0"),
        ("MIN_CONTENT_CHARS", "-5"),
    ],
)
def test_load_settings_refuses_non_finite_or_non_positive_numbers(
    monkeypatch, name, bad
):
    # `float()` accepts nan/inf and integers can be negative; a duration that is
    # negative expires before the request starts and a NaN one makes the deadline
    # comparison meaningless, while a non-positive size rejects every page. The
    # service must refuse to start and name the variable, not run half-dead.
    monkeypatch.setenv("SEARCHHUB_API_KEY", "t")
    monkeypatch.setenv(name, bad)

    with pytest.raises(RuntimeError, match=name):
        load_settings()


# -- beyond the brief: the factory refuses to build an open app --------------

def test_create_app_refuses_an_empty_key():
    with pytest.raises(RuntimeError):
        create_app(make_settings(api_key=""))
