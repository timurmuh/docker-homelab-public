# Open WebUI stack

This compose project contains two things:

| Service | Purpose | Exposure |
|---|---|---|
| `openwebui` | The Open WebUI application. | Traefik: `openwebui.homelab.example.com` |
| `open-terminal` | Shell for the assistant, with `/work` writable. | None; `notes_net` only |

```
Traefik ──> openwebui ──service name──> open-terminal
```

## Open Terminal

An alternative to the built-in code execution tool, which Open WebUI disables
when the terminal is enabled. It has a persistent `/work` volume and a
persistent home, so files survive a container restart.

It is reachable only from `openwebui` over `notes_net`, at
`http://open-terminal:8000`. It has no Traefik labels and no published ports.

## Deploy

```bash
cd open-webui
umask 077
printf 'OPEN_TERMINAL_API_KEY=%s\nNOTES_UID=1000\nNOTES_GID=1000\n' \
  "$(openssl rand -hex 32)" > .env
docker compose up -d --build
```

`.env.example` documents the variables. `OPEN_TERMINAL_API_KEY` is required; compose
refuses to start without it.

Then connect the terminal in Open WebUI: **Settings → Integrations → Open
Terminal**, URL `http://open-terminal:8000`, key from `.env`. Enabling it
disables the Code Interpreter, which is intended.

`NOTES_UID` / `NOTES_GID` must match the uid/gid inside the Open Terminal image
(usually `1000`). The one-shot `open-terminal-init` service fixes volume
ownership before the terminal starts.

## Notes

The Open WebUI notes feature is disabled (`notes.enable=false`). Notes were
migrated to `your-org/org` and live there; `org-mcp`, in `org-mcp/`, is the
write path.

## Exposure

`open-terminal` deliberately has no Traefik labels and no published ports. It is
reachable only from `openwebui` over the private `notes_net`. Do not add a
router for a shell.
