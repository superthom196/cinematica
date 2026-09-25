#!/usr/bin/env python3
"""Sendspin hifi-audio bridge.

Debian 12's system Python is 3.11; aiosendspin needs >=3.12, so this script
runs from a separate uv-managed venv (server/deploy/install-sendspin.sh) and
talks to server.py over a tiny localhost HTTP API instead of being imported
by it. It owns exactly one thing: pushing PCM audio, decoded from a running
film, into one Sendspin player client (the Music Assistant hifi player) with
enough clock information for the caller to keep video and audio in sync.

API (127.0.0.1 only):
  GET  /players             -> {players: [{id, name, url, connected}], default}
  POST /connect {url?}      -> {connected, client_id, name} or 503 {error}
  POST /prepare {src, aidx, centre?, start_s?}
                            -> {prepared: true, cache}: decode the track into the
                               PCM cache from start_s (default 0), from now on
  POST /start {src, aidx, centre?, start_s, gen, pos_at_us?}
                            -> {gen, t0_us, clock_offset_us} or 503 {error}
                               202 {gen, pending: true, clock_offset_us} when the
                               cache has not reached start_s yet: poll /status
  GET  /status              -> {connected, streaming, pending, gen, t0_us, now_us,
                                 audio_pos_s, ffmpeg_alive, volume, clock_offset_us,
                                 player_url, player_name, cache, supply}
  POST /delay {ms}          -> {delay_ms}: lip-sync trim, applied live
  POST /stop                -> {stopped: true}   (pause: the cache is kept)
  POST /release              -> {released: true}  (end: cache dropped, player freed)
  POST /volume {level|delta} -> {volume} or 400/503 {error}

Never runs SendspinServer.start_server() (no self-advertisement / port 8927,
which Music Assistant on this host may already hold). It only ever dials out,
as a client, to whichever hifi player the TV picked from /players -- falling
back to SENDSPIN_CLIENT_URL when the caller doesn't say. Players themselves
are discovered passively via mDNS browsing for `_sendspin._tcp.local.`.

How the audio is timed. The track is decoded ahead of time: /prepare (sent
while the film is still pre-buffering) starts one ffmpeg writing 48 kHz
stereo PCM into a file, kept at most DECODE_LEAD_S ahead of whatever is being
played (ffmpeg simply blocks on its pipe when the lead is full, so it never
pulls the swarm ahead of the picture by more than that). /start then serves
from that file: the first chunk is committed with an explicit play time, so
film time x plays on the DAC at exactly t0 + x where t0 is fixed from the
caller's timestamp -- no decoder seek, no guessed lead, nothing for the
picture to chase. A seek or a resume inside the decoded range is served the
same way at once; only a jump past the decoded range restarts the decoder.
The timeline is reported only while that push is alive and the player
connected; the moment it is stopped or replaced it is gone.

The lip-sync trim (/delay, positive = the sound heard later) moves the audio,
not the picture: it shifts t0 and re-pins the next chunk at the exact moment
the queued audio runs out, so the sound slides by that much with no gap and
nothing for the TV to chase. It is heard once the queue drains, about 1.6 s.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from pathlib import Path

from aiohttp import web
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from aiosendspin.clock import RawMonotonicClock
from aiosendspin.models.types import ConnectionReason
from aiosendspin.noise.keys import Identity, b64url_decode
from aiosendspin.noise.trust_store import FileServerPairingStore
from aiosendspin.server import AudioFormat, SendspinServer

SCRIPT_DIR = Path(__file__).resolve().parent

# Copied verbatim from server.py's load_env() -- deliberately not imported
# from server.py, so this bridge has no dependency on the 3.11 process.
def load_env(path):
    out = {}
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return out


ENV_FILE = os.environ.get("ENV_FILE", str(SCRIPT_DIR / ".env"))
ENV = load_env(ENV_FILE)


def cfg(key, default=""):
    """Env var wins over .env, both win over the given default."""
    val = os.environ.get(key)
    if val is not None and val != "":
        return val
    return ENV.get(key, default)


SENDSPIN_CLIENT_URL = cfg("SENDSPIN_CLIENT_URL", "")
BRIDGE_PORT = int(cfg("SENDSPIN_BRIDGE_PORT", "8091"))
FFMPEG = cfg("FFMPEG", "/usr/lib/jellyfin-ffmpeg/ffmpeg")
FFMPEG_CTR = cfg("FFMPEG_CTR", "stremio-server")
STATE_DIR = Path(cfg("SENDSPIN_STATE_DIR", str(SCRIPT_DIR)))

CHUNK_BYTES = 9600  # 50ms @ 48kHz / 16-bit / stereo
CHUNK_US = 50_000
BYTES_PER_S = 192_000
AUDIO_FORMAT = AudioFormat(48000, 16, 2)
# PCM cache: one file per decoder, in the same never-rsynced directory the AC3
# conversions use. Decoding runs this far ahead of the play head and no
# further (a full film is ~1.7 GB of PCM; the lead is what the disk holds
# beyond what has already played, which is never dropped, so a seek back is
# free). The same 180 s the AC3 path keeps.
CACHE_DIR = Path(cfg("SENDSPIN_CACHE_DIR", str(SCRIPT_DIR / "transcode")))
DECODE_LEAD_S = float(cfg("SENDSPIN_DECODE_LEAD_S", "180"))
DECODE_READ_BYTES = CHUNK_BYTES * 4  # 200 ms per read off ffmpeg
# The stereo downmix. Left alone, ffmpeg scales the matrix so that every
# channel at full scale at once still cannot clip: FL + 0.707 FC + 0.707 SL
# sums to 2.414, so a 5.1 track comes out 7.7 dB down, and real tracks never
# use that room (measured: true peak -7.4 dBFS over a 5.1 episode). This is
# the sum the matrix is scaled to instead; 2.0 gives 6.0 dB of it back. It is
# not a gain stage: a stereo source is not rematrixed and comes through as is.
DOWNMIX_MAXVAL = float(cfg("SENDSPIN_DOWNMIX_MAXVAL", "2.0"))
# Centre mode (/prepare and /start with centre=true): the TV's own speakers
# play the centre channel, so it comes out of this downmix. swresample only
# applies center_mix_level to a source that also has L/R, so a mono track
# still reaches both sides at -3 dB and a stereo one is untouched.
CENTRE_OFF = ":center_mix_level=0"
# A /start further past the decoded range than this restarts the decoder at
# the new position rather than waiting for it to get there.
CACHE_WAIT_S = 20.0
# Scheduling margin over the player's own send-ahead floor for the first
# chunk of a push: the earliest the DAC can be asked to play it.
FIRST_CHUNK_MARGIN_US = 300_000
# Lip-sync trim bounds, matching server.py's. Positive means the sound is
# heard later, the sign an AV receiver's "audio delay" uses.
DELAY_MIN_US = -2_000_000
DELAY_MAX_US = 5_000_000
# How far ahead of the DAC clock the push keeps the player fed, at least. The
# player states its own send-ahead floor (its buffer plus static delay); the
# queue must always sit ABOVE that floor, because the moment it drains to
# below it the library rebases the timeline forward -- a few ms of silence
# spliced in on every commit, which crept the audio 10 ms/s behind the
# picture on 2026-09-16 with a flat 1 s here against a ~1.02 s floor. So the
# limit is the floor plus this margin, never less than a second.
BUFFER_AHEAD_US = 1_000_000
BUFFER_MARGIN_US = 500_000
CONNECT_TIMEOUT_S = 10.0
# How long /start waits for the first chunk before answering 202 "pending".
# Seeking an MKV over the swarm's HTTP stream routinely takes 2-6s, so most
# starts answer pending and the caller learns t0 from /status.
START_TIMEOUT_S = 2.0
# Every step of a teardown is bounded by this, so that holding _lock across a
# teardown can never wedge later /start, /stop and /release calls. Two steps
# (container kill, reap) plus slack is the most a decoder stop can take; the
# caller's HTTP timeouts are set from this number, not guessed.
TEARDOWN_TIMEOUT_S = 5.0
TEARDOWN_MAX_S = 2 * TEARDOWN_TIMEOUT_S + 1.0
# stream/end and the group update that follow it are QUEUED on the client's
# writer task, not written by the call that sends them, and disconnecting
# cancels the connection that owns that writer. One turn of the loop between
# the two is what gets the last word out to the player.
TEARDOWN_FLUSH_S = 0.1
# `docker exec` hands us a client process, not ffmpeg: killing the client
# leaves ffmpeg decoding into a dead pipe inside the container. So each ffmpeg
# carries a unique marker in its own argument list (an ignored -metadata) and
# is killed container-side by that marker -- only that decoder, never a
# neighbour that was just started. The prefix alone sweeps up everything a
# previous life of this process left behind.
FFMPEG_MARKER_PREFIX = "cinematica-sendspin-"
# What pre-marker builds of this bridge left in the container: server.py's own
# AC3 conversion never uses pcm_s16le, so this pattern is ours alone.
FFMPEG_LEGACY_PATTERN = "pcm_s16le"
# A read that takes longer than this, once audio is live, is the source not
# keeping up (swarm stall, seek): counted in /status so a dropout can be told
# apart from a player-side problem. Logged, rate-limited, past STALL_LOG_MS.
STALL_MS = 200.0
STALL_LOG_MS = 1000.0
STALL_LOG_GAP_S = 10.0

SENDSPIN_SERVICE_TYPE = "_sendspin._tcp.local."
PLAYER_RESOLVE_TIMEOUT_MS = 3000

# Mutable bridge state, guarded by _lock where it matters (start/stop/release).
SERVER = None  # type: SendspinServer | None
CLIENT_ID = None  # type: str | None
ACTIVE_URL = None  # type: str | None -- url of the player CLIENT_ID is for
_lock = asyncio.Lock()
STATE = {
    "gen": 0,
    "decoder": None,  # type: Decoder | None -- the one decoder filling the cache
    "pusher": None,  # type: Pusher | None -- the one push from cache to the DAC
    # Lip-sync trim, in force across starts: a calibration of this room's
    # panel and DAC, not a property of one film.
    "delay_us": 0,
}

# mDNS discovery. Keyed by ws:// url (not the raw zeroconf service name) so
# /players and /connect can address a player the same way. Each entry also
# keeps the raw service name, so a Removed event (which only carries that
# name) can find the right url to drop. Entries live until zeroconf says the
# service is gone (goodbye packet or record expiry): mDNS announcements are
# not heartbeats, so a healthy player must never disappear on a timer.
AZC = None  # type: AsyncZeroconf | None
BROWSER = None  # type: AsyncServiceBrowser | None
PLAYERS = {}  # type: dict[str, dict]


class Decoder:
    """One ffmpeg decoding one track from start_s onwards into a PCM file.

    The decode task is the process's only owner: whether ffmpeg hits EOF,
    fails, or the task is cancelled by a /prepare for another source or a
    /release, its `finally` is the one place the process is terminated.
    The file is the cache; `end_bytes` is how much of it is valid."""

    def __init__(self, src, aidx, start_s, proc, marker, path, centre=False):
        self.src = src
        self.aidx = aidx
        self.centre = centre
        self.start_s = start_s
        self.proc = proc
        self.marker = marker
        self.path = path
        self.fh = open(path, "wb", buffering=0)
        self.end_bytes = 0
        self.head_s = start_s  # what is being played, for the decode-ahead bound
        self.started_at = time.monotonic()
        self.task = None  # type: asyncio.Task | None
        self.complete = False
        self.error = None  # type: str | None
        self.terminated = False
        # Supply measurements: reads off ffmpeg that took longer than STALL_MS
        # once audio was flowing are the source failing to keep up.
        self.reads = 0
        self.stalls = 0
        self.max_stall_ms = 0.0
        self._last_stall_log = 0.0

    @property
    def alive(self):
        return self.proc.returncode is None

    @property
    def end_s(self):
        return self.start_s + self.end_bytes / BYTES_PER_S

    def offset(self, x_s):
        return max(0, int((x_s - self.start_s) * BYTES_PER_S)) // 4 * 4

    def has(self, x_s):
        """Whether film time x_s is decoded (or will be shortly)."""
        if x_s < self.start_s - 0.01:
            return False
        if x_s <= self.end_s:
            return True
        return self.alive and not self.complete and x_s - self.end_s <= CACHE_WAIT_S

    def info(self):
        return {
            "src": self.src, "aidx": self.aidx, "centre": self.centre,
            "start_s": round(self.start_s, 3), "end_s": round(self.end_s, 3),
            "lead_s": round(self.end_s - self.head_s, 1),
            "decoding": self.alive, "complete": self.complete, "error": self.error,
        }


class Pusher:
    """One push from the cache to the DAC: film time x plays at t0_us + x."""

    def __init__(self, gen, dec, stream, x0_s, t0_us, delay_us):
        self.gen = gen
        self.dec = dec
        self.stream = stream
        self.pos_s = x0_s  # next film time to send
        self.t0_us = t0_us
        # The trim already baked into t0. A change to STATE["delay_us"] is
        # picked up by the push loop and re-pins the timeline by the difference.
        self.delay_us = delay_us
        self.next_play_us = None  # where the queued audio runs out
        self.started_at = time.monotonic()
        self.task = None  # type: asyncio.Task | None
        self.live = False
        self.chunks = 0
        self.waits = 0  # times the cache had nothing for us yet
        self.ahead_us = 0

    def supply(self):
        dec = self.dec
        return {
            "chunks": self.chunks,
            "waits": self.waits,
            "stalls": dec.stalls,
            "max_stall_ms": round(dec.max_stall_ms, 1),
            "ahead_ms": round(self.ahead_us / 1000.0, 1),
        }


def _load_or_create_identity(path):
    try:
        data = json.loads(path.read_text())
        return Identity.from_private_bytes(b64url_decode(data["private_b64u"]))
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        identity = Identity.generate()
        path.write_text(json.dumps({"private_b64u": identity.private_b64u}))
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return identity


def _clock_offset_us():
    return SERVER.clock.now_us() - (time.monotonic_ns() // 1000)


def _get_client():
    if SERVER is None or CLIENT_ID is None:
        return None
    return SERVER.get_client(CLIENT_ID)


def _player_role(client):
    roles = client.roles_by_family("player") if client is not None else []
    return roles[0] if roles else None


def _connected_url():
    """The url of the player we currently hold a live connection to, or None."""
    client = _get_client()
    if client is not None and client.is_connected:
        return ACTIVE_URL
    return None


def _default_player_url():
    """Where /connect goes when the caller names no player: the configured
    url, else the one discovered player if there is exactly one."""
    if SENDSPIN_CLIENT_URL:
        return SENDSPIN_CLIENT_URL
    if len(PLAYERS) == 1:
        return next(iter(PLAYERS))
    return None


def _txt_str(info, key, default=None):
    val = info.properties.get(key.encode()) if info.properties else None
    if val is None:
        val = info.properties.get(key) if info.properties else None
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    return val if val is not None else default


def _instance_name(service_name):
    suffix = "." + SENDSPIN_SERVICE_TYPE
    if service_name.endswith(suffix):
        return service_name[: -len(suffix)]
    return service_name


async def _resolve_and_add_player(zeroconf, service_type, service_name):
    info = AsyncServiceInfo(service_type, service_name)
    try:
        ok = await info.async_request(zeroconf, PLAYER_RESOLVE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - best-effort discovery
        ok = False
    if not ok:
        return
    addresses = info.parsed_scoped_addresses() or info.parsed_addresses()
    if not addresses or not info.port:
        return
    name = _txt_str(info, "name") or _instance_name(service_name)
    path = _txt_str(info, "path") or "/sendspin"
    host = addresses[0]
    if ":" in host:
        host = "[" + host + "]"
    url = "ws://%s:%d%s" % (host, info.port, path)
    is_new = url not in PLAYERS
    PLAYERS[url] = {
        "id": url,
        "name": name,
        "url": url,
        "seen_at": time.monotonic(),
        "_service_name": service_name,
    }
    if is_new:
        print("bridge: player found %s %s" % (name, url))


def _remove_player_by_service_name(service_name):
    for url, player in list(PLAYERS.items()):
        if player.get("_service_name") == service_name:
            del PLAYERS[url]
            print("bridge: player gone %s" % player["name"])


def _on_service_state_change(zeroconf, service_type, name, state_change):
    if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
        asyncio.ensure_future(_resolve_and_add_player(zeroconf, service_type, name))
    elif state_change is ServiceStateChange.Removed:
        _remove_player_by_service_name(name)


# ---- decoder lifecycle --------------------------------------------------------

async def _kill_container_ffmpeg(pattern):
    """Kill ffmpeg where it actually lives: inside the container. `pattern` is
    matched against the whole command line, so a decoder's marker hits that
    decoder and nothing else."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", FFMPEG_CTR, "pkill", "-9", "-f", pattern,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), TEARDOWN_TIMEOUT_S)
    except asyncio.TimeoutError:
        print("bridge: container pkill did not return in %ss" % TEARDOWN_TIMEOUT_S)
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    except Exception as exc:  # noqa: BLE001 - teardown is best-effort
        print("bridge: container pkill failed: %s" % exc)


async def _spawn_decoder(args):
    """The one place a decoder process is created (tests substitute it)."""
    return await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _terminate_decoder(dec):
    """Stop this decoder and only this decoder. Idempotent; every step bounded.

    Order matters: ffmpeg in the container first (by marker), then the
    `docker exec` client if it is somehow still up, then reap the client
    while DRAINING its stdout. Once the decode task stops consuming the pipe
    ffmpeg keeps writing, the read buffer fills and asyncio pauses it -- after
    which the pipe never reports EOF and a plain wait() never returns. That
    was the hang behind every "reader task still running after 5.0s".
    communicate() reads to EOF, so it cannot."""
    if dec.terminated:
        return
    dec.terminated = True
    await _kill_container_ffmpeg(dec.marker)
    proc = dec.proc
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    try:
        await asyncio.wait_for(proc.communicate(), TEARDOWN_TIMEOUT_S)
    except asyncio.TimeoutError:
        print("bridge: decoder client did not exit in %ss" % TEARDOWN_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - teardown is best-effort
        print("bridge: decoder reap failed: %s" % exc)
    with contextlib.suppress(Exception):
        dec.fh.close()


async def _await_task(task, what):
    """Wait for a task we cancelled, bounded; its own CancelledError is the
    normal outcome, not us being cancelled."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), TEARDOWN_MAX_S)
    except asyncio.TimeoutError:
        print("bridge: %s cleanup still running after %ss" % (what, TEARDOWN_MAX_S))
    except asyncio.CancelledError:
        if not task.done():
            raise
    except Exception:  # noqa: BLE001 - the task's own exception, already reported
        pass


async def _stop_pusher():
    """Retire the live push, if any. Called with _lock held. The timeline is
    gone the instant this is entered: STATE["pusher"] is cleared before
    anything is awaited, so a /status racing it already sees no stream."""
    p = STATE["pusher"]
    STATE["pusher"] = None
    if p is None:
        return
    with contextlib.suppress(Exception):
        p.stream.stop()
    await _await_task(p.task, "push gen=%s" % p.gen)


async def _drop_decoder():
    """Retire the decoder and its cache file. Called with _lock held, after
    the pusher (which reads the file) is gone."""
    dec = STATE["decoder"]
    STATE["decoder"] = None
    if dec is None:
        return
    await _await_task(dec.task, "decoder")
    await _terminate_decoder(dec)
    with contextlib.suppress(OSError):
        os.unlink(dec.path)


async def _stop_group(client):
    """Stop group playback, bounded. Called with _lock held, so it cannot block forever."""
    try:
        await asyncio.wait_for(client.group.stop(), TEARDOWN_TIMEOUT_S)
    except asyncio.TimeoutError:
        print("bridge: group.stop() did not return in %ss, abandoning it" % TEARDOWN_TIMEOUT_S)
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown is best-effort
        pass


async def _disconnect_active(keep_cache=False):
    """Tear down whatever player we're currently connected to, if any. The
    decoder and its cache go too, unless the caller is only reconnecting."""
    global CLIENT_ID, ACTIVE_URL
    async with _lock:
        await _stop_pusher()
        if not keep_cache:
            await _drop_decoder()
        client = _get_client()
        if client is not None:
            await _stop_group(client)
        if SERVER is not None and ACTIVE_URL is not None:
            # Let the stop above reach the player before the connection that
            # would carry it is cancelled: see TEARDOWN_FLUSH_S.
            await asyncio.sleep(TEARDOWN_FLUSH_S)
            with contextlib.suppress(Exception):
                SERVER.disconnect_from_client(ACTIVE_URL)
        CLIENT_ID = None
        ACTIVE_URL = None


async def _decode_loop(dec):
    proc = dec.proc
    try:
        while True:
            if dec.end_s - dec.head_s > DECODE_LEAD_S:
                # Lead full: stop reading and ffmpeg blocks on its pipe, so it
                # stops pulling the swarm too. Resumes as the play head moves.
                await asyncio.sleep(0.25)
                continue
            read_at = time.monotonic()
            data = await proc.stdout.read(DECODE_READ_BYTES)
            if not data:
                break
            took_ms = (time.monotonic() - read_at) * 1000.0
            if dec.reads and took_ms > STALL_MS:
                dec.stalls += 1
                dec.max_stall_ms = max(dec.max_stall_ms, took_ms)
                if took_ms > STALL_LOG_MS and time.monotonic() - dec._last_stall_log > STALL_LOG_GAP_S:
                    dec._last_stall_log = time.monotonic()
                    print("bridge: decoder stalled %.0fms at %.1fs (%d stalls so far, lead %.1fs)"
                          % (took_ms, dec.end_s, dec.stalls, dec.end_s - dec.head_s))
            dec.fh.write(data)
            dec.end_bytes += len(data)
            dec.reads += 1
        # stdout is at EOF, so wait() cannot hang on an unread pipe.
        rc = await asyncio.wait_for(proc.wait(), TEARDOWN_TIMEOUT_S)
        if rc == 0:
            dec.complete = True
            print("bridge: decode complete %.1fs-%.1fs" % (dec.start_s, dec.end_s))
        else:
            dec.error = "ffmpeg exited rc=%s at %.1fs" % (rc, dec.end_s)
            print("bridge: %s" % dec.error)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        dec.error = "decoder failed: %s" % exc
        print("bridge: %s" % dec.error)
    finally:
        try:
            await _terminate_decoder(dec)
        except Exception as exc:  # noqa: BLE001
            print("bridge: decoder cleanup failed: %s" % exc)


async def _ensure_decoder(src, aidx, start_s, centre=False):
    """The decoder for (src, aidx, centre) covering start_s, starting one if
    needed. Called with _lock held. Replacing the decoder retires the pusher
    first: the file it reads is about to go."""
    dec = STATE["decoder"]
    if (dec is not None and dec.src == src and dec.aidx == aidx
            and dec.centre == centre and dec.has(start_s)):
        return dec
    await _stop_pusher()
    await _drop_decoder()
    marker = FFMPEG_MARKER_PREFIX + uuid.uuid4().hex
    args = [
        "docker", "exec", FFMPEG_CTR, FFMPEG,
        "-hide_banner", "-loglevel", "error",
        "-ss", str(start_s),
        "-i", src,
        "-map", "0:a:%d" % aidx,
        # aformat right behind it makes this aresample the one that downmixes,
        # not one ffmpeg inserts for "-ac 2" without the option.
        "-af", "aresample=rematrix_maxval=%g%s,aformat=channel_layouts=stereo" % (
            DOWNMIX_MAXVAL, CENTRE_OFF if centre else ""),
        "-vn", "-ac", "2", "-ar", "48000",
        # Ignored by the raw muxer; it exists so `pkill -f <marker>` inside
        # the container hits exactly this ffmpeg.
        "-metadata", "comment=" + marker,
        "-c:a", "pcm_s16le", "-f", "s16le", "-",
    ]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = str(CACHE_DIR / ("sendspin-%s.pcm" % marker[len(FFMPEG_MARKER_PREFIX):][:12]))
    proc = await _spawn_decoder(args)
    dec = Decoder(src, aidx, start_s, proc, marker, path, centre=centre)
    STATE["decoder"] = dec
    dec.task = asyncio.ensure_future(_decode_loop(dec))
    print("bridge: decode from %.3fs src=%s aidx=%s%s -> %s" % (
        start_s, src, aidx, " centre-off" if centre else "", path))
    return dec


async def _push_loop(p, live_future):
    dec, stream = p.dec, p.stream
    send_ahead_us = _send_ahead_us(stream)
    limit_us = max(BUFFER_AHEAD_US, send_ahead_us + BUFFER_MARGIN_US)
    fh = open(dec.path, "rb")
    fh.seek(dec.offset(p.pos_s))
    try:
        while STATE["pusher"] is p:
            pin_us = None
            if p.chunks == 0:
                # The pin is t0 + pos_s. If waiting on the cache has let that
                # moment pass, skip forward: the audio for those film seconds
                # has already been shown, and pinning it in the past would
                # make the library rebase the whole timeline late.
                want_s = (stream.now_us() + send_ahead_us - p.t0_us) / 1_000_000
                if want_s > p.pos_s + 0.01:
                    p.pos_s = want_s
                    fh.seek(dec.offset(p.pos_s))
                pin_us = p.t0_us + int(p.pos_s * 1_000_000)
            elif STATE["delay_us"] != p.delay_us:
                # The trim moved. Carry on from where the queued audio ends --
                # so there is no gap and no overlap -- but under the shifted
                # timeline, which means stepping the film time we read by the
                # same amount. That step IS the delay.
                delta = STATE["delay_us"] - p.delay_us
                p.delay_us = STATE["delay_us"]
                p.t0_us += delta
                pin_us = max(p.next_play_us, stream.now_us() + send_ahead_us)
                pos_s = (pin_us - p.t0_us) / 1_000_000
                p.pos_s = max(dec.start_s, pos_s)
                pin_us = p.t0_us + int(p.pos_s * 1_000_000)
                fh.seek(dec.offset(p.pos_s))
                print("bridge: gen=%s lip-sync trim %+d ms (audio now at %.3fs)"
                      % (p.gen, p.delay_us // 1000, p.pos_s))
            avail = dec.end_bytes // 4 * 4 - fh.tell()
            if avail < CHUNK_BYTES and not (dec.complete and avail > 0):
                if dec.error:
                    raise RuntimeError(dec.error)
                if dec.complete:
                    break  # the track is over
                if not dec.alive:
                    raise RuntimeError("decoder gone at %.1fs" % dec.end_s)
                p.waits += 1
                await asyncio.sleep(0.02)
                continue
            chunk = fh.read(min(CHUNK_BYTES, avail))
            chunk = chunk[: len(chunk) // 4 * 4]
            stream.prepare_audio(chunk, AUDIO_FORMAT)
            if pin_us is not None:
                # Pin the timeline: this chunk is film time pos_s and plays at
                # t0 + pos_s. Every later commit continues from it.
                play_us = await stream.commit_audio(play_start_us=pin_us)
            else:
                play_us = await stream.commit_audio()
            if STATE["pusher"] is not p:
                return
            expected_us = p.t0_us + int(p.pos_s * 1_000_000)
            if abs(play_us - expected_us) > 20_000:
                # The stream fell behind real time (a cache stall) and the
                # library rebased its timeline: the DAC now plays this chunk
                # later than the pin said. Follow it, and say so.
                print("bridge: gen=%s timeline slipped %+.0fms at %.1fs"
                      % (p.gen, (play_us - expected_us) / 1000.0, p.pos_s))
                p.t0_us = play_us - int(p.pos_s * 1_000_000)
            chunk_us = int(len(chunk) / BYTES_PER_S * 1_000_000)
            p.chunks += 1
            p.pos_s += len(chunk) / BYTES_PER_S
            p.next_play_us = play_us + chunk_us
            dec.head_s = p.pos_s
            p.ahead_us = p.next_play_us - stream.now_us()
            if not p.live:
                p.live = True
                if not live_future.done():
                    live_future.set_result(p.t0_us)
                print("bridge: audio live gen=%s from %.3fs t0_us=%s (%.2fs after start, %d waits)"
                      % (p.gen, p.pos_s - len(chunk) / BYTES_PER_S, p.t0_us,
                         time.monotonic() - p.started_at, p.waits))
            await stream.sleep_to_limit_buffer(limit_us)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported via live_future / next /status
        print("bridge: gen=%s push failed: %s" % (p.gen, exc))
        if not live_future.done():
            live_future.set_exception(exc)
    finally:
        fh.close()
        if STATE["pusher"] is p:
            STATE["pusher"] = None
            # A push that nobody asked to end -- the track ran out, or it
            # failed. _stop_pusher(), which is where /stop and /release end a
            # stream, reads STATE["pusher"], and it has just been cleared, so
            # this is the last chance to tell the player the stream is over.
            # Without it the player keeps the last state it was given, which
            # after a film that played to its end is "playing": that is the
            # Sendspin client left running in Music Assistant when Fight Club
            # ran out on 2026-09-17. A stop never showed it, because a stop
            # tears the stream down while the push is still alive.
            with contextlib.suppress(Exception):
                stream.stop()
        print("bridge: gen=%s push ended at %.1fs after %d chunks (%d waits, %d decoder stalls)"
              % (p.gen, p.pos_s, p.chunks, p.waits, dec.stalls))
        if not live_future.done():
            live_future.set_exception(RuntimeError("push ended before first audio"))


def _clamp_delay(us):
    return max(DELAY_MIN_US, min(DELAY_MAX_US, int(us)))


def _send_ahead_us(stream):
    """The earliest the player can be asked to play a first chunk."""
    try:
        return int(stream._min_send_ahead_us()) + FIRST_CHUNK_MARGIN_US  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return 500_000 + FIRST_CHUNK_MARGIN_US


# ---- HTTP API --------------------------------------------------------------

async def handle_players(request):
    connected_url = _connected_url()
    players = sorted(PLAYERS.values(), key=lambda p: p["name"].lower())
    return web.json_response(
        {
            "players": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "url": p["url"],
                    "connected": p["url"] == connected_url,
                }
                for p in players
            ],
            "default": _default_player_url(),
        }
    )


async def handle_connect(request):
    global CLIENT_ID, ACTIVE_URL

    body = {}
    with contextlib.suppress(Exception):
        if request.body_exists:
            body = await request.json()
    target_url = (body or {}).get("url") or _default_player_url()
    if not target_url:
        return web.json_response(
            {"error": "no player: none named, none configured, none discovered"}, status=503
        )

    client = _get_client()
    if (
        client is not None
        and client.is_connected
        and _player_role(client) is not None
        and ACTIVE_URL == target_url
    ):
        return web.json_response(
            {"connected": True, "client_id": CLIENT_ID, "name": client.info.name}
        )

    if ACTIVE_URL is not None:
        # A different player, or the same one after its connection dropped:
        # either way the old registration is dead weight. The decoded audio
        # is not: the film is still on screen.
        await _disconnect_active(keep_cache=True)

    deadline = time.monotonic() + CONNECT_TIMEOUT_S
    try:
        await asyncio.wait_for(
            SERVER.connect_to_client_and_wait(
                target_url,
                connection_reason=ConnectionReason.PLAYBACK,
                retry_initial_connection=True,
            ),
            timeout=CONNECT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        print("bridge: connect to %s timed out after %ss" % (target_url, CONNECT_TIMEOUT_S))
        with contextlib.suppress(Exception):
            SERVER.disconnect_from_client(target_url)
        return web.json_response(
            {"error": "connect timed out after %ss" % CONNECT_TIMEOUT_S}, status=503
        )
    except Exception as exc:  # noqa: BLE001
        print("bridge: connect to %s failed: %s" % (target_url, exc))
        return web.json_response({"error": "connect failed: %s" % exc}, status=503)

    # connect_to_client_and_wait resolves as soon as the websocket is up, which is
    # before the hello exchange that registers client_id against the url -- so poll
    # rather than reading it once and declaring the connection unusable.
    client_id = SERVER.get_client_id_for_url(target_url)
    while client_id is None and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        client_id = SERVER.get_client_id_for_url(target_url)
    if client_id is None:
        return web.json_response({"error": "connected but client_id unknown"}, status=503)
    CLIENT_ID = client_id
    ACTIVE_URL = target_url
    # Right after a disconnect the id can still resolve to the old, dead
    # client object for a moment; keep looking it up until the new one is
    # connected and its player role is up, or give up at the deadline.
    client = SERVER.get_client(client_id)
    while (client is None or not client.is_connected or _player_role(client) is None) \
            and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        client = SERVER.get_client(client_id)
    if client is None or not client.is_connected:
        return web.json_response({"error": "client registered but not connected"}, status=503)
    if _player_role(client) is None:
        return web.json_response({"error": "player role never activated"}, status=503)

    if client.info.unpaired_access.enabled:
        await SERVER.trust_unpaired(client_id)

    print("bridge: connected client_id=%s name=%s" % (client_id, client.info.name))
    return web.json_response({"connected": True, "client_id": client_id, "name": client.info.name})


async def handle_prepare(request):
    data = await request.json()
    src = data["src"]
    aidx = int(data.get("aidx", 0))
    centre = bool(data.get("centre"))
    start_s = float(data.get("start_s", 0))
    async with _lock:
        try:
            dec = await _ensure_decoder(src, aidx, start_s, centre)
        except Exception as exc:  # noqa: BLE001 - docker missing, fork failure
            print("bridge: prepare could not spawn ffmpeg: %s" % exc)
            return web.json_response({"error": "could not start decoder: %s" % exc}, status=503)
    return web.json_response({"prepared": True, "cache": dec.info()})


async def handle_start(request):
    data = await request.json()
    src = data["src"]
    aidx = int(data.get("aidx", 0))
    centre = bool(data.get("centre"))
    start_s = float(data.get("start_s", 0))
    gen = data.get("gen")
    pos_at_us = data.get("pos_at_us")
    if data.get("delay_ms") is not None:
        STATE["delay_us"] = _clamp_delay(int(data["delay_ms"]) * 1000)

    async with _lock:
        if gen is None:
            gen = STATE["gen"] + 1
        await _stop_pusher()
        STATE["gen"] = gen

        client = _get_client()
        if client is None or not client.is_connected:
            print("bridge: start gen=%s refused: not connected" % gen)
            return web.json_response({"error": "not connected"}, status=503)
        if _player_role(client) is None:
            print("bridge: start gen=%s refused: no active player role" % gen)
            return web.json_response({"error": "no active player role"}, status=503)

        try:
            dec = await _ensure_decoder(src, aidx, start_s, centre)
        except Exception as exc:  # noqa: BLE001
            print("bridge: start gen=%s could not spawn ffmpeg: %s" % (gen, exc))
            return web.json_response({"error": "could not start decoder: %s" % exc}, status=503)

        stream = client.group.start_stream()
        stream.set_live_source(False)
        # The caller says film time start_s was on screen at pos_at_us (its
        # monotonic clock): that fixes t0. The first chunk we can still get
        # to the DAC in time is the one for film time x0, a send-ahead from
        # now; everything between start_s and x0 has already been shown.
        now_us = SERVER.clock.now_us()
        if pos_at_us is not None:
            t0_us = int(pos_at_us) + _clock_offset_us() - int(start_s * 1_000_000)
        else:
            t0_us = now_us - int(start_s * 1_000_000)
        # The trim rides in t0, so every reader of the timeline (the caller's
        # err_s included) sees where the sound really is.
        delay_us = STATE["delay_us"]
        t0_us += delay_us
        x0_s = max(start_s, (now_us + _send_ahead_us(stream) - t0_us) / 1_000_000)
        if x0_s < dec.start_s:
            x0_s = dec.start_s
        p = Pusher(gen, dec, stream, x0_s, t0_us, delay_us)
        dec.head_s = x0_s
        STATE["pusher"] = p
        print("bridge: start gen=%s at %.3fs (asked %.3fs, cache %.1f-%.1fs, player floor %dms)"
              % (gen, x0_s, start_s, dec.start_s, dec.end_s, (_send_ahead_us(stream) - FIRST_CHUNK_MARGIN_US) // 1000))
        live_future = asyncio.get_event_loop().create_future()
        # Whoever is still around to read it does; nobody may be, when the
        # start went pending and the caller has moved on to /status.
        live_future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        p.task = asyncio.ensure_future(_push_loop(p, live_future))

    try:
        t0_us = await asyncio.wait_for(asyncio.shield(live_future), timeout=START_TIMEOUT_S)
    except asyncio.TimeoutError:
        # The cache has not reached x0 yet (a fresh decoder is still seeking
        # the source). Leave it: the push goes live when the bytes land, and
        # the caller reads t0 off /status.
        return web.json_response(
            {"gen": gen, "pending": True, "clock_offset_us": _clock_offset_us()}, status=202
        )
    except Exception as exc:  # noqa: BLE001
        print("bridge: start gen=%s failed: %s" % (gen, exc))
        # start_stream() put the group into PLAYING for a stream that turned
        # out to have nothing in it -- the usual case being a start asked for
        # past the end of a track that has already played out. The push loop
        # has ended that stream, but the group's own state is only cleared by
        # stopping it, and a player left in PLAYING is a player that never
        # comes back to Music Assistant. Only ours: a start that has since
        # been superseded belongs to whoever replaced it.
        async with _lock:
            if STATE["gen"] == gen:
                await _stop_pusher()
                client = _get_client()
                if client is not None:
                    await _stop_group(client)
        return web.json_response({"error": "start failed: %s" % exc}, status=503)

    return web.json_response({"gen": gen, "t0_us": t0_us, "clock_offset_us": _clock_offset_us()})


def status_snapshot():
    """Everything /status says, as plain data. Only a live push on a
    connected player has a timeline; a stopped or superseded one has none."""
    client = _get_client()
    connected = bool(client is not None and client.is_connected)
    dec = STATE["decoder"]
    p = STATE["pusher"]
    pushing = bool(p is not None and p.task is not None and not p.task.done())
    streaming = bool(connected and pushing and p.live)
    pending = bool(pushing and not p.live)
    now_us = SERVER.clock.now_us() if SERVER is not None else (time.monotonic_ns() // 1000)
    t0_us = p.t0_us if streaming else None
    audio_pos_s = (now_us - t0_us) / 1_000_000 if t0_us is not None else None
    role = _player_role(client)
    return {
        "connected": connected,
        "streaming": streaming,
        "pending": pending,
        "gen": p.gen if p is not None else STATE["gen"],
        "t0_us": t0_us,
        "now_us": now_us,
        "audio_pos_s": audio_pos_s,
        # Whether audio can still come: a decoder running, or a cache that
        # is complete and being played.
        "ffmpeg_alive": bool(dec is not None and (dec.alive or (dec.complete and pushing))),
        "volume": role.volume if role is not None else None,
        "clock_offset_us": _clock_offset_us() if SERVER is not None else 0,
        "player_url": ACTIVE_URL if connected else None,
        "player_name": client.info.name if connected else None,
        "cache": dec.info() if dec is not None else None,
        "supply": p.supply() if p is not None else None,
        # What the live timeline actually carries, not what was last asked for.
        "delay_ms": (p.delay_us if p is not None else STATE["delay_us"]) // 1000,
    }


async def handle_status(request):
    return web.json_response(status_snapshot())


async def handle_delay(request):
    """Set the lip-sync trim, in ms, positive for sound heard later. Applied to
    the running push at the end of its queued audio; no restart, no gap."""
    data = await request.json()
    try:
        STATE["delay_us"] = _clamp_delay(int(data.get("ms", 0)) * 1000)
    except (TypeError, ValueError):
        return web.json_response({"error": "ms must be an integer"}, status=400)
    return web.json_response({"delay_ms": STATE["delay_us"] // 1000})


async def handle_stop(request):
    """Pause: the push stops, the decoder and its cache stay for the resume."""
    async with _lock:
        await _stop_pusher()
        client = _get_client()
        if client is not None:
            await _stop_group(client)
    print("bridge: stopped")
    return web.json_response({"stopped": True})


async def handle_release(request):
    await _disconnect_active()
    print("bridge: released")
    return web.json_response({"released": True})


async def handle_volume(request):
    data = await request.json()
    client = _get_client()
    role = _player_role(client)
    if client is None or not client.is_connected or role is None:
        return web.json_response({"error": "not connected"}, status=503)

    if "level" in data:
        level = int(data["level"])
    elif "delta" in data:
        level = role.volume + int(data["delta"])
    else:
        return web.json_response({"error": "level or delta required"}, status=400)
    level = max(0, min(100, level))

    role.set_volume(level)
    return web.json_response({"volume": level})


async def main():
    global SERVER, AZC, BROWSER

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    identity = _load_or_create_identity(STATE_DIR / "sendspin_identity.json")
    pairing_store = await FileServerPairingStore.open(STATE_DIR / "sendspin_pairing.json")

    loop = asyncio.get_event_loop()
    SERVER = SendspinServer(
        loop,
        identity,
        "Cinematica",
        pairing_store=pairing_store,
        # loftpi's sendspin daemon opens with a plaintext client/hello rather than
        # the Noise handshake, so a strict server aborts before the hello exchange
        # ("unexpected first frame type 'client/hello'") and never registers a
        # client_id. Music Assistant holds the same player with this same flag.
        allow_unencrypted=True,
        clock=RawMonotonicClock(),
    )

    if not SENDSPIN_CLIENT_URL:
        print("bridge: SENDSPIN_CLIENT_URL not set -- falling back to /connect {url} or /players default")

    # A previous life of this process (crash, restart mid-film) may have left
    # its ffmpeg running in the container; nothing else will ever reap it.
    await _kill_container_ffmpeg(FFMPEG_MARKER_PREFIX)
    await _kill_container_ffmpeg(FFMPEG_LEGACY_PATTERN)
    for stale in CACHE_DIR.glob("sendspin-*.pcm"):
        with contextlib.suppress(OSError):
            stale.unlink()

    AZC = AsyncZeroconf()
    BROWSER = AsyncServiceBrowser(
        AZC.zeroconf, SENDSPIN_SERVICE_TYPE, handlers=[_on_service_state_change]
    )

    app = web.Application()
    app.router.add_get("/players", handle_players)
    app.router.add_post("/connect", handle_connect)
    app.router.add_post("/prepare", handle_prepare)
    app.router.add_post("/start", handle_start)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/delay", handle_delay)
    app.router.add_post("/stop", handle_stop)
    app.router.add_post("/release", handle_release)
    app.router.add_post("/volume", handle_volume)

    # A caller that gives up on a slow /start must not cancel the teardown it
    # was waiting on half way through: handlers run to completion regardless
    # of the client. (aiohttp's default since 3.7; stated here on purpose.)
    runner = web.AppRunner(app, handler_cancellation=False)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", BRIDGE_PORT)
    await site.start()
    print("bridge: listening on 127.0.0.1:%d client_url=%s" % (BRIDGE_PORT, SENDSPIN_CLIENT_URL or "(unset)"))

    try:
        await asyncio.Event().wait()
    finally:
        await _disconnect_active()
        if BROWSER is not None:
            with contextlib.suppress(Exception):
                await BROWSER.async_cancel()
        if AZC is not None:
            with contextlib.suppress(Exception):
                await AZC.async_close()
        await runner.cleanup()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
