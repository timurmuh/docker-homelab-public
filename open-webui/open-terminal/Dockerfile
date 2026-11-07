# ghcr.io publishes only `latest`/`main` for this project (no v-prefixed
# tags). This index digest was `latest` on 2026-10-20 and its config was
# built from the v0.14.2 release. Pinning the digest keeps rebuilds stable.
FROM ghcr.io/open-webui/open-terminal:latest@sha256:b3a8f4bfb649824e686c3c7d60ab928553466023aba8ef46056d7deafc7a9392

# The base image ships grep but not ripgrep; the note mirror is queried with rg.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*
USER user
