#!/bin/sh
# The only process that runs `oikb sync`.
#
# oikb takes no lock.  Its sequence is scan -> /sync/diff -> sync_cleanup
# (DELETES stale files) -> mkdir -> upload, so two concurrent syncs against
# one KB can delete each other's uploads.  Two defences: the flock below, and
# a compose project that starts exactly one container.
#
# The clone is shared with the org-mcp sidecar, which fetches, rebases and
# commits to it.  A sync against a clone that is behind origin/main, mid-pull,
# or mid-write would present live files as stale and delete them from the KB.
# Every readiness check below therefore fails closed: on any doubt, skip.
set -eu

interval="${OIKB_INTERVAL:-120}"
case "${interval}" in
    '' | *[!0-9]*)
        echo "oikb: OIKB_INTERVAL='${interval}' is not a whole number; using 120" >&2
        interval=120
        ;;
esac
[ "${interval}" -gt 0 ] || interval=120

lock=/config/oikb.lock
heartbeat=/config/.heartbeat

# One runner, enforced rather than documented.  Held for the process lifetime.
exec 9>"${lock}"
if ! flock -n 9; then
    echo "oikb: another instance holds ${lock}; exiting" >&2
    exit 1
fi

child=
stop() {
    # Do not kill a sync between cleanup and upload; let it finish.
    echo "oikb: stopping" >&2
    if [ -n "${child}" ]; then
        wait "${child}" 2>/dev/null || true
    fi
    exit 0
}
trap stop TERM INT

check_ready() {
    command -v git >/dev/null 2>&1 || { echo "git is not installed"; return 1; }
    [ -d /org/.git ] || { echo "/org is not a git clone"; return 1; }
    toplevel=$(git -C /org rev-parse --show-toplevel 2>/dev/null) \
        || { echo "cannot read the clone"; return 1; }
    [ "${toplevel}" = "/org" ] || { echo "clone root is ${toplevel}, expected /org"; return 1; }
    git -C /org rev-parse --verify --quiet origin/main >/dev/null \
        || { echo "origin/main does not resolve"; return 1; }
    behind=$(git -C /org rev-list --count HEAD..origin/main 2>/dev/null) \
        || { echo "cannot compare HEAD with origin/main"; return 1; }
    [ "${behind}" = "0" ] || { echo "clone is ${behind} commit(s) behind origin/main"; return 1; }
    [ -z "$(git -C /org status --porcelain)" ] \
        || { echo "clone has uncommitted changes"; return 1; }
    count=$(find /org -name '*.org' -not -path '*/.git/*' | wc -l)
    [ "${count}" -ge 10 ] || { echo "only ${count} .org files under /org"; return 1; }
    return 0
}

echo "oikb: starting; source /org; interval ${interval}s"
while true; do
    # The heartbeat proves the loop is alive even when every cycle skips.
    touch "${heartbeat}"
    started=$(date -u +%FT%TZ)
    if reason=$(check_ready); then
        echo "oikb: sync start ${started}"
        # A hung HTTP call must not wedge the loop forever.
        timeout 1800 oikb sync &
        child=$!
        if wait "${child}"; then
            echo "oikb: sync ok $(date -u +%FT%TZ)"
        else
            status=$?
            echo "oikb: sync FAILED rc=${status} $(date -u +%FT%TZ)"
        fi
        child=
    else
        echo "oikb: skipping ${started}: ${reason}"
    fi
    sleep "${interval}" &
    wait $! || true
done
