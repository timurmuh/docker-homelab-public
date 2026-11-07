# oikb

Uploads an org-mode git clone into one or more Open WebUI knowledge bases,
incrementally. It is the only thing that runs `oikb sync`; see `entrypoint.sh`
for why.

## Why a container

`oikb` takes no lock, and its sync order is scan → diff → cleanup (deletes stale
files) → upload. Two runners would delete each other's uploads. On a workstation
this is usually guaranteed by a single-user launch agent; here it is guaranteed
by one long-running container with one loop.

## Layout

- `.oikb.yaml` — the knowledge-base entries and their filters. Copy the example
  and replace the `kb-id` values with the IDs from your Open WebUI instance.
  No credentials belong in this file.
- `config/` — host-only, git-ignored: `config.yaml` with the Open WebUI URL and
  API token, plus `history.db`.
- `/org` — the `org-data` volume, mounted **read-only**.

`config.yaml`:

```yaml
token: <Open WebUI API key>
url: http://open-webui:8080
```

The internal URL avoids Traefik entirely.

## Filters

Each entry is self-contained. `oikb` deep-merges entries by `kb-id`, and lists
are **replaced**, not concatenated, when both sides define the same key. Relying
on `defaults.filter` would silently drop one entry's filters, so every entry
spells out its own.

Exclude lists also handle a real Open WebUI behaviour: it refuses a file when
the hash of its extracted text already exists in the same collection from a
different file. Byte-identical copies (for example `note.org` and
`note_2.org`) therefore need one canonical copy plus an explicit exclude for
the rest, or `oikb` retries the duplicate forever. Empty files are excluded for
the same reason: `oikb` skips them at upload time, so they would otherwise show
up as a permanent phantom diff.

## Two guards

**One runner.** The container holds `flock` on `/config/oikb.lock` for its whole
life. A second container refuses to start rather than deleting the first one's
uploads. `container_name` alone is only a naming convention.

**A ready clone.** Before every sync the loop checks, and skips on any doubt:

- `git` exists in the image;
- `/org/.git` exists and `/org` is the clone's top level;
- `origin/main` resolves and `HEAD` is not behind it;
- the worktree is clean;
- there are at least 10 `.org` files, which catches an empty volume.

Every failure mode is "skip and say why". The check used to fail open: without a
`git` binary, `rev-list` failed, the `|| echo 0` turned that into "not behind",
and the loop synced a tree it could not vouch for while `sync_cleanup` deleted
files. `git` is now installed for that reason.

## Health

The loop touches `/config/.heartbeat` at the top of every iteration, and the
image's `HEALTHCHECK` fails when that file is older than 45 minutes. The
heartbeat is written before the readiness check, so a loop that is correctly
skipping still reports healthy; only a wedged loop goes unhealthy.

`OIKB_INTERVAL` is validated at start: a non-numeric value logs a warning and
falls back to 120 seconds instead of crash-looping. `SIGTERM` waits for a
running sync to finish, so `docker compose restart` cannot kill a sync between
`sync_cleanup` and upload.

## Operations

```sh
docker compose up -d --build
docker compose logs -f --tail=50
docker compose restart
```

A healthy cycle logs `oikb: sync start`, the per-KB diff, and `oikb: sync ok`.
A second run with no changes reports no uploads.

Never run `oikb sync` by hand while the loop is running, not even through
`docker exec`. To reproduce a report, stop the container first.

## Cost

Steady state is near zero: `oikb` uploads only files whose content changed, so a
quiet day embeds nothing.

Two operations are expensive:

- **A full reindex re-embeds the whole corpus.** Changing `rag.chunk_size`,
  `rag.chunk_overlap`, the tokenizer, or the embedding model makes every file a
  new embedding job. Treat it as a deliberate, budgeted action.
- **Repeated uploads are not free.** A file that `oikb` retries is extracted and
  embedded each time.

Embeddings are billed by whichever provider Open WebUI is configured to use.
Check the model and chunk settings before a reindex, and use a multilingual
model if the corpus is multilingual.

## What it does not do

- It does not write to the org repository. Changes come from the editor or the
  `org-mcp` sidecar and reach this container through the shared volume.
- It does not touch the wiki log or agent notes; it only uploads files the
  filters allow.
