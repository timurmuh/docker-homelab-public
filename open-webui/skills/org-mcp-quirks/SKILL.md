---
name: org-mcp-quirks
description: Non-obvious org-mcp behavior, tool contracts, client differences, and failure recipes. Load before driving org-mcp from an agent, especially when a write, agenda call, or resource read behaves unexpectedly.
version: 1.3.1
---

# org-mcp tool quirks

Tested 2026-10-28 against org-mcp 1.0.0, mcp-server-lib 0.4.0, and the
homelab sidecar (Emacs 30.1). The Mac runs Emacs 31.1; tool behavior is the
same unless noted. Allow-list section corrected twice on 2026-11-04: the
"hardcoded enumeration" claim was WRONG, and so was the follow-up claim that a
new file shows up without a restart.

This records non-obvious behavior, not the full tool reference.

## What must NOT be written to org

- **Handovers, machine-state notes and per-machine step lists.** These are md
  files on disk, not org notes. Writing them into org was tried on 2026-11-03
  and rejected by the user: they drift, they mix working detail into the
  decision record, and nobody finds them again.
- There are exactly **two** handovers, one per workstation. Not one per
  service, not one per project. Both machines reach the
  homelab over SSH, so an agent works on the homelab from whichever machine it
  is on.
- Do not archive a wrongly-placed note as a fix. Archiving preserves the
  mistake in a second place; the user deletes instead.
- Org holds decisions, findings, open questions, tasks and their context. Not
  plans, specs, implementation walkthroughs, or handovers.

## Surface

- The upstream tools plus `org-git-log` (read-only git history). Use
  `tools/list` for the current set. 19 tools as of org-mcp 1.0.0.
- Resource templates cover whole files, outlines, headlines and stable IDs;
  `resources/templates/list` reports the exact URIs.
- `resources/list` is empty; resources are templates only.
- Open WebUI exposes tools only. OpenCode exposes tools and resource templates.

## URIs

- Whole file: `org-headline:///org/inbox.org` (three slashes).
- Top-level refile parent: `org-headline:///org/actionable.org/` (trailing
  slash).
- Headline: `org-headline:///org/inbox.org#Parent/Child`, URL-encoded.
- Stable ID: `org-id://<uuid>`.
- Write tools create an ID if the target has none.
- `org-headline://` carries the file path. `org-id://` does not; resolve it
  with `org-read-by-id` if the file matters.

## Writes

- Every modifying tool requires user approval before it is called. The tools
  do not enforce this; it is a behavioural rule. There is no dry-run mode.
- Seven modifying tools: `org-add-todo`, `org-update-todo-state`,
  `org-edit-headline`, `org-edit-body`, `org-set-planning`,
  `org-archive-subtree`, `org-refile-headline`. **There is no delete tool** — a
  note written in error cannot be removed through org-mcp, only archived or
  deleted by the user by hand.
- There is no single write funnel. Five use `org-mcp--modify-and-save`;
  archive and refile write explicitly.
- Writes go through `write-region`, not `save-buffer`. `after-save-hook` and
  `git-auto-commit-mode` do not see the normal tool writes.
- `org-add-todo` requires `tags` as an array. **Omitted `tags` fails with
  `Missing required parameter: tags`** — the field is genuinely required, so
  a heading with no tags still needs a value.
- **`tags: []` intermittently fails with `Invalid tag name (must be
  alphanumeric, _, or @): []`.** The empty array arrives as the literal
  string `"[]"`. Client-side serialisation, not a stable contract; seen three
  times in one session then succeeding later with the identical argument.
  WORKAROUND (verified): create the node with any real tag, then strip it with
  `org-edit-headline` + `remove_tags`.
- `org-add-todo` takes `body` directly, so creating a heading with prose is
  one call. Pass `todo_state: ""` for a heading with no keyword.
- **A property drawer written into a body is not a property drawer.** Org only
  recognises the node's property drawer when it sits immediately after the
  heading and planning line. `org-add-todo` always writes its own `:ID:` drawer
  there and then the body, so a `:PROPERTIES: ... :END:` supplied inside `body`
  lands below it and is parsed as a *generic* drawer. The write succeeds, the
  file validates, and `org-read-by-id` returns it — but `org-entry-get` never
  sees those keys. Verified 2026-11-03.
  Consequence: **no org-mcp tool can set a real property.** Properties are
  human-authored only. When an agent needs a field, use a plain body line and
  treat the body as the record.
- **Body text is validated for balanced blocks.** `org-edit-body` rejects a
  `new_body` containing an unclosed `#+BEGIN_SRC` ("Body contains unclosed SRC
  block"). Avoid writing literal block markers in prose.
- `org-tag-alist` and `org-tag-persistent-alist` are both `nil`. No tag
  completion list, no file-scoped tag mechanism; tags are free-form.
  `org-tags-exclude-from-inheritance` is `nil`, so tags inherit everywhere.
- `org-edit-body` needs `old_body` to be unique unless `replace_all` is true.
  Empty `old_body` is only for a node with an empty/whitespace body.
- `org-edit-body` **can reject an `old_body` that matches what
  `org-read-by-id` returned**, especially a long multi-line block whose
  wrapping drifted. Use a SHORT unique anchor — one line or one sentence —
  rather than reproducing a whole paragraph. Seen repeatedly 2026-11-03.
- `org-update-todo-state` and `org-refile-headline` require the expected
  current state/title; a mismatch is a race guard, not a retry hint.
- On a **repeating** entry (a SCHEDULED with a `.+1w`-style cookie),
  `org-update-todo-state` to DONE comes back with `new_state` unchanged: org's
  repeat logic advances the schedule by one interval and updates
  `:LAST_REPEAT:`, but **writes no DONE log entry** even though
  `org-log-repeat` is `time` (observed on two workstations). The schedule also
  advances from the old date, not from today, so
  an overdue repeater stays overdue. Follow up with `org-set-planning` to put
  the next date where you want it, and do not expect the logbook to show it.
- **`org-refile-headline` preserves the node's ID**, so `org-id://` links to a
  moved subtree keep resolving. This makes "stage as parent heading" designs
  safe.
- Refile and archive can touch two files. Both files must be in
  `org-mcp-allowed-files`.
- A visiting buffer with unsaved changes blocks the write. Save it first.
- `.org_archive` is accepted by the allowed list and by the write tools.
- Optional parameters are declared `&optional` in the handler signature; a
  parameter without a default is still required by the JSON schema.

## Allow-list

- `org-mcp-allowed-files` is **generated RECURSIVELY from `org-directory`**, not
  a hardcoded path enumeration. But it is computed **once per Emacs session**
  and cached in that variable. A file created while the daemon is running is
  NOT in the list, and every write to it fails with `the referenced file not in
  allowed list`. Measured on a workstation: 615 entries with a newly created
  `new-note.org` absent although the file was on disk; after a
  daemon restart, 617 entries and the file present. **Restart the daemon after
  adding a file, before concluding anything else.**
- If a file is reported as "not in allowed list", there are exactly two known
  causes, and they look identical from the client: (a) the file was created
  after the daemon started — see above; (b) a **stale path**, where a symlinked
  or lagging org directory makes the reader look at an older tree than the file
  was written into. A stale path was confirmed: `/org/new-note.org`
  did not exist because `/org` was behind the real tree. `/org` also carried
  duplicates of the same note — a second symptom of the
  same lag. Check the daemon's uptime against the file's mtime first; it is the
  cheaper test.
- Because generation is recursive, `external/` IS included: 99 MB and ~982
  tracked files on a workstation. That is a **scanning cost, not a
  permission problem**. Always pass `file` to `org-grep` for routine searches.
  The agenda has its own separate filter, which is why agenda stays fast.
- `org-directory` differs per machine (for example `~/notes/org`).
  Check it before blaming the list.

## Commits

- Multiple writers share one branch (Macs plus the homelab sidecar). Fetch and
  `pull --rebase` before pushing, retry a rejected push once, and abort rather
  than resolve a conflict.
- Mac: `git-auto-commit-mode` only commits; a 60 s timer pushes when the tree is
  clean. The 30 s dirty-tree timer is the commit safety net.
- **Never `git commit` org from a shell on a Mac.** It races the advice and
  produces duplicate or conflicting commits.
- Archive is special: `org-archive-subtree` opens the archive file with
  `find-file-noselect`, which enables `git-auto-commit-mode`; org then saves
  that buffer. Suppress gac inside org-mcp calls or archive produces two
  commits.
- Refile does not trigger gac; it writes both sides through temp buffers.
- Container: `git-sync.sh` serialises git operations; commits every 30 s, then
  fetches, rebases if behind, pushes. Both `--rebase`; a real conflict aborts
  rather than autostashing.
- One transport per directory: do not put a Syncthing replica under a
  git-auto-commit tree. They fight and produce conflict files.

## Agenda

- `org-get-agenda` **ignores `org-agenda-files`**. It builds from
  `org-mcp-allowed-files`, so the two are linked by consequence, not by
  configuration.
- Built-in span views are the timeline: `view: "day"`, `"week"`, `"month"`.
  They accept `date` and are the pagination mechanism.
- There is no cursor. Fetch successive weeks:
  `{"view":"week","date":"2026-11-02"}` then `"2026-11-09"`.
- Custom-command views (`a`, `w`, `n`, `d`, etc.) **ignore `date`**; they
  anchor on today. Do not use them for pagination.
- Structured items expose `title`, `todo`, `priority`, `tags`, `scheduled`,
  `deadline`, and `uri`. There is no source-file field. Tags are local only.
- Routine views return in seconds; `a` is the expensive full dump.
- Archive files are excluded from the agenda scan but remain readable and
  writable through MCP.

## Search, reads, and history

- `org-grep` is a literal substring search, not regex; case-insensitive by
  default.
- With no `file`, it searches every allowed file, including the large
  `external/` tree. Pass `file` for routine searches.
- Read tools read from disk, not from visiting buffers. Unsaved changes are
  invisible.
- `org-grep` and `org-read-by-id` both return `headline_path` — the ancestor
  chain — so a node's parent headings are always available without reading
  them separately.
- `org-git-log` is read-only and can only run `git log`. Needs
  `default-directory` inside the repo; it works from the daemon with no extra
  argument. `file` is optional; `limit` defaults to 20 and caps at 200;
  `patch` and `stat` are mutually exclusive; `since` takes git approxidate and
  is silent on malformed values.

## Clients

- Open WebUI: MCP tools are admin-only, Streamable HTTP only, and a new tool
  appears only in **new chats**. Existing chats keep their old tool list.
- Open WebUI exposes MCP tools, not resources. Use the read tools.
- Open WebUI connection id is `server:mcp:org-mcp`; internal URL
  `http://org-mcp:8765/mcp`.
- OpenCode v2 uses `mcp.servers`, not a top-level `mcp` object. It uses a
  **local** stdio server against the Mac Emacs daemon, not the sidecar.
- OpenCode sees resource templates and can read them.
- The local entry uses `python3
  ~/.config/opencode/bin/emacs-mcp-stdio.py --server-id=org-mcp
  --init-function=my/org-mcp-start`. The Mac Emacs daemon must be running.
- This skill lives in the HOMELAB REPO and propagates from there to Open WebUI
  and to the local OpenCode config. OpenCode cannot fetch a skill from Open
  WebUI, so an edit made through one surface is incomplete until it lands in
  the repo.

## Machine traps (Mac)

- `doom sync` re-tangles `config.org` into `config.el`. Edit `config.org`,
  never `config.el` alone.
- `doom sync` prompts `y/n` when the Emacs version changed; with no stdin it
  dies with `end-of-file`. Always pass `-!`.
- Emacs installed as a brew cask: `brew upgrade` replaces it while the daemon
  keeps running the unlinked binary. Restart the daemon afterwards.
- `syncthing cli config ...` talks to `127.0.0.1:8384` and fails when the app
  is not running. Start the app first.

## Server / sidecar

- `org-mcp-enable` only registers the server. `mcp-server-lib-start` must also
  run. The sidecar wrapper calls both.
- Adding a tool to an existing server uses
  `mcp-server-lib-register-server :id "org-mcp" :tools (...)`. Registering the
  same tool id twice keeps the first spec and only bumps its reference count,
  so a changed handler or signature needs an unregister first.
- `mcp-proxy` is pinned to merge commit
  `ad058cb88c652b08007e1eaec7df090995a42a21` (PR #248). v0.13.0 predates the
  fix that makes the proxy exit when the stdio child exits.
- The sidecar shim is the Python bridge at
  `/root/.emacs.d/emacs-mcp-stdio.py`; it returns JSON-RPC errors instead of
  dying on an `emacsclient` failure.
- The proxy listens on `8765` on the internal compose network. No published
  host port and no public route.

## Common failures

| Symptom | Cause |
|---|---|
| `Missing required parameter: tags` | `tags` omitted; even an untagged heading needs a value |
| `Invalid tag name ...: []` | `tags: []` arrived as the literal string `"[]"`. Workaround: create with a real tag then `remove_tags` |
| `Invalid tag name ...: ` (empty) | passed `tags: ""` instead of an array |
| `Missing required parameter: <name>` | the handler lacks `&optional`, so the schema marks it required |
| `current_title`/`current_state` mismatch | stale read; re-read the node |
| `Body contains unclosed SRC block` | `new_body` has a literal `#+BEGIN_SRC` without `#+END_SRC`; avoid block markers in prose |
| `org-edit-body` rejects a correct `old_body` | long block with drifted wrapping; retry with a SHORT unique anchor |
| write refused, unsaved changes | save the visiting buffer first |
| archive/refile cannot find a file | archive or target not in `org-mcp-allowed-files` |
| a file reports "not in allowed list" | usually a STALE org path (symlink or lagging mount), not the generator |
| agent-set property invisible to `org-entry-get` | the drawer was written into the body and parsed as generic; use a plain body line |
| need to delete a note written in error | there is no delete tool; ask the user to remove it by hand |
| `resources/list` empty | use `resources/templates/list` |
| agenda unexpectedly slow | external files included in the scan; use a narrower custom view |
| custom agenda view ignores date | custom commands anchor on today |
| every tool call returns empty `isError: true` | mcp-proxy stdio child died; pin past PR #248 or restart the container |
| local opencode has no org tools | Mac Emacs daemon is not running |
| Open WebUI tool missing in old chat | start a new chat |
| `org-git-log` returns nothing | wrong `default-directory`, or a shallow clone |
