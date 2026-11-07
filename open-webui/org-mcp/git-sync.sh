#!/bin/sh
# Serialise git operations for the clone: commit MCP writes, push them, and
# pull only when the tree is clean.  org-mcp has no single write funnel, so
# this loop is the container's commit path rather than an Emacs save hook.
# The Mac may also commit/push, so the clean pull is a rebase.
set -u

cd /org || exit 1

n=0
while :; do
  sleep 30
  n=$((n + 1))

  (
    flock -n 9 || exit 0

    if [ -n "$(git status --porcelain)" ]; then
      git add -A
      if git commit -m "org: auto-commit $(date '+%Y-%m-%d %H:%M:%S')"; then
        :
      else
        echo "org-mcp: commit failed" >&2
      fi
    fi

    # Integrate before pushing.  The Mac writes the same branch, so a blind
    # push is rejected whenever it commits between our fetch and our push.
    # Rebase, never merge; abort rather than resolve a conflict.
    if [ "$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)" -gt 0 ]; then
      git fetch --quiet origin || echo "org-mcp: fetch failed" >&2
      if [ -z "$(git status --porcelain)" ] \
         && [ "$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)" -gt 0 ]; then
        if ! git pull --rebase --quiet; then
          echo "org-mcp: rebase conflict; aborting, commits kept locally" >&2
          git rebase --abort 2>/dev/null || true
        fi
      fi
      git push origin main || echo "org-mcp: push failed" >&2
    fi

    # Pull every other 30 s tick (60 s), and only over a clean tree.  Rebase
    # is deliberate: the Mac commits to the same branch, so a plain merge
    # would turn ordinary divergence into merge commits.  No autostash: commit
    # first, then pull.  A real content conflict aborts the rebase and leaves
    # the local history intact for the next cycle.
    if [ $((n % 2)) -eq 0 ] && [ -z "$(git status --porcelain)" ]; then
      echo "org-mcp: pulling (rebase)"
      if ! git pull --rebase; then
        echo "org-mcp: pull/rebase failed; aborting rebase" >&2
        git rebase --abort 2>/dev/null || true
      fi
    elif [ $((n % 2)) -eq 0 ]; then
      echo "org-mcp: pull skipped, worktree dirty" >&2
    fi
  ) 9>/org/.git/org-mcp.lock
done
