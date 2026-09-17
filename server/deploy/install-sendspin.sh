#!/usr/bin/env bash
# Idempotent installer for the sendspin bridge's Python runtime.
#
# Debian 12's system Python is 3.11; aiosendspin needs
# >=3.12. Rather than fight the system interpreter, the bridge
# (server/sendspin_bridge.py) runs from its own uv-managed Python 3.12 venv,
# kept OUTSIDE the rsync'd live directory so a code deploy never touches it.
#
# Run as the service user, with $VENV and $HOME pointing somewhere that user
# owns. install.sh does both for you (/var/lib/cinematica-sendspin); the
# defaults below are for a hand run on a box where the service user is the
# account you are logged in as, which is what post-receive expects to have
# happened once already.
#
# $HOME matters as much as $VENV: uv installs itself under $HOME/.local/bin and
# caches the interpreters it downloads under $HOME/.local/share/uv, so a $HOME
# this user cannot write fails here rather than at the point of use.
set -euo pipefail

# aiosendspin has shipped nine major versions since 1.0.0 -- this is still an
# early-stage implementation of a moving protocol spec, and a major bump has
# repeatedly meant a real API break, not a formality. sendspin_bridge.py (and
# tests/_stubs.py, which stands in for it) imports a specific, narrow surface:
# clock.RawMonotonicClock, models.types.ConnectionReason,
# noise.keys.{Identity,b64url_decode}, noise.trust_store.FileServerPairingStore
# and server.{AudioFormat,SendspinServer}. All of that was confirmed present,
# at these exact import paths, in aiosendspin 9.1.1 (the newest release on
# PyPI as of writing) -- so pin to the patch range of that verified version:
# ~=9.1.1 allows 9.1.2, 9.1.3, ... but refuses 9.2.0 or 10.0.0, either of
# which is exactly the kind of release this library's own history says could
# rename or drop something the bridge imports.
AIOSENDSPIN_SPEC="aiosendspin[server]~=9.1.1"

VENV="${VENV:-$HOME/cinematica-venv312}"

if ! command -v uv >/dev/null 2>&1; then
    echo "install-sendspin: uv not found, installing to ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "install-sendspin: $(uv --version)"

uv python install 3.12

if [ -d "$VENV" ]; then
    echo "install-sendspin: venv already exists at $VENV"
else
    echo "install-sendspin: creating venv at $VENV"
    uv venv --python 3.12 "$VENV"
fi

uv pip install --python "$VENV/bin/python" "$AIOSENDSPIN_SPEC" aiohttp

echo "install-sendspin: $("$VENV/bin/python" --version)"
echo "install-sendspin: aiosendspin $("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("aiosendspin"))')"
