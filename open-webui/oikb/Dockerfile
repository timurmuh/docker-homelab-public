FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

ARG OIKB_VERSION=0.5.0

# git is load-bearing: the readiness check reads the shared clone, and without
# it every check would fail and the guard would fail open.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "oikb==${OIKB_VERSION}"

# .oikb.yaml is read from the working directory.  It holds the three KB
# entries and their filters, not credentials.
WORKDIR /app
COPY .oikb.yaml /app/.oikb.yaml

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY healthcheck.sh /usr/local/bin/healthcheck.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

# /config holds config.yaml (API token), history.db, the single-instance lock
# and the heartbeat; all of them are written at runtime and live on a host bind
# mount.  The container runs as root, like the org-mcp sidecar, so the mount
# needs no ownership juggling.
ENV OIKB_CONFIG_DIR=/config

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
