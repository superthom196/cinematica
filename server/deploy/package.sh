#!/usr/bin/env bash
#
# Build the distributable server tarball:
#
#   server/deploy/package.sh            -> dist/cinematica-server-<version>.tar.gz
#   VERSION=1.2.3 server/deploy/package.sh
#
# <version> is `git describe --tags --match 'server-v*' --always` with the
# "server-v" prefix removed, i.e. the tag name for a tagged build and the short
# commit sha otherwise. $VERSION overrides it.
#
# The tarball unpacks to a single cinematica-server/ directory holding exactly
# what a stranger needs: install.sh, the server, the sendspin hifi-audio bridge,
# the phone page, the fixtures, the docs, the deploy templates and dev scripts,
# the guides and the licence.
# Runtime state and the developer-only push-deploy machinery (post-receive,
# install-hook.sh, package.sh, the committed cinematica.service) are left out.
#
# This is not the developer deploy path. Pushing to the original Pi still goes
# through deploy/post-receive (see deploy/README.md); the two are independent.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$SERVER_DIR/.." && pwd)"
DIST="$REPO/dist"
NAME="cinematica-server"

version="${VERSION:-}"
if [ -z "$version" ]; then
    if version="$(git -C "$REPO" describe --tags --match 'server-v*' --always 2>/dev/null)"; then
        version="${version#server-v}"
    else
        version=""
    fi
fi
if [ -z "$version" ]; then
    echo "package: not a git checkout and \$VERSION is unset; cannot name the tarball." >&2
    exit 1
fi

TARBALL="$DIST/${NAME}-${version}.tar.gz"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/$NAME"

echo "package: version ${version}"
mkdir -p "$ROOT" "$ROOT/deploy" "$DIST"

need() {
    [ -e "$1" ] || { echo "package: missing $1" >&2; exit 1; }
}

# --- the installer and the application -------------------------------------
# sendspin_bridge.py is the hifi-audio bridge: a second service, run from its
# own Python 3.12 venv, that install.sh sets up from deploy/install-sendspin.sh
# and deploy/cinematica-sendspin.service.in. It must sit beside server.py --
# the rendered unit's ExecStart is @DIR@/sendspin_bridge.py.
for f in install.sh server.py browser_play.py sendspin_bridge.py index.html \
         INSTALL.md README.md; do
    need "$SERVER_DIR/$f"
    cp -p "$SERVER_DIR/$f" "$ROOT/$f"
done
chmod +x "$ROOT/install.sh"
# The files the page pulls in besides itself, served under /static: the
# wordmark's face, and hls.js -- vendored rather than fetched from a CDN,
# so the player works with no third-party origin in its trust chain.
mkdir -p "$ROOT/static"
for f in alfa-slab-one.ttf alfa-slab-one-OFL.txt \
         hls-1.7.3.min.js hls-1.7.3-LICENSE.txt; do
    need "$SERVER_DIR/static/$f"
    cp -p "$SERVER_DIR/static/$f" "$ROOT/static/$f"
done

need "$REPO/LICENSE"
cp -p "$REPO/LICENSE" "$ROOT/LICENSE"

# --- docs and the providers package -----------------------------------------
# providers/ is the whole pluggable-provider package -- contract, addon, runner,
# host, store, registry, gateway -- everything server.py's
# `from providers import ...` needs at runtime. Any integration package a
# developer keeps in this checkout lives outside $SERVER_DIR entirely, so it is
# never even a candidate here.
need "$SERVER_DIR/providers"
rsync -a \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '*.bak-*' \
    "$SERVER_DIR/providers" "$ROOT/"

# docs/ is an ALLOWLIST, not a directory copy. It used to be rsync'd wholesale,
# which shipped the internal engineering notes to everyone who downloaded a
# release -- CONNECTIONS.md and ANDROID-TV-BRIEF.md between them carried this
# box's LAN name and address, its MAC, the VPN exit's city and AS number, the
# router's policy-routing tunnel id, and a /home path with the author's name in
# it. None of that is any of a stranger's business, and none of it is needed to
# run the application. Add a file here only when a user of the release needs it.
mkdir -p "$ROOT/docs"
# shellcheck disable=SC2043  # one entry today; a loop because it is a list that
# grows, and the next person adding a file should not also have to restructure
# this. Left as a warning it fails `shellcheck -x`, which is a required step of
# the release workflow -- so the release could not be built by CI at all.
for f in PROVIDERS.md; do
    need "$SERVER_DIR/docs/$f"
    cp "$SERVER_DIR/docs/$f" "$ROOT/docs/$f"
done

# The worked example PROVIDERS.md sends a package author to: manifest, entry
# file and its sample library, three small text files with nothing private in
# them. Shipped because the alternative is what the doc used to do -- send the
# reader into providers/*.py to work the contract out from the implementation.
need "$SERVER_DIR/docs/example-provider"
rsync -a --exclude '__pycache__/' --exclude '*.pyc' \
    "$SERVER_DIR/docs/example-provider" "$ROOT/docs/"

# fixtures/ is gone from the release too: captured sample responses from
# services this application no longer talks to, useful only when developing the
# optional provider packages, where they now live.

# --- the deploy templates, plus fake-app.sh and this dir's own README -------
# Deliberately NOT shipped: post-receive, install-hook.sh, package.sh and the
# committed cinematica.service, which are all about pushing to the original Pi
# and mean nothing on someone else's machine.
for f in docker-compose.yml.in server-settings.json cinematica.service.in \
         cinematica-sendspin.service.in install-sendspin.sh sendspin_probe.py \
         env.example fake-app.sh; do
    need "$HERE/$f"
    cp -p "$HERE/$f" "$ROOT/deploy/$f"
done
# install.sh invokes it as `bash install-sendspin.sh`, but deploy/README.md
# tells the reader to run it directly, so ship it executable.
chmod +x "$ROOT/deploy/install-sendspin.sh"

# --- belt and braces: nothing secret or stateful may have crept in ----------
# sendspin_identity.json holds the bridge's private key and sendspin_pairing.json
# the players it has been trusted by; both are per-machine state, never shipped.
# providers.json/secrets.json/admin.json are providers/store.py's on-disk state
# -- they only ever live under CINEMATICA_STATE (default /var/lib/cinematica),
# never under server/, but a name check here costs nothing and catches a future
# accident before it ships. providers-extra, __pycache__ and *.pyc are checked
# by pattern (find -name, not a bare string) since the first is a directory
# name and the other two can appear at any depth. providers-extra is kept in
# the list deliberately: it is not in this repository, and the guard exists so
# that it cannot be added and shipped by accident.
for bad in .env transcode netprofile.json nowplaying.json imdb-ratings.tsv.gz dist \
           sendspin_identity.json sendspin_pairing.json \
           providers.json secrets.json admin.json providers-extra \
           __pycache__ '*.pyc'; do
    if find "$ROOT" -name "$bad" -print -quit | grep -q .; then
        echo "package: refusing to ship '$bad' — it is in the staging tree." >&2
        exit 1
    fi
done

rm -f "$TARBALL"
# --- refuse to ship this box's own details -----------------------------------
# The docs allowlist above is the fix; this is the check that proves it worked,
# and that catches the next file someone adds without thinking. A release that
# names the author's machine, its MAC, or the VPN it sits behind is not a
# release, so the build stops rather than warning.
#
# It matches IDENTIFYING things, not every private address: a worked example
# like "--adb 192.168.1.50:5555" in the installer's help is genuinely useful and
# belongs in a release. The author's own hostname, home directory and hardware
# addresses do not.
leak_fail=0
for pat in 'serverpi' '\bthom\b' '/home/thom' \
           '([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}' \
           'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.'; do
    # No exceptions. There used to be one, for a deliberately fake JWT in
    # migrate.py's self-test; that file is gone, and with it the only reason
    # this guard ever had to trust something by name.
    hits=$(grep -rEIni "$pat" "$ROOT" 2>/dev/null || true)
    if [ -n "$hits" ]; then
        echo "package: REFUSING -- '$pat' appears in the staged tree:" >&2
        printf '%s\n' "$hits" | head -5 >&2
        leak_fail=1
    fi
done
[ "$leak_fail" -eq 0 ] || exit 1

tar -C "$STAGE" -czf "$TARBALL" "$NAME"

echo "package: wrote $TARBALL"
if command -v du >/dev/null 2>&1; then
    echo "package: $(du -h "$TARBALL" | cut -f1)"
fi

# --- sha256 sidecar ----------------------------------------------------------
if command -v shasum >/dev/null 2>&1; then
    (cd "$DIST" && shasum -a 256 "$(basename "$TARBALL")" > "$(basename "$TARBALL").sha256")
    echo "package: wrote ${TARBALL}.sha256"
elif command -v sha256sum >/dev/null 2>&1; then
    (cd "$DIST" && sha256sum "$(basename "$TARBALL")" > "$(basename "$TARBALL").sha256")
    echo "package: wrote ${TARBALL}.sha256"
else
    echo "package: WARNING - no shasum or sha256sum found, skipping checksum" >&2
fi
