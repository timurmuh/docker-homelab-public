# Homelab

A self-hosted homelab defined as a set of Docker Compose stacks. One Linux host
runs the services; Traefik is the ingress; Tailscale provides private network
access; Cloudflare handles public DNS.

This repository is a **sanitized public snapshot**. Secrets, private notes and a
few personal services are intentionally absent — see [Public snapshot](#public-snapshot).

## Architecture

```text
                  Internet / Tailscale
                           |
                    Traefik :80/:443
             Cloudflare DNS-01 wildcard cert
                           |
                      traefik_net
          /----------------+----------------\
       pihole          open-webui        other stacks
                            |
                        notes_net
                            |
                    open-terminal / org-mcp
                            |
                       search_net
                  /---------+---------\
              searxng      crw      searchhub
```

Three ideas hold the repo together:

- **Labels, not static config.** Traefik reads Docker labels and creates routers,
  services and certificates from them. Adding a service means adding the
  container to `traefik_net` and labelling it; there is no central route file.
- **Private by default.** A service gets a published port only when it has to.
  `crw` and `searchhub` have no `ports:` mapping at all; `open-terminal` is
  reachable only from `open-webui` over `notes_net`.
- **Fail visibly.** Containers have healthchecks, deploy fails when a stack does
  not come up, and the search/fetch gateways log one structured line per request
  rather than relying on an access log.

## Service map

| Area | Stack | What it does |
|---|---|---|
| Ingress | [`traefik/`](traefik/) | Reverse proxy, TLS termination, Cloudflare DNS-01 wildcard certificates |
| DNS / adblock | [`pihole/`](pihole/) | LAN DNS and ad blocking |
| Containers | [`portainer/`](portainer/), [`watchtower/`](watchtower/) | Container UI and image updates |
| Assistant | [`open-webui/`](open-webui/) | Open WebUI, an Open Terminal sidecar, an `org-mcp` bridge and an `oikb` knowledge-base sync |
| Search | [`searchhub/`](searchhub/), [`crw/`](crw/), [`searxng/`](searxng/) | A Firecrawl-v2-shaped search/fetch gateway, a self-hosted fetch engine, and a meta-search UI |
| Reading | [`wallabag/`](wallabag/), [`ArchiveBox/`](ArchiveBox/), [`changedetectionio/`](changedetectionio/) | Read-later, web archiving and change detection |
| Files | [`seafile/`](seafile/) | File sync; block storage is mounted through rclone |
| Location | [`dawarich/`](dawarich/) | Self-hosted location history |
| Recipes | [`TandoorRecipes/`](TandoorRecipes/) | Recipe manager |
| Media | [`transmission-openvpn/`](transmission-openvpn/), [`whisper-transcriber-telegram-bot/`](whisper-transcriber-telegram-bot/) | VPN-bound torrent client and a Telegram transcription bot |
| Proxies | [`rota/`](rota/), [`proxypool/`](proxypool/) | Proxy pool aggregator and a list of upstream proxy sources |
| Benchmark | [`eval/`](eval/) | A fetch-quality evaluation harness for the search stack |

## Networks

| Network | Owner | Purpose |
|---|---|---|
| `traefik_net` | `deploy.sh` | Every service Traefik routes to |
| `search_net` | `deploy.sh` | `searxng`, `crw` and `searchhub`; nothing is published on it |
| `notes_net` | Open WebUI stack | `open-webui` ↔ `open-terminal` and `org-mcp` |
| `seafile-net` | Seafile stack | Internal database, cache and app traffic |

`traefik_net` and `search_net` are declared `external: true` in every compose
file, so no single stack can create them. `deploy.sh` creates both before
starting anything.

## The search stack

Three services share `search_net`:

- **`searxng/`** — meta-search UI with a short upstream timeout. It is the free
  backstop of the search chain and `crw`'s search backend.
- **`crw/`** — a Rust, Firecrawl-compatible scrape/crawl/search server. It has
  no authentication layer and no published port, so only containers on
  `search_net` can reach it at `crw:3000`.
- **`searchhub/`** — the one endpoint clients talk to. It exposes a subset of the
  Firecrawl v2 API, fans out to paid search providers and fetch readers, falls
  back to SearXNG and `crw`, trips a per-provider breaker on repeated failures,
  and rejects bot-wall and consent-wall pages before they reach a model. See
  [`searchhub/README.md`](searchhub/README.md) for the API contract.

The two clients — Open WebUI and opencode — both speak Firecrawl-shaped requests
to `searchhub`. Changing a provider is a config change in `searchhub`, not a
change in either client.

## Adding a service

Add the container to `traefik_net` and give it labels:

```yaml
services:
  foo:
    networks:
      - traefik_net
    labels:
      traefik.enable: "true"
      traefik.http.routers.foo.rule: "Host(`foo.homelab.example.com`)"
      traefik.http.routers.foo.entrypoints: "websecure"
      traefik.http.routers.foo.tls.certresolver: "cloudflare"
      traefik.http.services.foo.loadbalancer.server.port: "80"

networks:
  traefik_net:
    name: traefik_net
    external: true
```

If the backend serves HTTPS with a self-signed certificate:

```yaml
      traefik.http.services.foo.loadbalancer.server.port: "9443"
      traefik.http.services.foo.loadbalancer.server.scheme: "https"
      traefik.http.serversTransports.foo-transport.insecureSkipVerify: "true"
      traefik.http.services.foo.loadbalancer.serversTransport: "foo-transport"
```

## Deploying

```sh
cp .env.example .env        # per-stack; fill in the values the stack needs
./deploy.sh                 # all stacks
./deploy.sh searchhub       # one stack, plus the shared networks
```

`deploy.sh` is a convenience wrapper: it creates the two external networks,
finds each top-level stack's compose file and runs `docker compose up -d
--build`. `open-webui/org-mcp` and `open-webui/oikb` are separate compose
projects; start them from their own directories after the parent stack. It does
not manage secrets. Stacks that need credentials read them from environment
variables or an untracked `.env` file; the tracked `.env.example` and
`.env.template` files document the shape.

## Repository layout

```text
traefik/                 ingress, TLS, dynamic config
pihole/                  DNS and ad blocking
open-webui/              Open WebUI, Open Terminal, oikb, org-mcp
searchhub/ crw/ searxng/ search stack
eval/                    fetch-provider benchmark
seafile/                 file sync with rclone-backed block storage
dawarich/                location history
TandoorRecipes/          recipes
ArchiveBox/              web archive
changedetectionio/       page change monitor
wallabag/                read-later
transmission-openvpn/    VPN-bound torrent client
whisper-transcriber-telegram-bot/
rota/ proxypool/         proxy pool tooling
portainer/ watchtower/   container management
```

## Public snapshot

This repository is a trimmed copy of a private homelab repository. Removed or
replaced before publication:

- credentials, tokens, private keys and `.env` files;
- personal hostnames, usernames, IP addresses and absolute paths;
- private notes and agent logs;
- personal media-management and note-import services;
- scraped benchmark corpora that were derived from private browsing data.

The remaining `.env.example` files are documentation, not working secrets.
Anything that looks like a real endpoint uses the reserved `example.com` domain.

## License

No license is declared. The configuration is published as a reference; adapt it
for your own lab.
