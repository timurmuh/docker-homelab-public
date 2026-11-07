#!/usr/bin/env bash
set -euo pipefail

# Deploy the homelab stacks.
#
# This is a convenience wrapper, not a production orchestrator. It creates the
# shared external networks, then runs `docker compose up -d --build` for each
# requested stack. Compose owns the rest: dependencies, healthchecks, labels.
#
# Usage:
#   ./deploy.sh                 deploy every stack in SERVICES
#   ./deploy.sh searchhub crw   deploy only the named stacks

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$SCRIPT_DIR"

SERVICES=(
    traefik
    pihole
    portainer
    changedetectionio
    open-webui
    searxng
    crw
    searchhub
    seafile
    wallabag
    dawarich
    ArchiveBox
    TandoorRecipes
    transmission-openvpn
    whisper-transcriber-telegram-bot
    watchtower
    rota
)

if [ "$#" -gt 0 ]; then
    ALL=("${SERVICES[@]}")
    SELECTED=()
    for want in "$@"; do
        found=0
        for known in "${ALL[@]}"; do
            [ "$want" = "$known" ] && found=1
        done
        if [ "$found" -eq 0 ]; then
            echo "unknown stack: $want" >&2
            printf 'known stacks: %s\n' "${ALL[*]}" >&2
            exit 2
        fi
        SELECTED+=("$want")
    done
    SERVICES=("${SELECTED[@]}")
fi

if ! command -v docker > /dev/null 2>&1; then
    echo "docker is not on PATH" >&2
    exit 1
fi
if ! docker compose version > /dev/null 2>&1; then
    echo "docker compose is not available" >&2
    exit 1
fi

echo ">>> Ensuring shared networks"
docker network create traefik_net > /dev/null 2>&1 || true
# search_net is shared by searxng, crw and searchhub. None of their compose
# files may create it: they all declare it external.
docker network create search_net > /dev/null 2>&1 || true

FAILED=()

for service in "${SERVICES[@]}"; do
    compose=$(find "$service" -maxdepth 1 \( -name 'docker-compose.yml' -o -name 'docker-compose.yaml' \) -print -quit 2>/dev/null || true)
    if [ -z "$compose" ]; then
        echo "--- $service: no compose file, skipping"
        continue
    fi

    echo "--- Deploying $service ($compose)"
    if docker compose -f "$compose" up -d --build; then
        echo "    $service: up"
    else
        echo "    $service: FAILED" >&2
        FAILED+=("$service")
    fi
done

if [ "${#FAILED[@]}" -gt 0 ]; then
    echo
    echo "=== Deployment INCOMPLETE ==="
    printf 'failed: %s\n' "${FAILED[*]}"
    exit 1
fi

echo
echo "=== Deployment complete ==="
