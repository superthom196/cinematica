#!/usr/bin/env bash
#
# Cinematica — server installer.
#
#   tar xzf cinematica-server-<version>.tar.gz
#   cd cinematica-server
#   sudo ./install.sh
#
# Automates the manual setup: the Stremio streaming container and its 4K tuning, the
# transcode bind mount, the state directory, and the systemd unit. Answer one
# question (the address your TV will use) and you get a running server with
# no film source configured yet — open the printed setup URL in a browser,
# paste the one-time token, and add a catalogue and a stream provider there.
# Cinematica ships with no provider credentials of its own to ask for.
#
# Idempotent: re-run it any time to update an existing install in place. Runtime
# state — .env, the provider state directory (default /var/lib/cinematica),
# netprofile.json, nowplaying.json, imdb-ratings.tsv.gz and transcode/ — is
# never overwritten.
#
# This does NOT replace the developer deploy path (deploy/post-receive +
# deploy/install-hook.sh, a git push to the original Pi). The two are
# independent; the only shared surface is the unit file, see the sync note in
# deploy/cinematica.service.in.

set -euo pipefail

# ---------------------------------------------------------------- defaults ---
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="/opt/cinematica"
SVC_USER=""
HOST=""
ADB=""
INTERACTIVE=1
DRY=0
SKIP_DOCKER=0
SKIP_SYSTEMD=0
INSTALL_DOCKER=0
UNIT_DST="/etc/systemd/system/cinematica.service"
COMPOSE_PROJECT="cinematica"
APT=0
COMPOSE=""
SS_WARNED=0
# What the health poll found, and whether this run can honestly call itself a
# success. The summary box at the bottom reads both, and the exit status is
# INSTALL_FAILED: an installer that prints "installed and running" after the
# server never answered is worse than useless to anything scripting it.
HEALTH_STATE="not-checked"   # not-checked | ok | silent | unreadable
INSTALL_FAILED=0

if [ -t 1 ]; then
    B=$'\033[1m'; N=$'\033[0m'; DIM=$'\033[2m'
    GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'
else
    B=''; N=''; DIM=''; GRN=''; YLW=''; RED=''
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$GRN" "$N" "$B" "$*" "$N"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s !! %s%s\n' "$YLW" "$*" "$N" >&2; }
die()  { printf '\n%s !! %s%s\n' "$RED" "$*" "$N" >&2; exit 1; }
plan() { printf '    %s+ %s%s\n' "$DIM" "$*" "$N"; }

# In --dry-run every side effect goes through here and is printed instead.
run() {
    if [ "$DRY" = 1 ]; then plan "$*"; return 0; fi
    "$@"
}

usage() {
    cat <<'EOF'
Cinematica server installer.

  sudo ./install.sh [options]

Options:
  --dir DIR         where to install (default: /opt/cinematica)
  --user USER       the user the service runs as (default: a "cinematica"
                    system user, created if it doesn't exist)
  --host NAME       PUBLIC_HOST: the address the TV and your phone will use to
                    reach this box. Default: this machine's primary LAN IPv4.
                    A DNS name that your TV can resolve (e.g. "mediabox.lan")
                    works just as well, and survives the IP changing.
  --adb IP:PORT     optional phone remote: lets the server wake the TV app to
                    the foreground over adb. Off unless given.
  --non-interactive never prompt; answer every question with its default.
  --dry-run         print the plan and change nothing (does not need root)
  --skip-docker     do not install or start the Stremio container (for
                    containers/CI, where there is no Docker daemon)
  --skip-systemd    do not install or start the systemd unit (for containers/CI,
                    where there is no init system)
  --install-docker  install Docker from the official get.docker.com script
                    without asking (needed with --non-interactive if Docker
                    is not already present)
  -h, --help        this

Examples:
  sudo ./install.sh
  sudo ./install.sh --host mediabox.lan --adb 192.168.1.50:5555
  sudo ./install.sh --non-interactive --host 192.168.1.42
EOF
}

# ------------------------------------------------------------------- args ----
while [ $# -gt 0 ]; do
    case "$1" in
        --dir)             [ $# -ge 2 ] || die "--dir needs a directory";  DIR="$2"; shift 2 ;;
        --user)            [ $# -ge 2 ] || die "--user needs a username";  SVC_USER="$2"; shift 2 ;;
        --host)            [ $# -ge 2 ] || die "--host needs a name or IP"; HOST="$2"; shift 2 ;;
        --adb)             [ $# -ge 2 ] || die "--adb needs IP:PORT";      ADB="$2"; shift 2 ;;
        --dir=*)           DIR="${1#*=}"; shift ;;
        --user=*)          SVC_USER="${1#*=}"; shift ;;
        --host=*)          HOST="${1#*=}"; shift ;;
        --adb=*)           ADB="${1#*=}"; shift ;;
        --non-interactive) INTERACTIVE=0; shift ;;
        --dry-run)         DRY=1; shift ;;
        --skip-docker)     SKIP_DOCKER=1; shift ;;
        --skip-systemd)    SKIP_SYSTEMD=1; shift ;;
        --install-docker)  INSTALL_DOCKER=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 usage >&2; die "unknown option: $1" ;;
    esac
done

DIR="${DIR%/}"
[ -n "$DIR" ] || die "--dir cannot be empty"
case "$DIR" in /*) ;; *) die "--dir must be an absolute path (got '$DIR')" ;; esac
if [ -n "$ADB" ]; then
    case "$ADB" in
        *:*) ;;
        *)   die "--adb wants IP:PORT, e.g. 192.168.1.50:5555 (got '$ADB')" ;;
    esac
fi
if [ "$INTERACTIVE" = 1 ] && [ ! -t 0 ]; then
    INTERACTIVE=0
    warn "stdin is not a terminal; continuing as if --non-interactive was given."
fi

ask_yn() {   # ask_yn "question" -> 0 yes / 1 no. Non-interactive answers yes.
    if [ "$INTERACTIVE" = 0 ]; then return 0; fi
    local a=""
    read -r -p "    $1 [Y/n] " a || true
    case "$a" in [nN]*) return 1 ;; *) return 0 ;; esac
}

ask() {      # ask VAR "prompt" "default"
    local __var="$1" __prompt="$2" __default="${3:-}" __a=""
    if [ "$INTERACTIVE" = 0 ]; then printf -v "$__var" '%s' "$__default"; return 0; fi
    if [ -n "$__default" ]; then
        read -r -p "    $__prompt [$__default]: " __a || true
    else
        read -r -p "    $__prompt: " __a || true
    fi
    [ -n "$__a" ] || __a="$__default"
    printf -v "$__var" '%s' "$__a"
}

# -------------------------------------------------------------- the plan -----
printf '\n%s  Cinematica server installer%s\n' "$B" "$N"
if [ "$DRY" = 1 ]; then
    printf '%s  dry run — nothing on this machine will be changed%s\n' "$YLW" "$N"
fi

# --------------------------------------------------------------- preflight ---
step "Preflight"

if [ "$DRY" = 0 ] && [ "$(id -u)" -ne 0 ]; then
    die "run this as root: sudo ./install.sh   (or add --dry-run to see the plan)"
fi

[ -f "$SRC/server.py" ]  || die "server.py is not next to install.sh — run this from inside the unpacked tarball."
[ -f "$SRC/index.html" ] || die "index.html is not next to install.sh — the tarball looks incomplete."
[ -d "$SRC/deploy" ]     || die "deploy/ is not next to install.sh — the tarball looks incomplete."

if command -v apt-get >/dev/null 2>&1; then APT=1; fi
info "package manager: $([ "$APT" = 1 ] && echo apt || echo 'not apt — packages must already be present')"
info "architecture:    $(uname -m)"

case "$(uname -m)" in
    armv7l|armv6l)
        die "32-bit ARM ($(uname -m)) is not supported — stremio/server:latest is amd64/arm64 only.
  Reflash with a 64-bit OS (e.g. Raspberry Pi OS 64-bit) and re-run."
        ;;
esac

apt_install() {   # apt_install pkg...
    [ "$APT" = 1 ] || die "need $* but this is not an apt system; install it by hand and re-run."
    run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
}

# --- python3 >= 3.11 ---
py_ok() { python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' >/dev/null 2>&1; }
if ! command -v python3 >/dev/null 2>&1; then
    info "python3: missing, installing"
    apt_install python3
fi
if command -v python3 >/dev/null 2>&1 && ! py_ok; then
    die "Python 3.11 or newer is required; this machine has $(python3 -V 2>&1).

Cinematica is standard-library only and needs no pip, but it does need 3.11.
  Debian 12 (bookworm), Ubuntu 24.04+ and Raspberry Pi OS Bookworm ship 3.11+.
  Debian 11 (bullseye) ships 3.9: upgrade the OS, or install 3.11 another way
  (pyenv, a backport, or from source) and point the unit's ExecStart at it.
  Ubuntu 22.04 ships 3.10; install python3.11 from the deadsnakes PPA or upgrade to 24.04."
elif command -v python3 >/dev/null 2>&1; then
    info "python3: $(python3 -V 2>&1)"
fi

# --- curl, rsync ---
missing=""
for t in curl rsync; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
if [ -n "$missing" ]; then
    info "installing:$missing"
    # shellcheck disable=SC2086  # deliberate word splitting: one package per word
    apt_install $missing
else
    info "curl, rsync: present"
fi

# --- docker + compose plugin ---
if [ "$SKIP_DOCKER" = 1 ]; then
    info "docker: skipped (--skip-docker)"
else
    if ! command -v docker >/dev/null 2>&1; then
        say ""
        info "Docker is not installed. It runs the Stremio streaming server, which does"
        info "all the BitTorrent work, and carries the ffmpeg used for the audio fix."
        if [ "$INTERACTIVE" = 1 ]; then
            ask_yn "Install Docker now with the official script from get.docker.com?" \
                || die "Docker is required. Install it yourself and re-run this script."
        elif [ "$DRY" = 0 ] && [ "$INSTALL_DOCKER" != 1 ]; then
            die "Docker is required but not installed, and this is non-interactive.
Install it yourself first, or re-run with --install-docker to fetch and run the
official script from get.docker.com automatically."
        fi
        docker_sh="$(mktemp)"
        run curl -fsSL https://get.docker.com -o "$docker_sh"
        [ "$DRY" = 1 ] || info "get.docker.com script sha256: $(sha256sum "$docker_sh" | cut -d' ' -f1)"
        run sh "$docker_sh"
        run rm -f "$docker_sh"
    fi
    if [ "$DRY" = 1 ] && ! command -v docker >/dev/null 2>&1; then
        COMPOSE="docker compose"
        plan "docker compose version   (assumed present after install)"
    elif docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
        info "docker: $(docker --version 2>/dev/null), $(docker compose version --short 2>/dev/null)"
    else
        info "the docker compose plugin is missing, installing it"
        apt_install docker-compose-plugin
        docker compose version >/dev/null 2>&1 || die "still no 'docker compose' plugin. Install docker-compose-plugin and re-run."
        COMPOSE="docker compose"
    fi
    run systemctl enable --now docker >/dev/null 2>&1 || true
fi

# --- ports ---
port_busy() {
    if command -v ss >/dev/null 2>&1; then
        [ -n "$(ss -H -ltn "sport = :$1" 2>/dev/null)" ]
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | grep -qE "[:.]$1[[:space:]]"
    else
        if [ "$SS_WARNED" = 0 ]; then
            warn "neither ss nor netstat is available; skipping the port checks."
            SS_WARNED=1
        fi
        return 1
    fi
}
if [ "$SKIP_SYSTEMD" = 0 ] && port_busy 8090; then
    if systemctl is-active --quiet cinematica 2>/dev/null; then
        info "port 8090: held by the cinematica service — that's us, it will be restarted"
    else
        die "port 8090 is already in use by something that is not Cinematica.
Stop it, or free the port, and re-run.   ss -ltnp 'sport = :8090'  shows what it is."
    fi
fi
if [ "$SKIP_DOCKER" = 0 ] && port_busy 11470; then
    if [ -n "$(docker ps --filter 'name=^/stremio-server$' --format '{{.Names}}' 2>/dev/null)" ]; then
        info "port 11470: held by the stremio-server container — that's us"
    else
        die "port 11470 is already in use by something that is not our Stremio container.
Stop it and re-run.   ss -ltnp 'sport = :11470'  shows what it is."
    fi
fi
[ "$SKIP_SYSTEMD" = 0 ] || info "ports: 8090 check skipped (--skip-systemd)"
[ "$SKIP_DOCKER" = 0 ]  || info "ports: 11470 check skipped (--skip-docker)"

# ------------------------------------------------------------ service user ---
step "Service user"
[ -n "$SVC_USER" ] || SVC_USER="cinematica"
if id -u "$SVC_USER" >/dev/null 2>&1; then
    info "running as existing user: $SVC_USER"
else
    info "creating system user: $SVC_USER"
    run useradd --system --no-create-home --home-dir "$DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi

if [ "$SKIP_DOCKER" = 0 ]; then
    # server.py shells out to `docker exec stremio-server …` for ffmpeg/ffprobe
    # and to clear the cache, and reads /stats.json. Without daemon access none
    # of the audio fix works.
    if [ "$DRY" = 1 ]; then
        plan "groupadd -f docker; usermod -aG docker $SVC_USER"
    elif id -nG "$SVC_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        info "$SVC_USER is already in the docker group"
    else
        info "adding $SVC_USER to the docker group (it talks to the daemon for ffmpeg and the cache)"
        info "note: docker group membership is root-equivalent on this machine."
        run groupadd -f docker
        run usermod -aG docker "$SVC_USER"
        warn "$SVC_USER picks the new group up on next login; the service gets it at start, so this is fine."
    fi
fi

# Python providers are someone else's code. Run as $SVC_USER they would inherit
# its docker group, which is root on this machine, so they run as their own
# account instead: no docker group, no login, a home of its own. The sudoers
# rule lets $SVC_USER drop to that account and nothing else; the unit names
# the account in CINEMATICA_PROVIDER_USER (see providers/runner.py).
PROVIDER_USER="cinematica-provider"
PROVIDER_HOME="/var/lib/cinematica-provider"
PROVIDER_SUDOERS="/etc/sudoers.d/cinematica-provider"
command -v sudo >/dev/null 2>&1 || apt_install sudo
if id -u "$PROVIDER_USER" >/dev/null 2>&1; then
    info "provider account exists: $PROVIDER_USER"
else
    info "creating the provider account: $PROVIDER_USER (not in the docker group)"
    run useradd --system --user-group --no-create-home --home-dir "$PROVIDER_HOME" \
        --shell /usr/sbin/nologin "$PROVIDER_USER"
fi
if [ "$DRY" != 1 ] && id -nG "$PROVIDER_USER" | tr ' ' '\n' | grep -qx docker; then
    die "$PROVIDER_USER is in the docker group, which defeats its purpose. Remove it: gpasswd -d $PROVIDER_USER docker"
fi
run install -d -m 0700 -o "$PROVIDER_USER" -g "$PROVIDER_USER" "$PROVIDER_HOME"
if [ "$DRY" = 1 ]; then
    plan "write $PROVIDER_SUDOERS ($SVC_USER may run commands as $PROVIDER_USER, nothing else)"
else
    tmp_sudoers="$(mktemp)"
    {
        echo "# Written by Cinematica's install.sh; re-running it rewrites this file."
        echo "# Lets the service account start provider code as $PROVIDER_USER, an"
        echo "# account with less access than its own. It grants nothing else."
        echo "# No pty: the server talks to the provider over pipes."
        echo "Defaults>$PROVIDER_USER !use_pty"
        echo "$SVC_USER ALL=($PROVIDER_USER) NOPASSWD: ALL"
    } > "$tmp_sudoers"
    visudo -cqf "$tmp_sudoers" || { rm -f "$tmp_sudoers"; die "the sudoers rule for $PROVIDER_USER did not validate"; }
    install -m 0440 -o root -g root "$tmp_sudoers" "$PROVIDER_SUDOERS"
    rm -f "$tmp_sudoers"
    info "installed $PROVIDER_SUDOERS"
fi

# ------------------------------------------------------------------- host ----
step "Address"
detect_ip() {
    local ip=""
    if command -v ip >/dev/null 2>&1; then
        ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*[[:space:]]src[[:space:]]\([0-9.]*\).*/\1/p' | head -n1)"
    fi
    if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
        ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    printf '%s' "$ip"
}
if [ -z "$HOST" ]; then
    guess="$(detect_ip || true)"
    if [ "$INTERACTIVE" = 1 ]; then
        say ""
        info "PUBLIC_HOST is the address your TV and your phone use to reach this box."
        info "It has to be something the TV can resolve: the converted-audio URL and the"
        info "stream URL are both handed to the player under this name."
        info "A LAN IP works. A DNS name (e.g. mediabox.lan) is fine too, and survives"
        info "the IP changing — use the fully-qualified form, not a bare hostname."
        ask HOST "Address for this machine" "$guess"
    else
        HOST="$guess"
    fi
fi
[ -n "$HOST" ] || die "could not work out this machine's LAN address; pass --host NAME|IP."
info "PUBLIC_HOST = $HOST   (the TV app will be pointed at $HOST:8090)"
if [ -n "$ADB" ]; then
    info "phone remote: ADB_TV=$ADB  (only ever used to wake the TV app to the foreground)"
else
    info "phone remote: off (no --adb). The TV app is the only player, which is the normal case."
fi

# ------------------------------------------------------------------ files ----
step "Files -> $DIR"
if [ "$SRC" = "$DIR" ]; then
    info "already running from $DIR; nothing to copy"
else
    run install -d -m 0755 "$DIR"
    # No --delete: runtime state living in DIR is not ours to remove. The
    # excludes matter when install.sh is run from a live directory or a repo
    # checkout, where those files exist on the source side too.
    run rsync -a \
        --exclude '.env' \
        --exclude 'netprofile.json' \
        --exclude 'nowplaying.json' \
        --exclude 'imdb-ratings.tsv.gz' \
        --exclude 'transcode/' \
        --exclude 'stremio/' \
        --exclude 'sendspin_identity.json' \
        --exclude 'sendspin_pairing.json' \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '*.bak-*' \
        --exclude 'dist/' \
        --exclude 'deploy/post-receive' \
        --exclude 'deploy/install-hook.sh' \
        --exclude 'deploy/package.sh' \
        "$SRC/server.py" "$SRC/browser_play.py" "$SRC/shelf.py" \
        "$SRC/sendspin_bridge.py" "$SRC/index.html" \
        "$SRC/static" "$SRC/docs" "$SRC/deploy" "$SRC/providers" \
        "$DIR/"
    for extra in INSTALL.md README.md LICENSE install.sh; do
        if [ -f "$SRC/$extra" ]; then run cp -p "$SRC/$extra" "$DIR/$extra"; fi
    done
    info "copied server.py, browser_play.py, shelf.py, sendspin_bridge.py, index.html, static/, docs/, deploy/, providers/"
    info "left alone: .env, the state directory, netprofile.json, nowplaying.json, imdb-ratings.tsv.gz, transcode/"
fi

# $DIR itself must be writable by $SVC_USER: server.py saves
# imdb-ratings.tsv.gz, netprofile.json and nowplaying.json beside itself via a
# tmp file + os.replace(), which needs write permission on the directory, not
# just on the files.
run chown "$SVC_USER" "$DIR"
# transcode/ is the host side of the container's /transcode bind mount. ffmpeg
# runs as root inside the container and writes here, so the .ts files end up
# root-owned — that is expected and documented. server.py (as $SVC_USER) also
# creates and serves files here, so the directory itself must be writable by it.
run install -d -m 0775 -o "$SVC_USER" "$DIR/transcode"
# stremio/ is the container's cacheRoot: the torrent cache plus
# server-settings.json. The container writes it as root; we only seed it.
run install -d -m 0775 -o "$SVC_USER" "$DIR/stremio"
run chown -R "$SVC_USER" "$DIR/deploy" "$DIR/docs" "$DIR/static" "$DIR/providers" 2>/dev/null || true
# sendspin_bridge.py runs as $SVC_USER under its own unit and keeps its identity
# and pairing state in $DIR, which is already $SVC_USER-owned (see above).
for f in server.py browser_play.py shelf.py sendspin_bridge.py index.html; do
    if [ "$DRY" = 1 ]; then
        plan "chown $SVC_USER $DIR/$f"
    elif [ -f "$DIR/$f" ]; then
        chown "$SVC_USER" "$DIR/$f"
    fi
done
info "transcode/ and stremio/ ready (owner $SVC_USER; container-written files will be root-owned)"
# The provider account imports providers/host.py from here. Under a private
# home directory (--dir ~/something) it cannot, and every Python provider
# would fail to start with nothing on screen but a crash.
if [ "$DRY" != 1 ] && ! sudo -n -u "$PROVIDER_USER" test -r "$DIR/providers/host.py"; then
    warn "$PROVIDER_USER cannot read $DIR/providers/host.py, so Python providers will not start."
    warn "Put Cinematica somewhere every account can read (the default /opt/cinematica is)."
fi

# --------------------------------------------------------- state directory ---
# Everything provider-related — installed packages, per-role activation,
# non-secret config, the credential store and the admin password/session
# material — lives here, OUTSIDE $DIR (see providers/store.py's module
# docstring): a code update replaces $DIR wholesale and must not be able to
# take an installed provider or its credentials down with it.
step "State directory"
STATE_DIR="${CINEMATICA_STATE:-/var/lib/cinematica}"
# 0711: $PROVIDER_USER must reach its own package in providers/, but may not
# list either level. Every file store.py writes here is 0600.
if [ "$DRY" = 1 ]; then
    plan "install -d -m 0711 -o $SVC_USER $STATE_DIR $STATE_DIR/providers"
else
    install -d -m 0711 -o "$SVC_USER" "$STATE_DIR"
    install -d -m 0711 -o "$SVC_USER" "$STATE_DIR/providers"
    info "ready: $STATE_DIR (mode 0711, owner $SVC_USER)"
fi

# ------------------------------------------------------------ local settings -
# No provider credentials are asked for here. Catalogues, metadata and stream
# sources are all providers now, installed and configured from the browser
# after the server is up. This step only ever writes the handful of settings
# that are genuinely local to this box, never a third-party key.
step "Local settings"
ENV_DST="$DIR/.env"
if [ -f "$ENV_DST" ]; then
    info "$ENV_DST already exists — left exactly as it is."
    info "Edit it by hand if a setting changed, then: systemctl restart cinematica"
else
    if [ "$DRY" = 1 ]; then
        plan "write $ENV_DST (mode 600, owner $SVC_USER) with SENDSPIN_CLIENT_URL"
    else
        umask 077
        cat > "$ENV_DST" <<EOF
# Cinematica local settings. Read by server.py from its own directory
# (\$ENV_FILE overrides the location). chmod 600, never committed, never sent
# to a browser. Written by install.sh; edit by hand and restart the service
# to change a value.
#
# There is nothing here for any catalogue, metadata or stream source. Those are
# all providers, installed and configured from the browser. See
# $DIR/docs/PROVIDERS.md.

# Network audio (server/sendspin_bridge.py) — play the film's soundtrack through
# a network audio player on the LAN instead of the TV. The player's websocket
# URL, e.g. ws://mediabox.lan:8928/sendspin. Empty = network audio off.
SENDSPIN_CLIENT_URL=
EOF
        umask 022
        chmod 600 "$ENV_DST"
        chown "$SVC_USER" "$ENV_DST"
        info "wrote $ENV_DST (mode 600, owner $SVC_USER)"
    fi
fi

# ----------------------------------------------------------------- stremio ---
step "Stremio streaming server"
COMPOSE_IN="$DIR/deploy/docker-compose.yml.in"
COMPOSE_YML="$DIR/deploy/docker-compose.yml"
SETTINGS_SEED="$DIR/deploy/server-settings.json"
SETTINGS_DST="$DIR/stremio/server-settings.json"
[ -f "$COMPOSE_IN" ]    || [ "$DRY" = 1 ] || die "missing $COMPOSE_IN — the tarball looks incomplete."
[ -f "$SETTINGS_SEED" ] || [ "$DRY" = 1 ] || die "missing $SETTINGS_SEED — the tarball looks incomplete."

if [ "$DRY" = 1 ]; then
    plan "render $COMPOSE_IN -> $COMPOSE_YML (@DIR@ -> $DIR)"
else
    {
        echo "# Generated by Cinematica's install.sh from deploy/docker-compose.yml.in."
        echo "# Re-running the installer rewrites this file."
        sed "s|@DIR@|$DIR|g" "$COMPOSE_IN"
    } > "$COMPOSE_YML"
    chown "$SVC_USER" "$COMPOSE_YML"
    info "rendered $COMPOSE_YML"
fi

# Stremio's own defaults are sized for 1080p and will stutter on 4K. Seeded once
# only: after that the file is the container's, and Cinematica itself rewrites
# cacheSize (every start) and btMaxConnections (link calibration) over HTTP, so
# only the two speed limits here stay hand-set.
if [ -f "$SETTINGS_DST" ]; then
    info "$SETTINGS_DST already exists — left alone"
elif [ "$DRY" = 1 ]; then
    plan "seed $SETTINGS_DST from deploy/server-settings.json (4K tuning)"
else
    cp "$SETTINGS_SEED" "$SETTINGS_DST"
    chown "$SVC_USER" "$SETTINGS_DST"
    info "seeded $SETTINGS_DST — 30 GiB cache, 10/16 MiB-s speed limits, no hardware transcoding"
fi

if [ "$SKIP_DOCKER" = 1 ]; then
    info "not starting the container (--skip-docker)"
else
    # shellcheck disable=SC2086  # COMPOSE is "docker compose", two words on purpose
    run $COMPOSE -p "$COMPOSE_PROJECT" -f "$COMPOSE_YML" up -d
    if [ "$DRY" = 1 ]; then
        plan "wait for http://localhost:11470/stats.json to answer"
    else
        info "waiting for Stremio on :11470 …"
        ok=0
        for _ in $(seq 1 60); do
            if curl -fsS --max-time 3 http://localhost:11470/stats.json >/dev/null 2>&1; then ok=1; break; fi
            sleep 2
        done
        if [ "$ok" = 1 ]; then
            info "Stremio is up (stats.json answers)"
        else
            warn "Stremio did not answer on :11470 within 2 minutes."
            warn "Look at:  docker logs stremio-server"
            warn "An 'exec format error' there means the wrong architecture image was pulled."
        fi
    fi
fi

# ----------------------------------------------------------------- systemd ---
step "Service"
UNIT_IN="$DIR/deploy/cinematica.service.in"
if [ "$SKIP_SYSTEMD" = 1 ]; then
    info "not installing the systemd unit (--skip-systemd)"
else
    [ -f "$UNIT_IN" ] || [ "$DRY" = 1 ] || die "missing $UNIT_IN — the tarball looks incomplete."
    adb_line=""
    if [ -n "$ADB" ]; then adb_line="Environment=ADB_TV=$ADB"; fi
    # Only needed when the state directory was overridden away from the
    # StateDirectory= default below (/var/lib/cinematica) -- the common case
    # needs no extra line, since StateDirectory= and store.py's own default
    # already agree.
    state_line=""
    if [ "$STATE_DIR" != "/var/lib/cinematica" ]; then
        state_line="Environment=CINEMATICA_STATE=$STATE_DIR"
    fi
    if [ "$DRY" = 1 ]; then
        plan "render $UNIT_IN -> $UNIT_DST (@DIR@=$DIR @USER@=$SVC_USER @HOST@=$HOST @ADB@='${adb_line}' @STATE@='${state_line}')"
        plan "systemctl daemon-reload"
        plan "systemctl enable --now cinematica"
    else
        tmp_unit="$(mktemp)"
        # The template's header explains its own placeholders, so it has to be
        # dropped before substitution rather than after -- a global s|@DIR@|…|
        # would otherwise rewrite the explanation into nonsense and leave it in
        # the installed unit. Start at [Unit] and put a real header on the front.
        {
            echo "# Generated by Cinematica's install.sh from deploy/cinematica.service.in."
            echo "# Re-running the installer rewrites this file. Hand edits will be lost;"
            echo "# change $DIR/deploy/cinematica.service.in, or edit here and stop re-running."
            echo "#"
            echo "# dir=$DIR user=$SVC_USER host=$HOST${ADB:+ adb=$ADB}"
            echo ""
            sed -n '/^\[Unit\]/,$p' "$UNIT_IN"
        } | sed -e "s|@DIR@|$DIR|g" \
                -e "s|@USER@|$SVC_USER|g" \
                -e "s|@HOST@|$HOST|g" > "$tmp_unit"
        if [ -n "$adb_line" ]; then
            sed -i "s|^@ADB@$|$adb_line|" "$tmp_unit"
        else
            # No address means no phone remote at all: the line goes away rather
            # than being set empty.
            sed -i "/^@ADB@$/d" "$tmp_unit"
        fi
        if [ -n "$state_line" ]; then
            sed -i "s|^@STATE@$|$state_line|" "$tmp_unit"
        else
            sed -i "/^@STATE@$/d" "$tmp_unit"
        fi
        install -m 0644 "$tmp_unit" "$UNIT_DST"
        rm -f "$tmp_unit"
        info "installed $UNIT_DST"
        systemctl daemon-reload
        systemctl enable cinematica >/dev/null 2>&1 || true
        systemctl restart cinematica
        # A unit that is started but not enabled works until the first reboot.
        if [ "$(systemctl is-enabled cinematica 2>/dev/null || true)" = "enabled" ]; then
            info "cinematica.service enabled and started"
        else
            warn "cinematica.service started but NOT enabled — it will not come back after a reboot."
            warn "Then:  systemctl enable cinematica"
        fi
    fi
fi

# ------------------------------------------------------------- sendspin -----
step "Network audio (the Sendspin bridge)"
SENDSPIN_INSTALL="$DIR/deploy/install-sendspin.sh"
# $HOME here is the INSTALLING user's home -- root's, since this script is run
# under sudo -- while everything below is created by, and has to be readable
# by, $SVC_USER. Defaulting the venv to $HOME/... therefore asked a system user
# with no access to /root to build a venv inside it, and hifi audio failed to
# install on every fresh machine: the venv never got created, the unit was
# installed anyway, and cinematica-sendspin crash-looped on a missing
# interpreter. So the location is a fixed, $SVC_USER-owned directory instead of
# anybody's home.
#
# Outside $DIR on purpose (the unit template says why): a code deploy replaces
# $DIR wholesale, and rebuilding a Python 3.12 toolchain on every deploy is
# both slow and a way to lose audio to a transient network failure.
#
# SENDSPIN_HOME is the HOME the installer runs under, not just the venv's
# parent: uv puts itself in $HOME/.local/bin and its downloaded interpreters in
# $HOME/.local/share/uv, so leaving HOME pointing anywhere else scatters them
# somewhere $SVC_USER cannot write, or -- worse, because it looks like it
# worked -- somewhere the next deploy deletes.
SENDSPIN_HOME="${SENDSPIN_HOME:-/var/lib/cinematica-sendspin}"
SENDSPIN_VENV="${SENDSPIN_VENV:-$SENDSPIN_HOME/venv312}"
# The unit's ExecStart is $DIR/sendspin_bridge.py. Without the script there is
# nothing to run, so don't build a venv and enable a service that can only
# crash-loop -- say so once and leave hifi audio off.
#
# Both checks look in $SRC as well: under --dry-run the files step hasn't run
# yet, so $DIR is still empty and testing it alone would cry wolf.
SENDSPIN_BRIDGE="$DIR/sendspin_bridge.py"
if [ -f "$SENDSPIN_BRIDGE" ] || [ -f "$SRC/sendspin_bridge.py" ]; then
    SKIP_SENDSPIN=0
else
    warn "missing $SENDSPIN_BRIDGE — skipping the network-audio bridge entirely (network audio stays off)"
    SKIP_SENDSPIN=1
fi
if [ "$SKIP_SENDSPIN" = 1 ]; then
    :
elif [ ! -f "$SENDSPIN_INSTALL" ] && [ ! -f "$SRC/deploy/install-sendspin.sh" ]; then
    warn "missing $SENDSPIN_INSTALL — skipping the network-audio bridge (network audio stays off)"
elif [ "$DRY" = 1 ]; then
    plan "install -d -m 0755 -o $SVC_USER $SENDSPIN_HOME"
    plan "sudo -u $SVC_USER HOME=$SENDSPIN_HOME VENV=$SENDSPIN_VENV bash $SENDSPIN_INSTALL"
else
    install -d -m 0755 -o "$SVC_USER" "$SENDSPIN_HOME"
    if sudo -u "$SVC_USER" env HOME="$SENDSPIN_HOME" VENV="$SENDSPIN_VENV" \
            bash "$SENDSPIN_INSTALL"; then
        # "the script exited 0" is not "audio will work". Check the thing the
        # unit's ExecStart actually names, and that it can import the library
        # the bridge is useless without -- a half-finished venv (interrupted
        # download, wheel that would not build on this architecture) exits 0
        # from pip often enough to be worth one second here rather than a
        # crash-loop the owner has to read the journal to understand.
        if sudo -u "$SVC_USER" "$SENDSPIN_VENV/bin/python" \
                -c 'import aiosendspin' >/dev/null 2>&1; then
            info "network-audio bridge venv ready at $SENDSPIN_VENV"
        else
            warn "$SENDSPIN_VENV exists but cannot import aiosendspin — network audio stays off"
            warn "Re-run by hand:  sudo -u $SVC_USER env HOME=$SENDSPIN_HOME VENV=$SENDSPIN_VENV bash $SENDSPIN_INSTALL"
            SKIP_SENDSPIN=1
        fi
    else
        warn "install-sendspin.sh failed — network audio will stay off until it's re-run by hand"
        warn "Re-run by hand:  sudo -u $SVC_USER env HOME=$SENDSPIN_HOME VENV=$SENDSPIN_VENV bash $SENDSPIN_INSTALL"
        # Installing a unit whose interpreter does not exist buys nothing but a
        # crash-loop in the journal and a "failed" in systemctl status. Leave
        # the bridge uninstalled and say so once, here.
        SKIP_SENDSPIN=1
    fi
fi

SENDSPIN_UNIT_IN="$DIR/deploy/cinematica-sendspin.service.in"
SENDSPIN_UNIT_DST="/etc/systemd/system/cinematica-sendspin.service"
if [ "$SKIP_SENDSPIN" = 1 ]; then
    :
elif [ "$SKIP_SYSTEMD" = 1 ]; then
    info "not installing the network-audio bridge unit (--skip-systemd)"
elif [ ! -f "$SENDSPIN_UNIT_IN" ] && [ ! -f "$SRC/deploy/cinematica-sendspin.service.in" ]; then
    warn "missing $SENDSPIN_UNIT_IN — skipping the network-audio bridge unit"
elif [ "$DRY" = 1 ]; then
    plan "render $SENDSPIN_UNIT_IN -> $SENDSPIN_UNIT_DST (@DIR@=$DIR @USER@=$SVC_USER @VENV@=$SENDSPIN_VENV)"
    plan "systemctl daemon-reload"
    plan "systemctl enable --now cinematica-sendspin"
else
    tmp_sendspin_unit="$(mktemp)"
    {
        echo "# Generated by Cinematica's install.sh from deploy/cinematica-sendspin.service.in."
        echo "# Re-running the installer rewrites this file. Hand edits will be lost;"
        echo "# change $DIR/deploy/cinematica-sendspin.service.in, or edit here and stop re-running."
        echo "#"
        echo "# dir=$DIR user=$SVC_USER venv=$SENDSPIN_VENV"
        echo ""
        sed -n '/^\[Unit\]/,$p' "$SENDSPIN_UNIT_IN"
    } | sed -e "s|@DIR@|$DIR|g" \
            -e "s|@USER@|$SVC_USER|g" \
            -e "s|@VENV@|$SENDSPIN_VENV|g" > "$tmp_sendspin_unit"
    install -m 0644 "$tmp_sendspin_unit" "$SENDSPIN_UNIT_DST"
    rm -f "$tmp_sendspin_unit"
    info "installed $SENDSPIN_UNIT_DST"
    systemctl daemon-reload
    systemctl enable cinematica-sendspin >/dev/null 2>&1 || true
    systemctl restart cinematica-sendspin

    # "systemctl restart" returning 0 only means systemd forked something. The
    # bridge's failures -- a missing interpreter, an aiosendspin import error,
    # a state directory it cannot write -- all happen a moment later, and the
    # unit's Restart=on-failure/RestartSec=3 then turns them into a crash-loop
    # that looks, to anyone not reading the journal, exactly like a working
    # install. Wait past one restart interval and check it is still up.
    sleep 5
    ss_state="$(systemctl is-active cinematica-sendspin 2>/dev/null || true)"
    ss_restarts="$(systemctl show -p NRestarts --value cinematica-sendspin 2>/dev/null || echo 0)"
    ss_enabled="$(systemctl is-enabled cinematica-sendspin 2>/dev/null || true)"
    if [ "$ss_state" = "active" ] && [ "${ss_restarts:-0}" = "0" ]; then
        if [ "$ss_enabled" = "enabled" ]; then
            info "cinematica-sendspin.service enabled and running"
        else
            # Running now, gone after the next reboot, and nothing in the
            # journal then: systemd was simply never asked to start it.
            warn "cinematica-sendspin is running but NOT enabled — network audio will stop at the next reboot."
            warn "Then:  systemctl enable cinematica-sendspin"
        fi
        if ! grep -qE '^SENDSPIN_CLIENT_URL=.+' "$ENV_DST" 2>/dev/null; then
            info "SENDSPIN_CLIENT_URL is empty in $ENV_DST — network audio stays off until it's set, then: systemctl restart cinematica-sendspin"
        fi
    else
        warn "cinematica-sendspin is $ss_state after ${ss_restarts:-0} restart(s) — network audio is NOT working."
        warn "The rest of Cinematica is unaffected; sound will play through the TV."
        warn "What it said:"
        journalctl -u cinematica-sendspin -n 20 --no-pager 2>/dev/null | sed 's/^/    /' >&2 || true
        warn "Then:  systemctl restart cinematica-sendspin"
    fi
fi

# ------------------------------------------------------------------ health ---
if [ "$SKIP_SYSTEMD" = 0 ] && [ "$DRY" = 0 ]; then
    step "Health"
    info "polling http://localhost:8090/api/health for up to 60 seconds …"
    body=""
    for _ in $(seq 1 60); do
        body="$(curl -fsS --max-time 3 http://localhost:8090/api/health 2>/dev/null || true)"
        if [ -n "$body" ]; then break; fi
        sleep 1
    done
    if [ -z "$body" ]; then
        HEALTH_STATE="silent"
        INSTALL_FAILED=1
        warn "the server did not answer on :8090 within 60 seconds."
        warn "Look at:  journalctl -u cinematica -n 50 --no-pager"
    else
        vals="$(printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
prov = d.get("providers") or {}
msg = (prov.get("message") or "").replace("\t", " ")
print("%s\t%s\t%s" % ("yes" if prov.get("configured") else "no", msg, d.get("movies")))
' 2>/dev/null || true)"
        if [ -z "$vals" ]; then
            # It answered, so the service is up; only the shape of the reply is
            # wrong. Not a failed install, but not a clean one either.
            HEALTH_STATE="unreadable"
            warn "health answered but could not be parsed: $body"
        else
            HEALTH_STATE="ok"
            h_configured="$(printf '%s' "$vals" | cut -f1)"
            h_message="$(printf '%s' "$vals" | cut -f2)"
            h_movies="$(printf '%s' "$vals" | cut -f3)"
            info "providers      $h_message"
            info "movies         $h_movies"
            if [ "$h_configured" != "yes" ]; then
                # "No providers configured" (or streams/catalogue not set) is
                # the normal state right after a fresh install -- nothing to
                # fix here, just a nudge toward the browser step below.
                info "that's expected on a fresh install: open the setup URL below to add a"
                info "catalogue and a stream provider. The grid stays empty until you do."
            fi
        fi
    fi
fi

# ------------------------------------------------------------------ setup ----
# The one-time bootstrap token that claims the admin account. Fetched by
# invoking providers/store.py directly (not an HTTP call — the server may
# still be starting, and this must never itself create or activate a
# provider) as $SVC_USER, so admin.json ends up owned the same way the
# running service will need it to be.
#
# Three outcomes, and they are NOT interchangeable: a token, an account that
# somebody already claimed, or a fetch that failed. This used to collapse the
# last two -- any empty output at all was reported as "already claimed", so a
# permission error or a broken interpreter told the reader their admin account
# existed when nothing of the sort had happened, and nobody could log in.
# store.bootstrap_token() returns None only for a claimed account, so the
# provider process says which case it is in words, and the exit status
# separates "answered" from "did not run".
SETUP_URL="http://$HOST:8090/"
BOOTSTRAP_TOKEN=""
ALREADY_CLAIMED=0
TOKEN_ERROR=0
TOKEN_MESSAGE=""
if [ "$DRY" = 1 ]; then
    plan "fetch/create the one-time setup token from $STATE_DIR (providers/store.py)"
else
    # $DIR is passed as an argument rather than cd'd into: `cd X && cmd` inside
    # a command substitution that also ends in `|| true` is the A && B || C
    # shape, which reads as if the fallback covered the cd, and shellcheck
    # (SC2015) fails the release build over it. The python side puts $DIR on
    # sys.path itself, which is all the cd was ever for.
    token_rc=0
    token_out="$(sudo -u "$SVC_USER" env CINEMATICA_STATE="$STATE_DIR" python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from providers import store
token = store.bootstrap_token()
print(("TOKEN %s" % token) if token else "CLAIMED")
' "$DIR" 2>&1)" || token_rc=$?
    if [ "$token_rc" != 0 ]; then
        TOKEN_ERROR=1
        INSTALL_FAILED=1
        # Last non-empty line: the traceback's final line names the failure,
        # and the box has room for one line, not twenty.
        TOKEN_MESSAGE="$(printf '%s\n' "$token_out" | grep -v '^[[:space:]]*$' | tail -n 1)"
        if [ -z "$TOKEN_MESSAGE" ]; then
            TOKEN_MESSAGE="providers/store.py exited $token_rc without saying why"
        fi
        warn "could not read the one-time setup token: $TOKEN_MESSAGE"
    elif [ "$token_out" = "CLAIMED" ]; then
        ALREADY_CLAIMED=1
    else
        BOOTSTRAP_TOKEN="${token_out#TOKEN }"
        if [ "$BOOTSTRAP_TOKEN" = "$token_out" ] || [ -z "$BOOTSTRAP_TOKEN" ]; then
            # It exited 0 and said something else entirely: report that, rather
            # than handing the reader whatever it printed as a token.
            TOKEN_ERROR=1
            INSTALL_FAILED=1
            BOOTSTRAP_TOKEN=""
            TOKEN_MESSAGE="unexpected reply from providers/store.py: $(printf '%s' "$token_out" | tr '\n' ' ' | cut -c1-120)"
            warn "could not read the one-time setup token: $TOKEN_MESSAGE"
        fi
    fi
fi

# ----------------------------------------------------------------- summary ---
# Box lines are ASCII on purpose: %-*s pads by bytes, and a stray em dash would
# push the right-hand border out by two columns.
BOXW=70
box() {
    local line rule
    rule="$(printf '%*s' "$BOXW" '' | tr ' ' '-')"
    printf '\n%s+%s+%s\n' "$B" "$rule" "$N"
    while IFS= read -r line; do
        printf '%s|%s %-*s %s|%s\n' "$B" "$N" "$((BOXW - 2))" "$line" "$B" "$N"
    done
    printf '%s+%s+%s\n' "$B" "$rule" "$N"
}

{
    # The first line is the one a reader believes, so it says what actually
    # happened. "Installed and running" is reserved for a server that answered
    # its own health endpoint.
    if [ "$DRY" = 1 ]; then
        echo "DRY RUN. Nothing above was done. This is what a real run would leave:"
    elif [ "$HEALTH_STATE" = "silent" ]; then
        echo "Cinematica is INSTALLED BUT NOT RUNNING: the server never answered"
        echo "on :8090. Nothing below can be set up until it does -- start with"
        echo "the log line at the end of this box."
    elif [ "$HEALTH_STATE" = "unreadable" ]; then
        echo "Cinematica is installed and answering on :8090, but its health reply"
        echo "could not be read (see above). No film source is set up yet."
    elif [ "$HEALTH_STATE" = "not-checked" ]; then
        # --skip-systemd: nothing was started, so nothing is running, and this
        # run has no idea whether it would.
        echo "Cinematica is installed. The service was not started, so its health"
        echo "was not checked."
    else
        echo "Cinematica is installed and running. No film source is set up yet."
    fi
    echo ""
    echo "  Open in a browser      $SETUP_URL"
    if [ -n "$BOOTSTRAP_TOKEN" ]; then
        echo "  One-time setup token   $BOOTSTRAP_TOKEN"
        echo "  Paste the token, set an admin password, then add a catalogue"
        echo "  provider and a stream provider -- either by pasting a compatible"
        echo "  add-on manifest URL, or by uploading a provider package."
        echo "  docs/PROVIDERS.md lists what is known to work."
    elif [ "$ALREADY_CLAIMED" = 1 ]; then
        echo "  The admin account is already claimed -- log in there to manage"
        echo "  providers, or to add one for the first time."
    elif [ "$TOKEN_ERROR" = 1 ]; then
        echo "  The one-time setup token could NOT be read. This does not mean the"
        echo "  admin account is claimed -- it means the token could not be asked"
        echo "  for at all, and until that is fixed nobody can claim the account."
        echo "  What it said:"
        echo "    $TOKEN_MESSAGE"
        echo "  Fix that and run this installer again; re-running is safe."
    elif [ "$DRY" = 1 ]; then
        echo "  (setup token not fetched in a dry run)"
    else
        echo "  No setup token was fetched. Open the URL above; if it asks for a"
        echo "  token, run this installer again to print one."
    fi
    echo ""
    echo "  Point the TV app at    $HOST:8090"
    echo "  Phone browser          http://$HOST:8090"
    echo ""
    echo "  Installed in           $DIR"
    echo "  Local settings         $DIR/.env"
    echo "  State directory        $STATE_DIR"
    echo "  Service unit           $UNIT_DST"
    echo "  Stremio cache          $DIR/stremio"
    echo "  Audio conversions      $DIR/transcode"
    echo ""
    echo "  Logs      journalctl -u cinematica -f"
    echo "  Restart   systemctl restart cinematica"
    echo "  Health    curl localhost:8090/api/health"
    echo ""
    echo "  To update: unpack a newer tarball and run sudo ./install.sh again."
    echo "  It keeps .env, the state directory (providers, credentials, admin"
    echo "  password), the learned link profile and the caches."
    echo ""
    echo "  INSTALL.md has the troubleshooting and the caveats worth knowing."
} | box
printf '\n'

# An install that did not work exits non-zero. The box above says what went
# wrong in words; this is the same fact in the only form a script running
# `./install.sh && ...` can see.
if [ "$INSTALL_FAILED" = 1 ]; then
    exit 1
fi
