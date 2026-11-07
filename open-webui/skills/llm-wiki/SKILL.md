---
name: llm-wiki
description: Curate a research wiki stored in org-mode files through org-mcp tools. Use when saving research, asking what was concluded earlier, asking what changed, running a curation pass, or before implementing something that may have been researched before — check for prior decisions and rejected options.
version: 1.3.2
---

# LLM Wiki

Maintains a curated wiki of research as org-mode headings. The model authors
and updates it; the user approves and directs. It does not do web research.

Storage is one git repository, cloned on the Mac and in the homelab sidecar.
Everything is read and written through the org-mcp tools. There is no note
store, no mirror, and no terminal in the loop.

Paths in tool arguments differ by host. Call `org-get-allowed-files` for the
exact paths on whichever host you are talking to; do not assume them.

**The whole repository is the wiki.** Every heading the assistant writes is in
scope. `inbox.org` holds unsorted material; `reference.org` holds reference
material; `actionable.org` holds tasks. Nothing is a "page" that must be
registered anywhere first.

## The system in one minute

- **Source of truth:** the org git repository. Git history is the change log.
- **Reads:** `org-read-outline`, `org-read-headline`, `org-read-file`,
  `org-read-by-id`, `org-grep`.
- **History:** `org-git-log` — how a note reached its current state, and what
  changed in it. Use `since` for a date range and `stat` for a file summary
  instead of a diff. There is no separate log file; do not create one.
- **Writes:** `org-add-todo`, `org-edit-headline`, `org-edit-body`,
  `org-update-todo-state`, `org-set-planning`, `org-refile-headline`,
  `org-archive-subtree`.
- **Commits:** every write is committed and pushed for you, on the Mac by
  advice on the tool handlers, in the sidecar by `git-sync.sh`. Never commit
  org yourself from a shell; you would race the writers.
- **Clients:** Open WebUI sees tools only and picks up a new tool only in a
  new chat. opencode sees tools and resource templates. Read with tools in
  both; do not assume `org-outline://…` works in Open WebUI.

## When to Use

- "Let's save this research."
- A thread has produced findings worth keeping past this chat.
- The user asks what we concluded or decided earlier.
- The user asks what changed recently.
- A recurring topic resurfaces and earlier work should be found.
- "Run the curation pass" / "lint the wiki".

## File map

| File | Holds |
|---|---|
| `inbox.org` | unsorted capture; prose that has no home yet |
| `backlog.org` | migrated notes awaiting per-item processing (temporary) |
| `actionable.org` | tasks and projects |
| `reference.org` | durable reference material, people, links |
| `calendar.org` | dates and events |
| `topics.org` | topic material |
| `projects.org` | project material; excluded from most agenda views |
| `archive/` | archived subtrees, one `*_archive` file per source |
| `external/` | imported material (Logseq exports, chat exports, course notes) |

## Reading

Cheap to expensive; stop as soon as the question is answered.

1. **Orient:** `org-read-outline` on one file when you do not know the
   structure. Do not read a whole file to find one heading.
2. **One subtree:** `org-read-headline` with an `org-headline://` URI.
3. **Search:** `org-grep` with `file` set. Unscoped `org-grep` scans the
   whole tree and takes seconds; it is only for a genuine global sweep.
4. **Whole file:** `org-read-file`, only after 1–3 failed.
5. **History:** `org-git-log` with `file` when the question is "when did this
   change" or "what did it say before". Add `patch: true` for the diff.

A retrieved source whose filename ends in `_archive` is historical. It was
closed or cancelled, or it is a note that has been superseded. Never present
an archived item as live or outstanding without checking the live file first.

Reads come from disk, not from Emacs buffers. Unsaved changes are invisible.

## What belongs here

Org holds high-level material: decisions, findings, open questions, reference
notes, tasks and their context. It does not hold project working detail — no
code plans, no specs, no implementation walkthroughs, no file-by-file designs.
Those live with the code, in the repository they describe. If a thread produces
both, keep the decision and the reasoning in org and leave the plan where it
was written.

The test: would this still be worth reading after the code has changed twice?
If not, it is working detail.

## Writing

**Author with tools, not with `write`.** A shell write bypasses the commit
advice and the allowed-file checks.

**Ask before the first write.** Reads are free; writes are not. Before calling
any modifying tool, say what you are about to write and where, and wait for
approval. Approval covers that write, not the session — a second, unrelated
write needs its own approval. This applies to `org-add-todo`,
`org-edit-body`, `org-update-todo-state`, `org-set-planning`,
`org-edit-headline`, `org-refile-headline` and `org-archive-subtree` alike.

- **A prose note** is one `org-add-todo` call with `todo_state: ""`,
  `title` = the note's subject, `body` = the content, `parent_uri` = the
  target file's top level. `tags: []`.
- **An edit** is `org-edit-body` with a unique `old_body`. Whole-body replace
  is destructive; anything omitted is gone. Re-read first if the content may
  have changed.
- **A move** is `org-refile-headline`. It can touch two files.
- **Removal** is `org-archive-subtree` to `archive/`, never a delete.
- A write fails if a buffer visiting the file has unsaved changes, or if a
  `current_title`/`current_state` guard is stale. Re-read and retry once, then
  report.

One writer at a time. The Mac and the sidecar both write to the same branch;
do not start a bulk edit while the other side is mid-edit. If a rebase
conflict is reported, stop and tell the user — do not resolve it yourself.

## Agenda

- Built-in spans `day`, `week`, `month` accept `date` and are the only
  pagination mechanism.
- Custom views **ignore `date`** and anchor on today. The set differs by
  surface; call `org-get-agenda` with an unknown key and it lists the
  available ones.
- `a` is the slowest view — the full dump. Prefer a narrower view for
  routine calls.
- `external/` and `archive/` are excluded from the agenda but fully readable.

## Conventions

- Heading titles: `<Domain> — <Topic>`, for example
  `Example domain — Topic`.
- Anything the user has not approved carries the marker
  `unconfirmed by the user` in the body, exactly that phrasing. Remove it only
  when the user confirms.
- Preserve the user's wording when migrating existing material. Do not tidy on
  the way in; tidying is a separate pass with approval.
- Dates in headings use org's inactive timestamp form when the source had a
  date.

## Archive and history mining

Only when the durable layer misses, in this order:

1. `org-grep` across the live files.
2. `org-grep` with `file` set to an `archive/` file.
3. `org-git-log` for a heading or file that used to exist.
4. Archived chats: `search_chats` with `archived:true <topic>`.

Offer the last rung rather than taking it: "I can also look through archived
chats, want me to?"

## Pitfalls

- `org-add-todo` rejects `tags: ""`; pass `[]`.
- A parameter without `&optional` in the handler is required by the schema,
  even if the description calls it optional.
- `org-git-log` rejects paths outside the org directory, including absolute
  paths that merely look similar.
- Open WebUI exposes no resources; use read tools there.
- A tool added mid-session is invisible in an existing Open WebUI chat.
