# crw

Internal fetch engine for the homelab: [fastCRW](https://github.com/fastcrw/crw)
("crw") is a Rust, Firecrawl-compatible scrape/crawl server that replaces the
self-hosted Firecrawl stack. It fetches a URL, extracts main content, and returns
markdown/HTML/JSON. Rendering is a ladder: static pages are fetched over plain
HTTP; pages that need JavaScript escalate to [LightPanda](https://lightpanda.io/),
a lightweight headless browser that speaks CDP over a websocket; pages LightPanda
cannot carry escalate once more to full Chromium. Nothing is published inbound:
no `ports:` mapping, no Traefik route. The stack only opens outbound connections,
and only peers on `search_net` can reach it.

## No Traefik route, by design

`crw` is not exposed publicly. There is no `ports:` mapping and no Traefik
labels in this stack, and that is deliberate — crw has no authentication layer
of its own. Only containers attached to `search_net` can reach it, as
`http://crw:3000`. If a caller on the host or outside Docker ever needs it, add
a loopback-only publish (`127.0.0.1:3000:3000`) rather than a Traefik router.

## Network

`search_net` is an external bridge network. `deploy.sh` is the only thing that
creates it (`docker network create search_net`); this compose file and
`searxng/docker-compose.yml` both declare it `external: true` and merely join it.
That is why a fresh host has to run `deploy.sh` (or that one command) before
either stack can start. It is the network shared with SearXNG, which is what
makes `http://searxng:8080` reachable from crw — that is the only reason searxng
is attached to it (see "Search" below). Recreate it by hand if it is ever
missing:

```sh
docker network create search_net
```

## Pinned images

All three images are pinned deliberately. The rule is a forward one — a new
stack pins its images — and `searxng` is the single documented exception: it
stays tag-floating because watchtower owns its upgrades under the
`com.centurylinklabs.watchtower.enable` label (see the comment above its
`image:` line). Older stacks in this repo (`pihole`, `wallabag`, `dawarich`, `ArchiveBox`)
still float; they predate the rule and are not a licence to add more. (Firecrawl
and the wrapped Jina reader floated too; both are now retired.)

| Service      | Reference                                                                                          | Resolved   |
| ------------ | -------------------------------------------------------------------------------------------------- | ---------- |
| `crw`        | `ghcr.io/fastcrw/crw:0.39.1`                                                                        | 2026-10-29 |
| `lightpanda` | `lightpanda/browser@sha256:5d1d724c476957d8c2f8393a51fd089477523b0e5aa9c783c48688785d934fb9`        | 2026-10-29 |
| `chrome`     | `chromedp/headless-shell@sha256:7bee201478b2f6266aae01243740ee85c16281e32f42d3aed9fdf04f4eace7a1`   | 2026-10-29 |

The LightPanda digest was resolved on the homelab with:

```sh
docker pull lightpanda/browser:latest
docker image inspect lightpanda/browser:latest --format '{{index .RepoDigests 0}}'
```

The Chrome digest (image built 2026-10-29T01:34Z, i.e. same-day as this pin) came
from the same two commands against `chromedp/headless-shell:latest`:

```sh
docker pull chromedp/headless-shell:latest
docker image inspect chromedp/headless-shell:latest --format '{{index .RepoDigests 0}}'
# chromedp/headless-shell@sha256:7bee201478b2f6266aae01243740ee85c16281e32f42d3aed9fdf04f4eace7a1
```

To upgrade either renderer, re-run those two commands, paste the new digest into
`docker-compose.yml`, update the table above, and redeploy. Do not fall back to
a tag: the upstream compose file uses `:latest` for both, and a changed tag can
change the flags this file passes (a stale LightPanda image exits and
restart-loops on `--block-private-networks` / `--block-cidrs`; a Chrome image
that drops `--remote-allow-origins=*` breaks CDP over the `chrome` service name).

## Config

`config.toml` is mounted read-only at `/app/config.toml` and selected with
`CRW_CONFIG=config` — the file name must stay `config.toml` for that selector
to resolve. It sets the renderer to `auto` (HTTP first, LightPanda when the page
needs JS, Chrome when LightPanda cannot carry it), points the two JS tiers at
`ws://lightpanda:9222/` and `ws://chrome:9222/`, disables robots.txt enforcement
for internal fetches, and points `[search]` at the homelab's own SearXNG.

Because the file is a bind mount, `up -d` does **not** notice an edit to it —
Compose hashes the compose file, not the mounted contents, so a plain
`up -d` prints `Running` and leaves the old config live. Recreate the engine
after every config change; this is the only recipe in this document:

```sh
cd crw && docker compose up -d --force-recreate crw
```

A `--force-recreate` is what a compose-file edit needs too, so one recipe covers
both cases.

Startup logs prove the config was taken up (`docker logs crw | head`):

```
Renderer mode: Auto
Lightpanda CDP: ws://lightpanda:9222/
Chrome CDP: ws://chrome:9222/
search: enabled (searxng=http://searxng:8080)
JS renderers in fallback order: ["lightpanda", "chrome"]
```

`[crawler.stealth]` is on: crw rotates the User-Agent from its built-in
Chrome/Firefox/Safari pool and injects the browser-shaped headers
(`Accept`, `Accept-Language`, `Sec-Fetch-*`, `Sec-CH-UA*`,
`Upgrade-Insecure-Requests`) on the plain-HTTP tier, which supersedes the
`user_agent` value in `[crawler]` at request time. Verified by scraping
`https://httpbin.org/headers` through crw, which echoed a Safari 18.5 UA plus
the full `Sec-Fetch-*` set back as markdown.

Robots.txt stays unenforced: `https://www.w3.org/robots.txt` carries
`Disallow: /blog/?` for `User-agent: *`, and `https://www.w3.org/blog/?s=accessibility`
scrapes fine (5,782 chars of markdown, 0.13 s).

The `crw` container runs with `read_only: true`, `/tmp` on tmpfs,
`cap_drop: ALL`, `no-new-privileges`, a hard 2 GiB memory cap and
`pids_limit: 512`. LightPanda gets 1 GiB and `pids_limit: 512`; Chrome gets
2 GiB, `pids_limit: 1024` (Chromium forks renderer/gpu/utility processes) and
`shm_size: "1g"`.

`shm_size` is not free memory: `/dev/shm` is a tmpfs and tmpfs pages are charged
to the container's cgroup, so `shm_size: "2g"` inside `mem_limit: 2g` left no
headroom for Chromium itself. It is `1g` now, and forced-Chrome scrapes of
airbnb.com after the change returned the same size as before (10,010–10,058
chars of hydrated markdown, no "without JavaScript" text, ~3.5 s), so 1 GiB of
shm is enough for the pages this tier is asked to render. The 64 MiB default
crashes tabs, which is why it is not smaller.

Chrome also carries `cap_drop: ALL` and `no-new-privileges: true`, same as the
other two services. That is safe here because the image's entrypoint already
passes `--no-sandbox`, so the renderer does not depend on any capability; both
flags were measured, not assumed — booking.com still returns 23,430 chars at
statusCode 202 and airbnb.com still hydrates with them on.

All three services have a Docker healthcheck: `crw` probes
`GET /health` on port 3000, `lightpanda` and `chrome` probe a bare TCP connect
to 9222. `/bin/sh` is dash in both images (no `/dev/tcp`), so each probe is an
explicit `bash -c` — see the comments in `docker-compose.yml`. `crw` declares
`depends_on: chrome` with `required: false`, so an inactive `heavy` profile does
not break a plain `docker compose up`.

## Search (SearXNG backend)

`[search]` is on and pointed at the homelab's SearXNG over `search_net`:

```toml
[search]
enabled = true
searxng_url = "http://searxng:8080"
timeout_ms = 30000
default_limit = 8
max_limit = 20
```

That makes `searxng/docker-compose.yml` load-bearing for this stack: searxng is
the only service attached to both `traefik_net` (public route) and `search_net`
(this call). If searxng is not on `search_net`, crw still starts with `[search]`
enabled and every search fails — crw probes the backend at startup and logs the
result, so the second line below is the go/no-go signal:

```
INFO crw_server: search: enabled (searxng=http://searxng:8080)
INFO crw_server: search: SearXNG reachable at http://searxng:8080
```

After changing `config.toml` force a recreate rather than a bare `up -d`, then
confirm the `search: enabled` line appeared:

```sh
cd crw && docker compose up -d --force-recreate crw
docker logs crw 2>&1 | grep -i search
```

`POST /firecrawl/v2/search` returns the Firecrawl shape — `data.web` is an array
of objects; `success:true` says the request was served, not that it found
anything:

```sh
docker run --rm --network search_net curlimages/curl:8.10.1 -sS \
  -X POST http://crw:3000/firecrawl/v2/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"python asyncio tutorial","limit":5}'
```

```json
{"success":true,"data":{"web":[
  {"url":"https://realpython.com/async-io-python/",
   "title":"Python's asyncio: A Hands-On Walkthrough – Real Python",
   "description":"…","snippet":"…","position":1,"score":90.0,
   "publishedDate":"2026-10-24T14:09:37","category":"general"}
]},"creditsUsed":0,"id":"…"}
```

Per-result keys seen on every entry: `url`, `title`, `description`, `snippet`,
`position`, `score`, `category`; `publishedDate` appears only when the engine
supplies one. `description` and `snippet` carry the same text — SearXNG gives
one content field and crw fills both.

Measured 2026-10-29, limit 5, *before* the 4 s cap described under "SearXNG's
4 s cap" below — the 20.0 s rows are what that cap removed:

| Case | Response | Wall |
| --- | --- | --- |
| `python asyncio tutorial`, `limit: 5` | `success:true`, 5 entries, all with `url`+`title` | 20.0 s |
| same query, no `limit` | `success:true`, 8 entries (`default_limit`) | 20.0 s |
| zero results (`!stackoverflow zqxjvkbflurb9x3qqqz`) | `success:true`, `data.web: []` (94 bytes) | 0.24 s |
| `limit: 100` (above `max_limit`) | HTTP 400 `invalid_request`, "limit must be between 1 and 20" | 4 ms |

Three contract notes:

- **A nonsense query is not a zero-result query here.** SearXNG's engine set
  fuzzy-matches tokens, so `zzqxv flurb 9x3` returns five loosely-related hits
  (`flurb.me`, an Amazon rug listing, …) rather than nothing. To exercise the
  empty path, scope the query to one engine that does match, with a bang
  (`!stackoverflow zqxjvkbflurb9x3qqqz`).
- **Zero results are `success:true`, not an error.** `data.web` is `[]` and the
  call returns immediately. Callers must not treat an empty array as failure.
- **`max_limit` is enforced, not clamped.** A `limit` above 20 is rejected with
  HTTP 400 rather than silently reduced.

The 20 s was SearXNG's engine fan-out, not crw: `searxng/searxng/settings.yml`
force-enables every engine (`disabled: false` across the list), so one search
waited on the slowest responders. SearXNG's own JSON reply for the same query
lists a dozen engines in `unresponsive_engines` (`timeout`, `CAPTCHA`,
`unexpected crash`), and crw inherits that latency because it cannot see
per-engine status — only one flattened `data.web`. crw's `timeout_ms = 30000`
was the ceiling above that observed 20 s, not a target. The cap below removed
the 20 s without touching crw's side of it.

### SearXNG's 4 s cap (2026-10-29)

That 20 s was waiting on nothing. This build defaults
`outgoing.max_request_timeout` to `None` (`searx/settings_defaults.py`), and with
no overall deadline a search's budget is just the slowest *selected* engine's own
`timeout`. Here that is `360search` at `timeout: 20.0` — a Chinese engine that
does not answer from this host — so a search that selected it paid the full 20 s
(five of the ten baseline samples below; the other five came back in 4.8–8.3 s
because it failed fast those times). `outgoing.request_timeout` (3.0 s) bounds
one engine's socket read, not the search. `searxng/searxng/settings.yml` now sets
both:

```yaml
outgoing:
  request_timeout: 3.0
  max_request_timeout: 4.0
```

`max_request_timeout` is a whole-search budget, not a per-engine one:
`searx/search/__init__.py` computes `actual_timeout = min(default_timeout,
max_request_timeout)` and hands that to the fan-out, so the wall clock is capped
whichever engines are slow. Measured in-process against the deployed container,
flipping the setting between two runs (real engines, real network, no config
change), same query in `general`:

| `max_request_timeout` | computed `actual_timeout` | wall      | results |
| --------------------- | ------------------------- | --------- | ------- |
| `None` (old default)  | 20.0                      | 20.002 s  | 164     |
| `4.0` (new)           | 4.0                       | 4.002 s   | 163     |

Wall time matches `actual_timeout` to the millisecond whenever the slowest engine
burns its budget; a search returns sooner only if every engine answers first.
Dropping `360search` from that set with the cap still `None` moves the budget to
10.0 s (next rung: `openlibrary` and `gentoo` at `timeout: 10`), so pruning
engines only shortens the ladder one rung at a time — the cap is the lever that
bounds it outright.

`settings.yml` is a bind mount, so Compose notices nothing and `up -d` leaves
the old settings running. Recreate, then prove the value from inside the
container before believing any timing:

```sh
cd searxng && docker compose up -d --force-recreate searxng
docker exec searxng /usr/local/searxng/.venv/bin/python -c \
  'from searx import settings; o = settings["outgoing"]; print(o["request_timeout"], o["max_request_timeout"])'
# 3.0 4.0
```

Use the image's venv interpreter: the `python3` on `PATH` has no `msgspec` and
cannot `import searx` (`ModuleNotFoundError: No module named 'msgspec'`). Printing
the whole `outgoing` dict confirms nothing else moved — `verify: True`,
`proxies: None`, `enable_http2: True`, i.e. this is still direct-IP mode with
default TLS verification.

Ten distinct queries (mixed topics; en/ru/de/ja/zh) through
`https://searxng.homelab.example.com/search?q=…&format=json`, "before" being
the same ten immediately before the recreate. Wall time is curl `time_total`
from the Mac, so it includes TLS and the Traefik hop (~0.1 s) on top of
SearXNG's own budget:

| #  | Query                                         | Before (s) | After (s) | Results before | Results after | Delta         |
| -- | --------------------------------------------- | ---------: | --------: | -------------: | ------------: | ------------- |
| 1  | `python asyncio tutorial`                      | 7.46       | 4.14      | 189            | 167           | −22 (−12 %)   |
| 2  | `rust borrow checker explained`                | 20.09      | 1.05      | 169            | 156           | −13 (−8 %)    |
| 3  | `kubernetes persistent volume reclaim policy`  | 8.10       | 1.39      | 133            | 115           | −18 (−14 %)   |
| 4  | `traefik docker labels example`                | 20.11      | 1.13      | 138            | 113           | −25 (−18 %)   |
| 5  | `esp8266 deep sleep power consumption`         | 8.33       | 2.13      | 158            | 137           | −21 (−13 %)   |
| 6  | `рецепт борща классический`                    | 20.12      | 4.10      | 92             | 88            | −4 (−4 %)     |
| 7  | `Klimawandel Folgen für die Landwirtschaft`    | 4.81       | 2.32      | 251            | 233           | −18 (−7 %)    |
| 8  | `東京 観光 おすすめ`                           | 20.12      | 2.18      | 128            | 126           | −2 (−2 %)     |
| 9  | `意大利面 做法`                                 | 7.69       | 4.11      | 139            | 137           | −2 (−1 %)     |
| 10 | `home assistant zigbee coordinator comparison` | 20.12      | 4.15      | 156            | 152           | −4 (−3 %)     |

All 20 replies were HTTP 200 with non-empty `results`. Summary over the ten
after-samples: **p50 2.25 s, max 4.15 s** (before: p50 14.21 s, max 20.12 s). The
acceptance criterion was "p95 ≤ 5.0 s"; read over ten samples that means "the
maximum must stay under 5.0 s", and it does, with 0.85 s of margin. Three
supplementary probes outside the table — a news query and two others — ran
4.19 s, 3.63 s and 4.10 s, so the ceiling holds across query shapes. Note the
before-run's shape: five samples at exactly 20.09–20.12 s, i.e. a fixed wait, not
one engine being slow by chance; those five are now 1.05–4.15 s.

What the cap costs: results fell 1,553 → 1,424 in total (−8.3 %), the worst
single query losing 18 % (138 → 113). Nothing collapsed (no 200 → 20), and the
per-query `unresponsive_engines` count is 2–4 higher, which is exactly the
engines that used to answer between 4 s and 20 s now being cut off. If that
recall matters more than the seconds, the lever is pruning the engine list —
not raising the cap.

The crw path inherits the fix, because crw only waits on that same fan-out.
One probe after the change, `POST /firecrawl/v2/search` with
`{"query":"python asyncio tutorial","limit":5}`: `success:true`, 5 entries,
**4.01 s**, against 20.0 s for the same call in the table above. crw's
`timeout_ms = 30000` is unchanged and still the outer ceiling; the cap just makes
it unreachable for a search rather than tighter.

## Deploy

```sh
docker network create search_net || true        # deploy.sh does this; idempotent
cd crw && docker compose --profile heavy up -d --remove-orphans --force-recreate
```

That is exactly the line `deploy.sh` runs for this service; `--force-recreate` is
there for the bind-mounted `config.toml` (see "Config").

`chrome` sits behind the `heavy` profile because it costs a 371 MB image and
~180 MB resident for a tier that most scrapes never reach. Without the flag only
`crw` + `lightpanda` are selected, so a plain `docker compose up -d` neither
starts nor stops Chrome:

```sh
docker compose config --services                 # crw, lightpanda
docker compose --profile heavy config --services # crw, lightpanda, chrome
```

Verify:

```sh
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E '^crw'
# crw              Up 3 minutes (healthy)
# crw-chrome-1     Up 3 minutes (healthy)
# crw-lightpanda-1 Up 3 minutes (healthy)
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' crw crw-lightpanda-1 crw-chrome-1
# crw 71.81MiB / 2GiB   crw-lightpanda-1 4.203MiB / 1GiB   crw-chrome-1 179.3MiB / 2GiB
```

`(healthy)` comes from the compose healthchecks, not from Docker's default: they
are the only signal that the engine answers and that both CDP ports are open.
`docker compose --profile heavy config --quiet` must exit 0 as well — it is the
cheap check that the file (healthchecks, `depends_on`, hardening) is valid.

The crw image ships no `curl`/`wget`, so a bare `docker exec crw curl ...` fails
with exit 127. Probe the port with bash's `/dev/tcp`:

```sh
docker exec crw bash -c 'exec 3<>/dev/tcp/127.0.0.1/3000; \
  printf "GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n" >&3; \
  head -1 <&3'
# HTTP/1.1 200 OK
```

For real requests (JSON in, JSON out) use a throwaway client container on the
same network — nothing is published on the host:

```sh
docker run --rm --network search_net curlimages/curl:8.10.1 -sS \
  -X POST http://crw:3000/firecrawl/v2/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","formats":["markdown"]}'
```

crw caches scrape results per URL, so re-scraping the same URL to check a
renderer or a compose change returns the cached body in milliseconds without
touching a browser tier (visible as a 60 ms request with no renderer line in the
log). Add a throwaway query parameter (`?cb=…`) to force the real path.

## Measured scrape results (2026-10-29, crw 0.39.1, chrome tier up)

Sizes are the `size_download` of the JSON response; "markdown" is the length of
`data.markdown` in characters.

| Case                        | URL                                                          | Result                                                  | Markdown | Bytes  | Wall    |
| --------------------------- | ------------------------------------------------------------ | ------------------------------------------------------- | -------- | ------ | ------- |
| plain HTML (short page)     | `https://example.com`                                        | `success:true`, statusCode 200                          | 976      | 1,680  | 16.6 s  |
| plain HTML                  | `https://doc.rust-lang.org/book/ch01-01-installation.html`   | `success:true`, statusCode 200                          | 6,280    | 6,859  | 0.10 s  |
| plain HTML                  | `https://en.wikipedia.org/wiki/Example.com`                  | `success:true`, statusCode 200                          | 5,303    | 5,805  | 0.16 s  |
| JS page (Next.js, hydrated) | `https://www.airbnb.com/`                                    | `success:true`                                          | 15,146   | 16,359 | 3.2 s   |
| JS page (JS challenge)      | `https://www.booking.com/`                                   | `success:true`, statusCode 202 (origin's challenge)     | 13,959   | 15,010 | 1.9 s   |
| PDF                         | `https://www.orimi.com/pdf-test.pdf`                         | `success:true`, `contentType: application/pdf`, 1 page | 415      | 786    | 0.55 s  |
| private IP                  | `http://192.168.1.1/`                                        | HTTP 400 `invalid_request`, no content                  | —        | 132    | 2 ms    |
| loopback                    | `http://127.0.0.1:3000/`                                     | HTTP 400 `invalid_request`, no content                  | —        | 130    | 3 ms    |
| 404 target                  | `https://httpbin.org/status/404`                             | `success:true` + `metadata.statusCode: 404`             | 0        | 374    | 0.5 s   |

Notes on the rows that need them:
- `example.com` is under 1,000 chars because the page itself is short (976 chars
  of multilingual boilerplate), not because content was dropped. It is also the
  slowest row: its tiny HTML reads as an SPA shell to crw, so the request walks
  the whole ladder (LightPanda, then Chrome) before returning the HTTP result —
  both JS attempts burn seconds without adding content.
- `airbnb.com` is the hydration check. The same URL fetched with plain `curl` and
  no JS returns 615 KB of HTML whose *visible text* is 573 chars, including
  "some parts of the Airbnb website don't work properly without JavaScript
  enabled". The markdown crw returns carries the location-keyed carousels
  ("Explore experiences nearby") that exist only after
  hydration.
- `booking.com` answers a non-JS client with HTTP 202 and 157 chars of "JavaScript
  is disabled … we need to verify that you're not a robot", but yields 13,959
  chars of real navigation once a browser tier renders it. Its `statusCode` stays
  202 in metadata — the origin's challenge response — so `success:true` +
  `statusCode 202` is a rendered page, not an error.
- **A target 404 is not `success:false`.** crw follows Firecrawl: a 404 with a
  body comes back `success:true` with the status in `metadata.statusCode` /
  `metadata.error`, and a terse one adds only a warning (`"Target returned 404
  Not Found"`, empty markdown, `creditsUsed: 0`). `success:false` is reserved
  for crw's own failures — SSRF refusal, DNS, "all engines failed". Callers must
  branch on `metadata.statusCode`, never on `success`. Upstream:
  `conformance/FIRECRAWL-DIFF.md` §1.
- **Consumers must impose their own timeout.** crw's budget is deliberately
  generous (`request_timeout_secs = 120`, `page_timeout_ms = 30000` per tier),
  and it has no fast-fail for a slow-but-working page: the measured worst case
  for a *static* page is 16.8 s (`example.com`, which walks both JS tiers). The
  ceiling is therefore 30 s per tier under a 120 s request budget, so any caller
  that cannot wait that long — a chat turn, a UI request — sets its own deadline
  rather than relying on crw's.

## Map and crawl (2026-10-29)

Plan B's `searchhub` is specified to pass `/v2/map` and `/v2/crawl` through to
crw, and neither had been exercised before this run. Both work on the deployed
`crw` container, including its `read_only: true` root filesystem and tmpfs
`/tmp` — so the crawl job store does not need a writable volume.

| Call | Target | Result | Wall |
| --- | --- | --- | --- |
| `POST /firecrawl/v2/map` | `https://example.com`, `limit: 20` | `success:true`, 1 link (the site has no internal links) | 8.3 s |
| `POST /firecrawl/v2/map` | `https://quotes.toscrape.com/`, `limit: 20` | `success:true`, 20 links (authors, tags, `/login`) | 1.4 s |
| `POST /firecrawl/v2/crawl` | `https://example.com`, `limit: 3` | 200 + job `id`, then `status:"completed"`, 1 page, 1 credit | 21 ms to accept; ~27 s to complete |
| `POST /firecrawl/v2/crawl` | `https://quotes.toscrape.com/`, `limit: 3` | `status:"completed"`, 3 pages, 3 credits, 62–3,956 chars each | ~25 s to complete |

Two things Plan B must know:

- **`crawl` is asynchronous.** `POST /firecrawl/v2/crawl` returns
  `{"success":true,"id":…,"url":…}` in milliseconds and the pages arrive later
  from `GET /firecrawl/v2/crawl/{id}` (`status` is `scraping` then `completed`;
  `data` is empty until it finishes). The URL it returns points at the
  deprecated `/v2/crawl/{id}` alias — poll the `/firecrawl/v2/` form instead.
  A job is still running after 25 s for three pages, so the shim needs its own
  wait budget, not a single blocking call.
- **`map` is not cheap.** On a cold `example.com` it logged `SPA shell
  detected, retrying with JS renderer` and needed 8.3 s for a single link; the
  20-link site took 1.4 s. Map walks the same renderer ladder as scrape, so
  budget it like one rather than like an HTML parse.

## Renderer fallback (2026-10-29)

| Scenario                                            | Result                                                    |
| --------------------------------------------------- | --------------------------------------------------------- |
| JS page, `"renderer":"lightpanda"`                  | `success:true`, 21,040 chars, 1.96 s                      |
| JS page, auto ladder, `lightpanda` stopped          | `success:true` via Chrome — booking 15,550 chars 3.10 s, airbnb 10,016 chars 3.76 s |
| JS page, `"renderer":"chrome"`, `lightpanda` stopped| `success:true`, 15,406 chars, 1.93 s                      |
| JS page, `"renderer":"lightpanda"`, `lightpanda` stopped | HTTP 500 `SCRAPE_ALL_ENGINES_FAILED`, no content, 0.97 s |
| after `docker compose start lightpanda`             | `success:true`, 21,230 chars, 2.13 s (back on LightPanda)  |

A dead tier costs about 1.5 s of the budget (CDP discovery is refused
immediately) rather than the full `page_timeout_ms`, so a cold LightPanda cannot
push a request past the 30 s ceiling.

## Known exposure: Chrome has no private-network block of its own

`lightpanda` is started with `--block-private-networks` and `--block-cidrs`
(enforced in the browser, on the resolved socket address). The Chromium tier
gets no equivalent: its `command:` block is upstream's verbatim and Chromium has
no flag that does the same job. So the question "can the top tier be walked into
`http://192.168.1.1/`?" is answered by crw's own guards — measured, not assumed.

Do **not** test this with `httpbin.org` or `httpbingo.org`: both refuse to *issue*
a redirect to a private address (`403 Forbidden redirect target: redirect target
is an internal address`, 66 bytes, from the echo service itself), so they answer
before crw is ever involved. `postman-echo.com/redirect-to` does issue the real
302 and is what the table below uses.

Refusal can happen at three places, and they are not alternatives — a refusal at
one does not end the request where another layer still applies:

1. **Request validation.** A private address passed as the input `url` never
   reaches a tier: HTTP 400 `invalid_request` in ~2 ms. This is the only layer
   that stops a request before anything is fetched.
2. **HTTP-client redirect validation.** The plain-HTTP (and impersonated-HTTP)
   client refuses the hop itself: `redirect blocked: Access to 192.168.1.1 is
   not allowed`. This refusal does not end the request — it **escalates** the
   whole fetch to the JS tier, because that tier might render a page the HTTP
   client could not.
3. **CDP request interception.** The browser tier then intercepts the same
   navigation and fails it inside the browser, which surfaces as
   `net::ERR_BLOCKED_BY_CLIENT`.

So a forced-Chrome redirect gets refused twice in one request, and the second
refusal is the one that decides the response. That is why the browser layer
matters even though the HTTP client already refused: the HTTP client only sees
the initial navigation and its redirect chain. It cannot see what happens after
a page renders — subresources and worker-initiated fetches — and there the
browser layer is the only guard.

Probes, one per tier, `https://postman-echo.com/redirect-to?url=…` with
`"renderer"` forced:

| Redirect target          | Tier forced  | Response                                                                                                     | HTTP | Bytes | Wall    |
| ------------------------ | ------------ | ------------------------------------------------------------------------------------------------------------ | ---- | ----- | ------- |
| `http://192.168.1.1/`    | `chrome`     | `{"success":false,"error":"Renderer error: Navigation failed: net::ERR_BLOCKED_BY_CLIENT","errorCode":"renderer_error","code":"SCRAPE_ALL_ENGINES_FAILED"}` | 500 | 153 | 0.571 s |
| `http://127.0.0.1:3000/` | `chrome`     | same                                                                                                          | 500  | 153   | 0.576 s |
| `http://192.168.1.1/`    | `lightpanda` | same                                                                                                          | 500  | 153   | 0.478 s |
| `http://127.0.0.1:3000/` | `lightpanda` | same                                                                                                          | 500  | 153   | 0.406 s |

All four fail closed: no content, `success:false`, well under a second (0.4–0.6 s
in these probes; 0.92 s for a re-run through the full auto ladder after the
hardening below — the variance is the redirect fetch, not the refusal). crw's log
for that re-run shows the whole chain — the HTTP tier refuses, escalates, and the
browser tier refuses the same hop:

```
INFO crw_renderer: HTTP fetch failed, escalating to JS renderer url=https://postman-echo.com error=HTTP request failed: https://postman-echo.com (u:486d92c2606f): error following redirect
WARN crw_renderer: JS renderer failed: Renderer error: Navigation failed: net::ERR_BLOCKED_BY_CLIENT renderer="chrome"
WARN crw_renderer: Both HTTP and JS failed: http=HTTP request failed: … error following redirect, js=Renderer error: Navigation failed: net::ERR_BLOCKED_BY_CLIENT
INFO crw_server::middleware: request method=POST route="/firecrawl/v2/scrape" status=500 latency_ms=571
```

So the Chromium tier is **not** weaker than LightPanda on navigations and
redirects, and stripping `--block-private-networks` from LightPanda would not open
this path. But note what that equality means: the enforcement is the *engine's*
interception, so it covers exactly what crw intercepts, and nothing in the
`chrome` service enforces anything by itself.

Residual exposure, **not** measured here. Upstream's compose comments state that
interception "cannot see websockets or worker targets", and these probes only
cover a navigation redirect. A page that opens
`new WebSocket('ws://192.168.1.1/')` or fetches a private URL from a worker is not
a CDP-intercepted request, and Chromium will resolve any address that gets past
that layer.

"crw is unpublished and only on `search_net`" bounds **who can ask crw to fetch
a URL**. It does not bound **what a rendered page can reach**. Both browser
containers sit on an ordinary Docker bridge with ordinary egress, so from inside
a rendered page the host's published ports and the tailnet are ordinary
destinations. Measured from inside `crw-chrome-1`, with the host's bridge
gateway at `192.168.80.1`:

```
192.168.80.1:443       OPEN   (traefik, published 0.0.0.0:443)
192.168.80.1:3000      OPEN   (open-webui, published 0.0.0.0:3000)
192.168.80.1:8080      closed (searxng binds 127.0.0.1 only)
100.64.0.10:443     OPEN   (the host's tailnet address)
100.64.0.10:22      OPEN
```

That is the shape of the residual: a subresource-capable escape is not
"an attacker reaches a private page", it is "an attacker reaches everything the
host publishes and the whole tailnet". Nothing needs the escape today, because
crw fetches URLs our own stack chose. **Plan B changes that**: `searchhub` makes
crw fetch arbitrary internet-supplied URLs — a link in a chat message, a page
Open WebUI ingests — and from then on the URL is attacker-controlled, so the
subresource path becomes reachable by a remote party. Fix the egress side before
Plan B ships, not after.

Mitigations we have **not** taken, cheapest first:

1. `--proxy-server=<filtering proxy>` for the Chrome tier (crw also has a
   `[crawler] proxy` setting for the HTTP and Chrome paths), so every request —
   including websockets and workers — leaves through one filter that enforces the
   private-range deny list. Complete, but adds a service.
2. A Docker/host egress rule dropping packets from the `chrome` container to
   RFC1918, loopback and link-local. The only option that also covers websockets
   and workers *without* new software, at the cost of a host-level rule that lives
   outside this compose file.

There is **no per-flag option** that blocks only internal ranges. The one that
looks like it, `--host-resolver-rules="MAP * ~NOTFOUND"`, makes every hostname
unresolvable — and a literal `http://192.168.1.1/` never consults DNS, so it does
not even cover that case. It does not mitigate the tier, it disables it, which is
why it is not in `chrome`'s command. Filtering (proxy) or blocking (egress rule)
are the only real options.

Until one of those exists, treat the two browser tiers as equal on the navigation
path and unequal on the subresource path.

Separately, two flags in `chrome`'s `command` widen the browser's own surface.
Both are upstream's, both are kept deliberately:

- `--remote-allow-origins=*` governs the `Origin` header on the DevTools
  websocket handshake (not `Host` — the compose comment used to say that and was
  wrong). Chrome ≥ 111 refuses DevTools attachment from a web origin unless the
  origin is allowlisted; the wildcard removes that refusal. The residual is an
  attacker already on `search_net` who can open a websocket to `chrome:9222` and
  attach to a target it has to *name by UUID* — the flag does not make targets
  enumerable or guessable. `search_net`-only reachability plus unknown UUIDs is
  the whole bound.
- `--ignore-certificate-errors` means TLS is **not verified** for any page the
  Chrome tier fetches: an expired cert, a self-signed cert or a MITM on the path
  all render normally, and content from an intercepting proxy is
  indistinguishable from the origin's. It is copied from upstream because many
  scrape targets are misconfigured, and it only affects this tier; the plain-HTTP
  tier uses the Rust HTTP stack's default verification.

## Rollback

```sh
cd crw && docker compose --profile heavy down
```

`--profile heavy` is required on `down` as well, not just on `up`: without it
Compose only selects the services that have no profile, so it removes `crw` and
`crw-lightpanda-1`, leaves `crw-chrome-1` running and cannot delete the network
(`Network …_default Resource is still in use`). Measured on a scratch project
with one profiled service: `down` removed the unprofiled container and left the
profiled one behind.

To drop the Chrome tier alone and leave the fetch engine up:

```sh
docker compose --profile heavy stop chrome   # verified: Container crw-chrome-1 Stopped
```

Explicitly naming a profiled service works without taking the whole profile
down; `docker compose --profile heavy start chrome` brings it back.

Rolling back the SearXNG cap is a separate two-line change in
`searxng/searxng/settings.yml`: delete the `outgoing:` block (both
`request_timeout` and `max_request_timeout`) and recreate the container — again,
a bind mount, so `restart`/`up -d` alone leaves the old values live:

```sh
cd searxng && docker compose up -d --force-recreate searxng
```
