# searchhub

A small FastAPI service that puts a stable, Firecrawl-v2-shaped API in front of
several search and page-fetch providers.

It exists because the clients in this lab — Open WebUI and opencode — both speak
the Firecrawl API. `searchhub` keeps that surface while the implementation
behind it changes: providers can be reordered, keys can expire, a paid account
can run out of credits, and a page can turn out to be a CAPTCHA rather than an
article. The client still makes one request to one URL.

`searchhub` has no published port. It is reachable from other containers on
`search_net` at `searchhub:8080`, and from Traefik on `traefik_net` at
`https://searchhub.homelab.example.com`.

## API surface

| Route | Purpose |
|---|---|
| `POST /v2/search` | Search the web through the provider chain |
| `POST /v2/scrape` | Fetch one URL and return markdown |
| `POST /v2/map` | Pass through to `crw`'s site-map endpoint |
| `POST /v2/crawl` | Pass through to `crw`'s crawl endpoint |
| `GET /v2/crawl/{id}` | Pass through to `crw`'s job status |
| `DELETE /v2/crawl/{id}` | Pass through to `crw`'s job cancel |
| `GET /health` | Unauthenticated health probe |

Every route except `/health` requires:

```http
Authorization: Bearer $SEARCHHUB_API_KEY
```

### Search

```http
POST /v2/search
Content-Type: application/json

{"query": "rust async runtime", "limit": 8}
```

```json
{
  "success": true,
  "data": {
    "web": [
      {"url": "https://...", "title": "...", "description": "..."}
    ]
  }
}
```

Every entry has exactly `url`, `title` and `description`; entries without a URL
are dropped. Zero hits is `200` with `"web": []`, not an error. `limit` is
clamped to `1..20` (default `8`).

### Scrape

```http
POST /v2/scrape
Content-Type: application/json

{"url": "https://example.com/article", "formats": ["markdown"]}
```

```json
{
  "success": true,
  "data": {
    "markdown": "...",
    "metadata": {
      "statusCode": 200,
      "sourceURL": "https://example.com/article",
      "title": "...",
      "description": ""
    }
  }
}
```

`markdown` is non-blank on every `200`. `description` is always `""` because none
of the readers returns a meta description, but the key is part of the contract.

### Map and crawl

These are passthroughs to `crw`; there is no provider chain. The request body is
forwarded unchanged, and `crw`'s body and status are returned unchanged. A
`crw` 5xx, timeout or connection failure becomes `502` with
`{"success": false, "error": "crw: ..."}`. `POST /v2/extract` is deliberately
not implemented.

## Search chain

`SEARCH_ORDER` sets the preference order. Default:
`exa,tavily,brave,searxng`.

| Provider | Endpoint | Notes |
|---|---|---|
| Exa | `POST https://api.exa.ai/search` | `numResults`, and `contents.highlights` for snippets |
| Tavily | `POST https://api.tavily.com/search` | bearer auth; `max_results` |
| Brave | `GET https://api.search.brave.com/res/v1/web/search` | `X-Subscription-Token`, not `Bearer` |
| SearXNG | `POST {CRW_URL}/firecrawl/v2/search` | free backstop via `crw` |

For each request the chain walks the order and the **first provider with at
least one hit wins**. A provider with no key keeps its place and is skipped;
`/health` reports it as `unconfigured`.

### Breaker

One `CircuitBreaker` per provider: three consecutive failures open it for 60
seconds. After the cooldown the first call is a trial. Only a non-empty answer
resets the failure count.

- `401` (`unauthorized`) and `402` (`no_credits`) are hard failures: the
  provider is disabled until the process restarts.
- `429` advances the chain without recording a failure.
- A call cut short by the request's own deadline advances without recording a
  failure.
- Zero results advance the chain without touching the breaker.

`?provider=<name>` pins a single provider for diagnosis. A pinned provider that
was attempted and failed returns `502` with the reason rather than a misleading
empty result. A pinned call that succeeds with zero hits stays `200`.

## Fetch chain

`FETCH_ORDER` sets the preference order. Default: `crw,jina,tavily,exa`.

| Provider | Call | Text from |
|---|---|---|
| crw | `POST {CRW_URL}/firecrawl/v2/scrape` | `data.markdown` |
| Jina | `GET https://r.jina.ai/<url>` | `data.content` |
| Tavily | `POST https://api.tavily.com/extract` | `results[0].raw_content` |
| Exa | `POST https://api.exa.ai/contents` | `results[0].text` |

A candidate is accepted when all of these hold:

- the transport call succeeded;
- the page status is below `400`;
- `looks_blocked()` did not match;
- the extracted text is at least **200 characters** (`MIN_CONTENT_CHARS`).

Rejections have two classes. A **wall** is never returned once detected. A
**too-short** candidate can still be returned as the best-effort answer if every
provider failed to produce an accepted page. If every candidate was a wall, the
route returns `502`.

`crw` has no key and is always enabled. The other readers are skipped when their
key is blank.

### SSRF guard

`validate_url` runs before any reader is called:

- only `http` and `https`;
- literal IP hosts must not be private, loopback, link-local, reserved,
  multicast or unspecified;
- loopback names are refused;
- a trailing dot and the legacy all-digit IPv4 form are normalised before the
  check;
- **no DNS resolution**: a public name that resolves privately is the reader's
  job to refuse. Resolving here would make the service do DNS work before a
  fetch and make the verdict resolver-dependent.

A rejected URL is `400` and touches no provider.

## Bot-wall detection

`app/botwall.py` holds substring signatures that separate a wall from content.
The list is intentionally short and evidence-driven:

| Signature | Class |
|---|---|
| `just a moment` | JavaScript challenge |
| `checking your browser` | JavaScript challenge |
| `performing security verification` | interstitial |
| `protect against malicious bots` | interstitial |
| `sorry, you have been blocked` | hard block |
| `prove your humanity` | challenge |
| `you've been blocked by network security` | network block |
| `masa a kostí` | localised challenge |
| `the request could not be satisfied` | CDN/WAF 403 |
| `there was a problem providing the content you requested` | publisher access block |
| `reader experiencing an access issue` | publisher stub |
| `cloudflare` | brand token, membership only |

Two rules keep the detector from firing backwards:

- **Length cap.** `looks_blocked()` refuses to scan text at or above
  `WALL_MAX_CHARS` (1,000 characters), so a 4,000-character article whose footer
  says "we value your privacy" is content, while a 600-character Cloudflare
  interstitial is a wall.
- **Title check.** A wall also fires when the page title, normalised for case,
  whitespace and trailing punctuation, is *exactly* a phrase-level signature. It
  is exact, not substring: a real article titled "Cloudflare outage takes down
  half the web" is content, and a page titled exactly `Cloudflare` is not
  treated as a wall because the bare brand token is excluded from the title
  comparison.

The detector is signature-based, so a new wall phrase in a long body can still
pass. That residue is documented in the code rather than hidden.

## Configuration

`app/settings.py` reads the environment once at startup.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `SEARCHHUB_API_KEY` | yes | — | Bearer token clients present |
| `EXA_API_KEY` | no | — | Exa key; blank disables Exa |
| `TAVILY_API_KEY` | no | — | Tavily key; blank disables Tavily |
| `BRAVE_API_KEY` | no | — | Brave key; blank disables Brave |
| `JINA_API_KEY` | no | — | Jina key; Jina also works anonymously |
| `CRW_URL` | no | `http://crw:3000` | Where `crw` answers on `search_net` |
| `SEARCH_ORDER` | no | `exa,tavily,brave,searxng` | Search preference |
| `FETCH_ORDER` | no | `crw,jina,tavily,exa` | Fetch preference |
| `SEARCH_BUDGET_S` | no | `35` | Wall clock for one search |
| `SCRAPE_BUDGET_S` | no | `75` | Wall clock for one scrape |
| `PROVIDER_TIMEOUT_S` | no | `15` | Per-search-provider ceiling |
| `FETCH_PROVIDER_TIMEOUT_S` | no | `30` | Per-fetch-provider ceiling |

A blank order falls back to the compiled default. An unknown provider name stops
the container at startup rather than silently reordering. The fetch ceiling is
larger than the search ceiling because `crw`'s slowest measured page takes
about 17 seconds.

## Running

There is deliberately no tracked `.env` file. Supply the variables through the
environment or an untracked `.env` beside the compose file:

```sh
cd searchhub
SEARCHHUB_API_KEY="$(openssl rand -hex 32)" \
CRW_URL="http://crw:3000" \
docker compose up -d --build
```

The compose file requires `SEARCHHUB_API_KEY` and pins the container to
`read_only: true`, `cap_drop: ALL`, `no-new-privileges` and a 512 MB memory
limit. It has no published ports.

`.env.template` shows the full variable set. Supply the values through the
environment or an untracked `.env` file.

## Tests

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r searchhub/requirements-dev.txt
.venv/bin/pytest searchhub/tests -v
```

`pytest.ini` sets `pythonpath = .` and `testpaths = tests`. The chain tests use
`httpx.MockTransport`, so "which provider was called" is an assertion about the
recorded request list rather than a network dependency.

## Operational notes

- `uvicorn` runs with `--no-access-log`; the application logs exactly one INFO
  line per completed request.
- The breaker, the deadline and the clock are injected, so the tests cover
  cooldown and timeout behaviour without sleeping.
- A provider that returns a well-formed "this URL failed" — Tavily's empty
  `results`, Exa's `statuses[]` error, `crw`'s page-level 404 inside a `200`
  envelope — advances the chain without demoting a healthy provider.
- `searchhub` and `crw` share no credentials; `crw` has no auth and is kept
  private by network membership alone.
