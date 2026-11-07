FROM debian:trixie-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG MCP_SERVER_LIB_VERSION=0.4.0
ARG ORG_MCP_VERSION=1.0.0
# v0.13.0 predates the stdio-child-exit fix (PR #248).  Pin the merge commit
# rather than a release so a dead shim makes the proxy exit and Docker can
# restart it.
ARG MCP_PROXY_REF=ad058cb88c652b08007e1eaec7df090995a42a21

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      emacs-nox \
      git \
      openssh-client \
      python3 \
      python3-venv \
      util-linux \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/mcp-proxy \
 && /opt/mcp-proxy/bin/pip install --no-cache-dir \
      "git+https://github.com/sparfenyuk/mcp-proxy.git@${MCP_PROXY_REF}"

RUN mkdir -p /opt/emacs-lisp \
 && git clone --depth 1 --branch "${MCP_SERVER_LIB_VERSION}" \
      https://github.com/laurynas-biveinis/mcp-server-lib.el \
      /opt/emacs-lisp/mcp-server-lib.el \
 && git clone --depth 1 --branch "${ORG_MCP_VERSION}" \
      https://github.com/laurynas-biveinis/org-mcp \
      /opt/emacs-lisp/org-mcp

RUN emacs --batch \
      -L /opt/emacs-lisp/mcp-server-lib.el \
      -L /opt/emacs-lisp/org-mcp \
      -f batch-byte-compile \
      /opt/emacs-lisp/mcp-server-lib.el/mcp-server-lib.el \
      /opt/emacs-lisp/mcp-server-lib.el/mcp-server-lib-commands.el \
      /opt/emacs-lisp/mcp-server-lib.el/mcp-server-lib-metrics.el \
      /opt/emacs-lisp/org-mcp/org-mcp.el

# Read-only `org-git-log' tool.  Canonical copy lives in this repo; a Doom
# configuration may keep a duplicate at ~/.doom.d/lisp/my/org-git-tool.el.
COPY org-git-tool.el /opt/emacs-lisp/org-git-tool.el

# Install the stdio shim through the library's own installer, then assert it
# exists so a future upstream path change fails the build instead of runtime.
RUN emacs --batch \
      -L /opt/emacs-lisp/mcp-server-lib.el \
      --eval '(progn (require (quote mcp-server-lib-commands)) (mcp-server-lib-install))' \
 && test -x /root/.emacs.d/emacs-mcp-stdio.sh

# Replace the upstream bash shim with the resilient Python bridge.  Keep the
# shell path because mcp-proxy already points at it.
COPY emacs-mcp-stdio.py /root/.emacs.d/emacs-mcp-stdio.py
RUN printf '%s\n' '#!/bin/sh' 'exec python3 /root/.emacs.d/emacs-mcp-stdio.py "$@"' \
      > /root/.emacs.d/emacs-mcp-stdio.sh \
 && chmod 0755 /root/.emacs.d/emacs-mcp-stdio.sh /root/.emacs.d/emacs-mcp-stdio.py

COPY init.el /root/.emacs.d/init.el
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY git-sync.sh /usr/local/bin/git-sync.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/git-sync.sh

ENV HOME=/root
WORKDIR /root

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
