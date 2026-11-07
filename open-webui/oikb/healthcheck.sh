#!/bin/sh
# Healthy means: the loop touched its heartbeat within the last 45 minutes.
# One cycle is 2 minutes plus up to 30 minutes for a sync, so 45 minutes
# tolerates one long cycle without flapping.
set -eu

heartbeat=/config/.heartbeat
max="${OIKB_HEARTBEAT_MAX:-2700}"

[ -f "${heartbeat}" ] || exit 1
age=$(( $(date +%s) - $(stat -c %Y "${heartbeat}") ))
[ "${age}" -lt "${max}" ]
