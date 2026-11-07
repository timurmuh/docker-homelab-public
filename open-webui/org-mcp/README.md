# org-mcp sidecar

An Emacs daemon serving [`org-mcp`](https://github.com/laurynas-biveinis/org-mcp)
over MCP, with a git clone of `your-org/org` on a named volume.

This is a **server**, not an editor. It deliberately has no Doom, no
blank-line normaliser, and no `git-auto-commit-mode`.

## Why it looks like this

### Emacs daemon + `mcp-server-lib`

`org-mcp` is an Emacs package. `mcp-server-lib` runs the MCP server inside a
running Emacs and exposes a stdio shim (`emacs-mcp-stdio.sh`) for clients.
The shim talks to the daemon through `emacsclient`, so the daemon must exist
before the first MCP request.

`org-mcp-enable` only **registers** tools and resources. It does not call
`mcp-server-lib-start`. `init.el` therefore defines `my/org-mcp-start`, which
does both. The proxy passes that function as `--init-function`.

The upstream bash shim uses `set -e`, so one failed `emacsclient` kills the
bridge. The image replaces it with `emacs-mcp-stdio.py`: failed calls become
JSON-RPC errors, timeouts are bounded, and the process stays alive. It still
exits on stdin EOF; combined with the pinned proxy, that makes Docker restart
the sidecar instead of leaving a silently broken endpoint.

### `mcp-proxy`, not `mcpo`

`mcp-proxy` forwards resources as well as tools, which keeps the sidecar
useful to clients that understand resources. Open WebUI exposes tools only,
so `mcpo` would also be sufficient for that one client; it remains the
fallback if `mcp-proxy` proves unstable.

The image pins `mcp-proxy` to the merge commit for PR #248, not v0.13.0.
v0.13.0 predates the fix that makes the proxy exit when its stdio child
exits; without it the proxy stays alive and every tool call returns an empty
`isError`.

### Debian trixie

The pinned `org-mcp` requires Emacs 28.2. Bookworm sits exactly on that floor;
trixie ships a newer Emacs. Building on trixie avoids running the oldest
supported Emacs for no reason and gives a newer Org.

### Pinned versions

The image pins its dependencies as `ARG`s at the top of the Dockerfile:
`MCP_SERVER_LIB_VERSION`, `ORG_MCP_VERSION`, `MCP_PROXY_REF`, and the Debian
base tag. Read the versions there; do not trust a copy in prose.

`mcp-proxy` is pinned past PR #248, as above. Do not use `latest` or
`uvx @latest` at runtime. An upstream change to `org-mcp-enable`, the stdio
script path, or the resource API should be a deliberate update, not a surprise
restart.

### Git is a clone, not the origin

The container holds a clone on the `org-data` volume. The origin remains
GitHub. A write-capable deploy key is mounted read-only at
`/run/secrets/org_deploy_key`; it is never baked into the image.

Because `org-mcp` has no single write funnel (five tools use
`org-mcp--modify-and-save`; archive and refile write explicitly), the
container does **not** hook Emacs save events. `git-sync.sh` serialises git
operations for the clone:

- every 30 s, commit dirty changes and push;
- every 60 s (every second tick), if the tree is clean, `git pull --rebase`;
- pull aborts, with a log line, when the tree is dirty; it never autostashes.

The Mac commits to the same branch, so rebase is deliberate: it keeps
ordinary divergence from becoming merge commits. A real content conflict
aborts the rebase and leaves local history intact for the next cycle.

`flock` serialises the loop so a pull cannot race a commit.

### Networking

The service joins one network:

- `notes_net` — Open WebUI reaches it at `http://org-mcp:8765/mcp`, with no
  TLS and no auth.

There is no published host port and no Traefik route. The service is a private
implementation detail of Open WebUI; an endpoint that can write every note,
guarded by a shared password, is worse than no endpoint at all. opencode uses
a local stdio MCP server against the Mac Emacs daemon, not this sidecar.

### `org-mcp-allowed-files`

Generated at startup from the clone:

```sh
find /org -type f \( -name '*.org' -o -name '*.org_archive' \) \
  -not -path '*/.git/*' -not -path '*/.attach/*' \
  -not -path '*/.stversions/*' -not -path '*/.stfolder/*'
```

Everything in the clone is writable, including `external/` (ID properties
are added there for id-linking) and the archives. `.attach/` is excluded as
binary grep noise; `.git/` and Syncthing leftovers are not org content.
Repository agent notes stay on disk but are deliberately **not** in this list:
they are not Org files, and agents read the rules from the skills rather than
through MCP.

### Agenda

`org-get-agenda` does **not** use `org-agenda-files`; it builds from
`org-mcp-allowed-files`. `init.el` therefore advises
`org-mcp--agenda-allowed-file-list` to drop `/org/external/`,
`/org/archive/`, and any name ending in `_archive`. External files remain
readable and writable through MCP; only the agenda scan skips them. The
resulting scope is every top-level `.org` file; adding one extends the scan
and needs no edit here.

`projects.org` is also in `org-agenda-files` for consistency.

Fixed views:

| Key | View |
|---|---|
| `d` | Today & Next |
| `n` | NEXT |
| `t` | TODO |
| `l` | TICKLE |
| `r` | RESEARCH |
| `i` | IDEA |
| `p` | PROJ |
| `a` | all active states |
| `w` | `projects.org` only |

The timeline is the built-in `day`/`week`/`month` span. Those accept a `date`
and are the pagination mechanism; custom views ignore `date` and always
anchor on today. Fetch multiple weeks with successive `week` calls.

Routine views return in seconds; `a` is the expensive full dump. The cost is
Org’s agenda build, not file count; prefer `n`, `d`, or `w` for routine calls.

Items expose `tags`, `scheduled`, `deadline`, `todo`, `priority`, and `uri`.
There is no source-file field; `org-headline://` URIs carry the path, while
`org-id://` URIs need `org-read-by-id` to resolve the file.

## Credentials

### Transport auth

There is no transport credential. `open-webui/org-mcp/.env` (gitignored,
mode 600) holds only the optional deploy-key path override.

### Deploy key

Public key: `/home/homelab/org-mcp/deploy_key.pub`. It must be added
to `your-org/org` as a **write-capable** deploy key. The private key stays on
the host at mode 600 and is bind-mounted read-only.

## Operations

```sh
cd open-webui/org-mcp
docker compose up -d --build
docker compose logs -f --tail=100
docker compose ps
docker compose restart
```

Healthcheck: posts an MCP `initialize` to `127.0.0.1:8765/mcp` and requires a
`result` field. A live process with a stopped Emacs daemon fails it.

Logs to inspect:

- `docker compose logs org-mcp` — entrypoint, proxy, git loop;
- `docker exec org-mcp emacsclient -e '(emacs-version)'` — daemon;
- `docker exec org-mcp sh -c 'cd /org && git status --short'` — clone;
- `docker exec org-mcp tail -50 /root/.emacs.d/emacs-mcp-stdio.log` only if
  `EMACS_MCP_DEBUG_LOG` is enabled; it is off by default.

## Known assumptions to re-test

- `.org_archive` is accepted by `org-mcp-allowed-files` and by the write
  tools.
- `initialize` advertises `capabilities.resources`.
- An unqualified `org-grep` across `external/` is acceptable, or agents must
  pass `file`.
- `org-get-agenda` stays bounded to the top-level files; `a` remains the
  expensive full dump.

See `UPDATING.md` for the test matrix.
