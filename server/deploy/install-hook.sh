#!/usr/bin/env bash
# Idempotent installer for the git-push deploy hook. Run this from your own machine.
#
# It ssh-es to the deploy host and:
#   - creates the bare repo <home>/cinematica.git if it doesn't exist yet
#   - installs server/deploy/post-receive as that repo's post-receive hook
#   - creates <home>/cinematica-src (the plain checkout used by the hook)
# then, locally, adds a "pi-deploy" git remote pointing at that bare repo
# if one isn't already configured. <home> is the SSH-ing user's home
# directory ON THE DEPLOY HOST, queried below -- never assumed, since it
# only matches this machine's $HOME when you happen to use the same
# username on both ends.
#
# Usage: server/deploy/install-hook.sh <host>   (no default -- name your own box)
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "install-hook: usage: server/deploy/install-hook.sh <host>" >&2
    echo "install-hook: <host> is anything 'ssh <host>' resolves -- an /etc/hosts entry, an SSH config alias, or a bare LAN address." >&2
    exit 1
fi
HOST="$1"
REMOTE_HOME="$(ssh "$HOST" 'printf %s "$HOME"')"
BARE_REPO="${REMOTE_HOME}/cinematica.git"
SRC_DIR="${REMOTE_HOME}/cinematica-src"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_FILE="${SCRIPT_DIR}/post-receive"

if [ ! -f "$HOOK_FILE" ]; then
    echo "install-hook: cannot find ${HOOK_FILE}" >&2
    exit 1
fi

echo "install-hook: target host = ${HOST} (remote home: ${REMOTE_HOME})"

ssh "$HOST" bash -s <<EOF
set -euo pipefail

if [ -d "${BARE_REPO}" ]; then
    echo "install-hook: bare repo ${BARE_REPO} already exists"
else
    echo "install-hook: creating bare repo ${BARE_REPO}"
    git init --bare "${BARE_REPO}"
fi

mkdir -p "${SRC_DIR}"
echo "install-hook: ensured ${SRC_DIR} exists"
EOF

echo "install-hook: installing post-receive hook"
ssh "$HOST" "cat > ${BARE_REPO}/hooks/post-receive" < "$HOOK_FILE"
ssh "$HOST" "chmod +x ${BARE_REPO}/hooks/post-receive"
echo "install-hook: installed and chmod +x ${BARE_REPO}/hooks/post-receive"

if git remote get-url pi-deploy >/dev/null 2>&1; then
    echo "install-hook: local remote 'pi-deploy' already exists ($(git remote get-url pi-deploy))"
else
    git remote add pi-deploy "ssh://${HOST}${BARE_REPO}"
    echo "install-hook: added local remote 'pi-deploy' -> ssh://${HOST}${BARE_REPO}"
fi

echo "install-hook: done. Deploy with: git push pi-deploy main"
