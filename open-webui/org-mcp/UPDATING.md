# Updating the org-mcp sidecar

An update is a deliberate change to one or more of: the Debian base, Emacs,
`mcp-server-lib`, `org-mcp`, `mcp-proxy`, the init wrapper, or the git
supervisor. Read the release notes for the two Emacs packages before bumping
either.

## Before changing anything

1. Record the current pins:

   ```sh
   grep -E '^ARG |^FROM ' open-webui/org-mcp/Dockerfile
   docker inspect org-mcp --format '{{.Config.Image}} {{.Image}}'
   ```

2. Check `org-mcp`'s `Package-Requires` and `NEWS`/README for:
   - the Emacs floor;
   - changes to `org-mcp-enable`;
   - changes to tool names or resource URIs;
   - changes to `org-mcp-allowed-files`.

3. Check `mcp-server-lib` for:
   - the stdio script path and installer behaviour;
   - `mcp-server-lib-start`;
   - the `initialize` capability shape.

4. Check `mcp-proxy` for the stdio-child-exit fix. The image pins merge commit
   `ad058cb88c652b08007e1eaec7df090995a42a21` (PR #248); v0.13.0 and earlier
   keep running after the shim exits and return empty `isError` on every
   later tool call.

## Update

1. Bump the relevant `ARG` or `FROM` in `Dockerfile`. Prefer a digest for the
   base image.
2. Rebuild without touching the volume:

   ```sh
   cd open-webui/org-mcp
   docker compose build --no-cache
   docker compose up -d
   ```

3. Watch startup:

   ```sh
   docker compose logs -f --tail=100 org-mcp
   ```

   Expected: clone present (or cloned), daemon up, proxy listening, health
   becoming healthy.

4. Prove the daemon and shim independently:

   ```sh
   docker exec org-mcp emacsclient -e '(emacs-version)'
   docker exec org-mcp test -x /root/.emacs.d/emacs-mcp-stdio.sh
   docker exec org-mcp head -1 /root/.emacs.d/emacs-mcp-stdio.sh
   ```

   The shim must be the Python bridge (`exec python3 ...`).

5. Prove the MCP endpoint:

   ```sh
   docker exec org-mcp sh -c '
     curl -fsS -X POST http://127.0.0.1:8765/mcp \
       -H "Content-Type: application/json" \
       -H "Accept: application/json, text/event-stream" \
       -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},\"clientInfo\":{\"name\":\"update\",\"version\":\"1\"}}}"
   '
   ```

   Require `capabilities.tools` and `capabilities.resources`.

## Test matrix

Run every item. Record pass/fail and any number, not just "works".

| Test | How | Expected |
|---|---|---|
| Tools list | `tools/list` through the shim or a client | The seven modifying tools and the read tools |
| Resources advertised | `initialize` | `capabilities.resources` present |
| Resource read | `resources/read` `org-outline:///org/inbox.org` | Outline, not an error |
| Read tool | `org-read-outline` on `/org/inbox.org` | Same content |
| Add | `org-add-todo` to `inbox.org` | One commit, pushed, only that change |
| Edit body | `org-edit-body` on the new node | One commit |
| Refile | move it to `actionable.org` | One commit touching both files |
| Archive | archive it | One commit touching source and archive |
| `.org_archive` | read/write a file in `archive/` | Accepted, or record the rejection |
| Grep cost | unqualified `org-grep` with `external/` in scope | Record seconds |
| Agenda day | `org-get-agenda` `day` | ~3 s; no archive/external items |
| Agenda week/month | `org-get-agenda` `week`, `month` | Timeline works; `start_day` matches the requested date |
| Agenda pagination | two `week` calls with different `date` values | Different `start_day` and item sets; no cursor |
| Agenda filters | `n`, `w`, `a` | `n` NEXT only; `w` `projects.org` only; `a` all active states |
| Pull | dirty tree, wait for the 60 s tick | Pull skipped and logged |
| Rebase pull | clean tree with a remote commit | Rebase succeeds, no merge commit |
| Shim death | kill the Python shim inside the container | Proxy exits; Docker restarts the container |
| Local opencode | `opencode mcp list` | `org` connected over local stdio, no sidecar |
| Restart | `docker compose restart` | Reconnects without manual steps |

`org-add-todo` requires `tags` to be an array; an empty string is rejected.
Use `"tags": []`.

## Rollback

1. Restore the previous `ARG`/`FROM` values.
2. `docker compose build --no-cache && docker compose up -d`.
3. Keep the `org-data` volume. Do not delete it to "fix" a code problem.
4. If the clone is corrupt, clone it aside and inspect before replacing the
   volume; the origin still has the history.

## Credential rotation

### Transport credentials

There are none. The sidecar is reachable only on `notes_net`, from Open WebUI.
If a route is ever restored, use the Traefik route with a Tailscale allowlist
and a secret manager for credentials; do not put a static password in `.env`.

### Deploy key

1. Generate a new key on the host.
2. Add the public key to `your-org/org` with write access.
3. Replace the bind-mounted private key, then restart the container.
4. Remove the old deploy key from GitHub after a successful fetch.

## Networking notes

There is no published host port and no Traefik route. Open WebUI uses the
internal service name `org-mcp` on `notes_net`. Verify the container directly:

```sh
docker compose exec -T org-mcp curl -fsS http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'
```

The early `UNABLE_TO_VERIFY_LEAF_SIGNATURE` seen during the first deployment
belonged to a Traefik route that has since been removed. It was the
pre-issuance default certificate, never a missing root.
