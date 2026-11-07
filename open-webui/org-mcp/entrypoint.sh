#!/bin/sh
set -eu

export HOME=/root
export GIT_SSH_COMMAND="ssh -i /run/secrets/org_deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

git config --global user.name "org-mcp"
git config --global user.email "org-mcp@localhost"
git config --global --add safe.directory /org

if [ ! -d /org/.git ]; then
  echo "org-mcp: cloning org repository"
  git clone git@github.com:your-org/org.git /org
fi

echo "org-mcp: starting Emacs daemon"
emacs --daemon

for _ in $(seq 1 60); do
  if emacsclient -e '(emacs-version)' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! emacsclient -e '(emacs-version)' >/dev/null 2>&1; then
  echo "org-mcp: Emacs daemon did not come up" >&2
  exit 1
fi

echo "org-mcp: starting git sync loop"
/usr/local/bin/git-sync.sh &

exec /opt/mcp-proxy/bin/mcp-proxy \
  --pass-environment \
  --port 8765 \
  --host 0.0.0.0 \
  -- /root/.emacs.d/emacs-mcp-stdio.sh \
  --server-id=org-mcp \
  --init-function=my/org-mcp-start
