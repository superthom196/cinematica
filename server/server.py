#!/usr/bin/env python3
"""
Cinematica — browse a pluggable catalogue, see the best available stream, and
hand it to the player on the TV.

Stdlib only, deliberately: no pip install, nothing to break on a rebuild.

  catalogue/metadata/streams providers -> reached only through providers/gateway.py
  Stremio    -> http://{PUBLIC_HOST}:11470/{infoHash}/{fileIdx}
  TV app     -> picks that URL up over the heartbeat channel and plays it
  adb        -> optional: wakes the TV app to the foreground (phone remote)
"""
import hmac, io, ipaddress, json, math, os, queue, random, re, shutil, subprocess, sys, tarfile, tempfile, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE      = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from providers import contract, gateway   # noqa: E402
import browser_play   # noqa: E402 -- the HLS grid/tfdt helpers the packager needs
import shelf          # noqa: E402 -- favourites/watched/resume, rules kept out of here
# Defaults to .env beside server.py. Was hardcoded to a second directory in
# $HOME, which is the only reason that directory still existed.
ENV_FILE  = os.environ.get("ENV_FILE", os.path.join(HERE, ".env"))
PORT      = int(os.environ.get("PORT", 8090))
# The name this box answers to on the LAN. Both the phone and the TV app reach
# the server here, and the converted-audio URL is handed to the player under this
# name, so it cannot stay hardcoded the way audio_url() had it -- a second
# install on a differently-named host served URLs its own TV could not resolve.
def _default_host():
    """This machine's address, for URLs the TV and the phone have to reach.

    install.sh always sets PUBLIC_HOST explicitly, so this only matters for a
    hand-started server -- but it used to default to the author's own box name,
    which is not a sensible thing to ship to anyone else. Ask the OS which
    interface reaches the outside world (the UDP connect does no traffic) and
    fall back to loopback.
    """
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.settimeout(0.5)
            sk.connect(("192.0.2.1", 9))      # TEST-NET-1: routed, never answers
            return sk.getsockname()[0]
    except Exception:
        return "127.0.0.1"

PUBLIC_HOST = os.environ.get("PUBLIC_HOST") or _default_host()
STREMIO   = os.environ.get("STREMIO", f"http://{PUBLIC_HOST}:11470")
# The names this server answers to. A page on the public internet can point a
# hostname it owns at this box's private address -- DNS rebinding -- and from
# then on the browser treats it as same-origin, so _origin_ok() below sees
# Origin and Host agree and waves it through. Both of them say the attacker's
# name, which is the tell: the one thing the attack cannot fake is being
# addressed to a name this server actually has. An IP literal is always
# accepted, because the attack needs a name whose DNS it controls and cannot
# make a browser put someone else's address in Host.
HOST_ALLOW = tuple(h.strip().lower() for h in
                   os.environ.get("HOST_ALLOW", "").split(",") if h.strip())
# Suffixes nobody can register against this house from the public DNS: .lan and
# .local are link-local, and a .ts.net name exists only inside one tailnet. The
# phone reaches this server under all three, so none of them can be dropped.
HOST_ALLOW_SUFFIX = (".lan", ".local", ".ts.net")
# adb is no longer how a film gets played: the TV app is the player, and it needs
# none of this. What is left is an opt-in "phone remote" for a hand-built install
# without the app, and it is OFF unless an address is given -- empty means
# nothing in this process ever runs the adb binary.
# TV is the previous name for the same setting and still works; deprecated, do
# not use it in new units.
ADB_TV    = os.environ.get("ADB_TV", os.environ.get("TV", ""))
ADB_ENABLED = bool(ADB_TV)
ADB       = os.environ.get("ADB", "/usr/bin/adb")
# The standalone VLC install this used to fall back to is gone from the TV --
# the Cinematica app is the only player left. The app registers itself over the
# heartbeat channel once it is in the foreground; adb's only remaining job is to
# bring it there.
PLAYER    = os.environ.get("PLAYER", "io.github.superthom196.cinematica/.MainActivity")
PLAYER_PKG= PLAYER.split("/")[0]
# How long a TV app counts as connected after its last heartbeat. Longer than
# the app's poll interval, so one lost request is not a disconnect; short enough
# that a TV switched off at the wall stops claiming the player within a poll of
# the phone's health check.
APP_TTL   = float(os.environ.get("APP_TTL", 15))
# How long to wait for the app to report that it is actually playing after being
# told to. Opening a 4K stream and filling the player's own buffer is not instant.
APP_HANDOFF_SECS = int(os.environ.get("APP_HANDOFF_SECS", 60))
# How long to wait for a woken app to show up on the heartbeat channel. adb's
# "am start" returns as soon as the activity is requested, well before the app
# has actually launched and made its first heartbeat.
APP_WAKE_SECS = int(os.environ.get("APP_WAKE_SECS", 20))
# How long a queued command is still worth delivering. Nothing else expires it:
# the app may be off, asleep or wedged, and an order that has sat unacked this
# long describes a world that no longer exists -- an app coming back an hour
# later must not start a film nobody is in the room for.
APP_CMD_TTL = float(os.environ.get("APP_CMD_TTL", 90))
# How long the last reported playback state is still believed after the app's
# heartbeat has gone stale. APP_TTL is deliberately one missed poll, which is
# nothing like "the film ended": without this a 15 s network blip made the
# phone's now-playing strip flap and made cache_watch() empty the cache in the
# middle of the film. Three missed polls before that is believed.
APP_GRACE = float(os.environ.get("APP_GRACE", 3 * APP_TTL))
PAGE      = int(os.environ.get("PAGE_SIZE", 20))    # films per infinite-scroll page
POOL_MAX  = int(os.environ.get("POOL_MAX", 600))   # candidate pool depth per view
CHANNEL_POLL_MIN = int(os.environ.get("CHANNEL_POLL_MIN", 30))  # how often followed channels are checked for uploads
# Beside server.py, like .env, netprofile.json and nowplaying.json. It was
# pinned to one absolute path under $HOME, which silently made any second
# checkout read and overwrite the first one's dataset.
TTL_LIST  = 6 * 3600                # popular list cache
TTL_STREAM= 3 * 3600                # per-film stream cache
TTL_FAIL  = int(os.environ.get("TTL_FAIL", 120))   # transport failures: retry soon
TTL_JOB   = 2 * 3600                # finished/abandoned play-job records
# caches never evicted otherwise grow forever -- _pool's key is a sort+genre
# combination an unauthenticated caller can enumerate at will.
MAX_POOL_ENTRIES   = int(os.environ.get("MAX_POOL_ENTRIES", 40))
MAX_STREAM_ENTRIES = int(os.environ.get("MAX_STREAM_ENTRIES", 2000))
MAX_JOB_ENTRIES    = int(os.environ.get("MAX_JOB_ENTRIES", 200))
# A catalogue's TV vote counts run much lower than film (a well-known show
# can sit under 500), so the pool's rating floor needs its own, lower knob.
TV_MIN_VOTES = int(os.environ.get("TV_MIN_VOTES", 200))
# Sorted purely by vote average, the series pool is anime and K-drama (very
# high averages, and 200 votes is nothing for them) with British TV buried
# under them on vote count, and the film pool leans the same way. With the
# bias on, a pool is built in two tiers instead, roughly what Netflix UK's
# front page does: HOME countries get in at the kind's normal vote floor and
# carry a rating bonus (HOME_W); everything else needs a much higher floor
# (TV_BIG_MIN_VOTES / MOVIE_BIG_MIN_VOTES) so only the widely watched titles
# come through, wherever they were made. Both tiers are held to BIAS_LANG as
# the ORIGINAL language, which is what actually separates anime from The
# Incredibles and a Korean drama from an Irish one -- so no genre is touched
# (the Genres menu keeps that job) and no country is blocklisted. A third
# tier lets the world's famous titles back in regardless of language at
# WORLD_MIN_VOTES: measured 2026-09-16, the biggest anime series sit under
# 9,000 votes while Squid Game has 17,700 and Money Heist 19,700, and
# films split the same way (Parasite 21,300, The Intouchables 18,800), so
# 10,000 is the line. Search is untouched. The TV app's "UK/US bias" setting
# sends ?bias=0/1 on every request; BIAS is what a caller without a switch
# (the phone page) gets.
def _countries(s):
    return sorted({c.strip().upper() for c in s.split(",") if c.strip()})
BIAS            = os.environ.get("BIAS", "1").lower() not in ("0", "", "false", "no", "off")
BIAS_LANG       = os.environ.get("BIAS_LANG", "en").strip().lower()   # ISO 639-1; "" = any
HOME_COUNTRIES  = _countries(os.environ.get("HOME_COUNTRIES", "GB"))
TV_BIG_MIN_VOTES    = int(os.environ.get("TV_BIG_MIN_VOTES", 1500))
MOVIE_BIG_MIN_VOTES = int(os.environ.get("MOVIE_BIG_MIN_VOTES", 3000))
WORLD_MIN_VOTES     = int(os.environ.get("WORLD_MIN_VOTES", 10000))   # any language; 0 = no such tier
HOME_W          = float(os.environ.get("HOME_W", 0.5))   # rating points added to a home title

# ---- device limits, measured on the Sony KD-55XF8096 (2018, Android 9) -------
# HEVC decoder tops out at 4096x2304 / 60 Mbps; there is NO AV1 decoder at all,
# and no transcode fallback anywhere, so an AV1 pick is simply a dead end.
MAX_GB_4K   = float(os.environ.get("MAX_GB_4K", 25))   # hard ceiling regardless
# What the VPN'd link actually sustains on a torrent swarm, measured: 0.9-3.6
# MB/s, typically ~1.5-1.8. A file whose bitrate exceeds this CANNOT be fixed by
# pre-buffering -- the buffer just drains at the difference and stalls. So the
# real fix is refusing to pick such files in the first place.
SUSTAIN_MBPS = float(os.environ.get("SUSTAIN_MBPS", 12))   # base budget, retuned by netcheck
# ---- link assessment --------------------------------------------------------
# SUSTAIN_MBPS is a budget for TORRENT throughput over the VPN, which is nothing
# like a single TLS stream to a CDN: measured on this link, plain HTTP ran ~82
# Mbps while the value that actually works is 8. So the primary signal is what
# real swarms have delivered -- probe_and_buffer() already measures that for
# every film it buffers -- and the HTTP probe is used for sizing peer
# connections and as a sanity ceiling, never on its own for the budget.
NET_FILE     = os.path.join(HERE, "netprofile.json")
NET_SECS     = float(os.environ.get("NET_SECS", 8))        # seconds per endpoint
# Plain HTTP deliberately, not HTTPS. A flaky link can corrupt sustained TLS
# transfers -- the "bad record MAC" that makes docker pull restart every layer
# forever -- and an 8-second read is long enough to hit that every time,
# while a short curl succeeds. Measured: both endpoints fail over HTTPS and give
# ~85 Mbps over HTTP. A throughput probe carries nothing worth encrypting.
NET_URLS     = [u for u in os.environ.get("NET_URLS",
                "http://cachefly.cachefly.net/100mb.test,"
                "http://fsn1-speed.hetzner.com/100MB.bin").split(",") if u.strip()]
NET_TRIES    = int(os.environ.get("NET_TRIES", 2))   # this link drops transfers
# HTTP -> budget, used ONLY until three real streams have been measured. 0.10
# from the one link there is evidence for: 87 Mbps measured over HTTP against a
# hand-tuned budget of 8 that demonstrably works, i.e. ~0.09. Deliberately close
# to that rather than optimistic -- being too generous offers films the swarm
# cannot sustain, which is the failure this number exists to avoid.
NET_FRACTION = float(os.environ.get("NET_FRACTION", 0.10))
NET_SAMPLES  = int(os.environ.get("NET_SAMPLES", 20))       # swarm rates remembered
NET_PCT      = float(os.environ.get("NET_PCT", 0.25))       # percentile of those to trust
NET_MIN_OBS  = int(os.environ.get("NET_MIN_OBS", 3))        # ordinary swarms needed first
# First-run calibration: rather than waiting for three real films to go by --
# during which the budget is a guess and the first film is the one that matters
# -- measure real swarms up front, once.
CAL_FILMS    = int(os.environ.get("CAL_FILMS", 4))          # ordinary swarms to sample
CAL_SECS     = float(os.environ.get("CAL_SECS", 30))        # seconds of download each
CAL_MAX      = float(os.environ.get("CAL_MAX", 600))        # overall ceiling, 10 min
# Stremio keeps whole films so a resume does not re-download. That is not wanted
# here: the disk is worth more than the re-download, so the cache is capped and
# emptied when playback ends.
CACHE_GB     = float(os.environ.get("CACHE_GB", 30))
# How much to ask for per sample. Stremio buffers well past the requested range
# -- asking for BUFFER_MAX (1 GB) left every sampled film pulling a gigabyte in
# the background long after its 30s measurement had finished.
CAL_MB       = int(os.environ.get("CAL_MB", 128))
SUSTAIN_MIN, SUSTAIN_MAX = 4.0, 40.0
CONNS_MIN, CONNS_MAX     = 60, 180
WELL_SEEDED  = int(os.environ.get("WELL_SEEDED", 200))     # peers for the bonus
SEED_BONUS   = float(os.environ.get("SEED_BONUS", 1.5))    # extra budget when well seeded
# H.265 only. AV1 has no decoder on this panel, and H.264 needs far more bitrate
# for the same quality -- which the peer-starved swarm cannot deliver. Releases
# with no codec token in the name are rejected too: they cannot be verified.
HEVC_ONLY    = os.environ.get("HEVC_ONLY", "1") == "1"
# A torrent index's seeder counts are scraped and unreliable -- a "53 seeder"
# torrent can deliver nothing while a "34 seeder" one flies. There is no way
# to tell from metadata, so instead of guessing we PROBE each candidate and
# move on.
ATTEMPTS    = int(os.environ.get("ATTEMPTS", 5))     # candidates to try per film
DEAD_SECS   = int(os.environ.get("DEAD_SECS", 25))   # no bytes at all -> abandon
PROBE_SECS  = int(os.environ.get("PROBE_SECS", 35))  # too slow by now -> abandon
SLOW_RATIO  = float(os.environ.get("SLOW_RATIO", 0.7))
# When runtime_min is unknown, need_bps is 0 and the SLOW_RATIO check above is
# disabled outright -- nothing stops a trickling swarm from buffering forever.
# This is the backstop: no candidate gets longer than this, full stop.
HARD_CAP_SECS = int(os.environ.get("HARD_CAP_SECS", 240))

# --- ordering -----------------------------------------------------------------
# Sorting purely by IMDb rating is permanently stuck in 1994. "balanced" adds a
# recency bonus that decays with age, so a well-reviewed recent film can out-rank
# a canonical classic without letting poorly-rated new releases in (they still
# have to clear the rating and vote floors).
RECENCY_W    = float(os.environ.get("RECENCY_W", 1.2))    # max IMDb-points bonus
RECENCY_SPAN = float(os.environ.get("RECENCY_SPAN", 20))  # years until bonus is 0
RECENT_YEARS = int(os.environ.get("RECENT_YEARS", 5))     # window for "recent"
MIN_RATING   = float(os.environ.get("MIN_RATING", 6.5))   # keeps the shit out
MIN_IMDB_VOTES = int(os.environ.get("MIN_IMDB_VOTES", 2000))
SORTS = ("top", "balanced", "recent")
# How many search candidates may be resolved before giving up looking for
# playable ones. Each resolution is a metadata + streams round trip, so this
# is the ceiling on how slow a fruitless search can get.
SEARCH_POOL = int(os.environ.get("SEARCH_POOL", 120))
MIN_SEEDERS = int(os.environ.get("MIN_SEEDERS", 20))   # "a decent amount of peers"
# Below these the release is a heavy re-encode: it still carries a 4K tag but
# will not look like one. Bitrate, not file size, is what decides that -- a 20 GB
# three-hour film is roughly a 13 GB two-hour one. Flagged rather than rejected,
# because it may be the only thing available today and a better release often
# appears in the swarm a day later.
LOW_MBPS_4K = float(os.environ.get("LOW_MBPS_4K", 5.0))
LOW_MBPS_HD = float(os.environ.get("LOW_MBPS_HD", 2.5))
RESOLVE_CHUNK = int(os.environ.get("RESOLVE_CHUNK", 20))  # candidates resolved per pass
RESOLVE_FIRST = int(os.environ.get("RESOLVE_FIRST", 10))  # ...fewer, for the first row
BUFFER_SECS = int(os.environ.get("BUFFER_SECS", 90))   # seconds of video to pre-load
BUFFER_MIN  = int(os.environ.get("BUFFER_MIN_MB", 40)) * 1048576
BUFFER_MAX  = int(os.environ.get("BUFFER_MAX_MB", 400)) * 1048576
# Matroska keeps its seek index (Cues/SeekHead) at the END of the file, and every
# player reads it the moment it opens the stream. Stremio downloads strictly
# sequentially, so that tail is never cached and the player sits on a spinner
# while those pieces are fetched out of order. Pre-fetch the tail too.
TAIL_MB     = int(os.environ.get("TAIL_MB", 8))
# The tail fetch runs BEFORE probe_and_buffer's guarded loop, so without its own
# ceiling it was bounded only by the socket timeout: a swarm trickling a few KB/s
# could sit in "fetching the seek index..." for ten minutes, per candidate.
TAIL_SECS   = int(os.environ.get("TAIL_SECS", 90))
# This TV plays AC3 cleanly but its VLC software-decodes AAC 5.1 and the audio
# clock drifts -- audible break-up while the video stays perfect. Measured at
# 21x realtime on this Pi, remuxing the SAME 4K video untouched and re-encoding
# only the audio to AC3 is nearly free, so AAC picks get piped through ffmpeg.
FFMPEG      = os.environ.get("FFMPEG", "/usr/lib/jellyfin-ffmpeg/ffmpeg")
FFMPEG_CTR  = os.environ.get("FFMPEG_CTR", "stremio-server")
AUDIO_FIX   = os.environ.get("AUDIO_FIX", "1") == "1"
# ffmpeg runs INSIDE the stremio container, which uses Docker's DNS and cannot
# resolve the host name -- it must reach the server on the container's own loopback.
STREMIO_IN  = os.environ.get("STREMIO_INTERNAL", "http://127.0.0.1:11470")
FFPROBE     = os.environ.get("FFPROBE", "/usr/lib/jellyfin-ffmpeg/ffprobe")
# Codecs this panel decodes in hardware. Anything else -- DTS, AAC multichannel,
# TrueHD -- gets software-decoded by VLC and the audio clock drifts. Guessing
# this from the release name does not work: "Shawshank [2160p x265 10bit FS97
# Joy]" carries no audio token at all and is actually DTS 5.1. So probe it.
NATIVE_AUDIO = tuple(os.environ.get("NATIVE_AUDIO", "ac3,eac3").split(","))
FOURK_ONLY  = os.environ.get("FOURK_ONLY", "1") == "1"
AUTOPLAY_NEXT = os.environ.get("AUTOPLAY_NEXT", "1") == "1"
# A film nobody in this house can follow is not a film. Two layers enforce it:
# the listing layer reads whatever languages the streams provider reports on
# the candidate itself, the file layer reads the audio tracks ffprobe finds
# in the actual file. PREF_LANG is an ISO 639-1 code -- ffprobe reports 639-2
# ("eng"), so LANG_TAGS below bridges the two.
PREF_LANG   = os.environ.get("PREF_LANG", "en")
REJECT_LANG = os.environ.get("REJECT_LANG", "1") == "1"
# Hardcoded ("burnt-in") subtitles are part of the picture: no player can turn
# them off, so an HC/KORSUB release is unwatchable here no matter how good the
# swarm is. Release names mark them reliably, which is why this one needs no
# file-level counterpart.
REJECT_HARDSUB = os.environ.get("REJECT_HARDSUB", "1") == "1"
# The bridge sidecar that owns the hi-fi DAC. Its own process, its own port --
# server.py never touches the device directly, it only tells the bridge what
# to play and reads back where the audio actually is.
SENDSPIN_BRIDGE = os.environ.get("SENDSPIN_BRIDGE", "http://127.0.0.1:8091")

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

ENV       = load_env(ENV_FILE)
# Hi-fi only makes sense once the bridge sidecar is around to receive it --
# this is the one flag that turns the whole bridge codepath on. The TV now
# picks which Sendspin player to use at runtime (GET /api/hifi/players), so
# a fixed client URL is no longer required -- the bridge's own presence is
# what matters.
SENDSPIN_CLIENT_URL = ENV.get("SENDSPIN_CLIENT_URL") or os.environ.get("SENDSPIN_CLIENT_URL")
SENDSPIN_ENABLED = os.environ.get("SENDSPIN", "1") == "1"
# One clock, one follower: the bridge's DAC timeline is the reference and the
# TV corrects its picture against it. The audio itself is only ever restarted
# for an explicit viewer seek (the TV's seek_seq), a pause/buffering recovery,
# a source change, or a stream that actually died -- never on a drift reading,
# which is the TV's to close. A start that fails is retried with a gap that
# doubles from MIN to MAX, so a bridge with no player is asked every 30 s, not
# every 3 s. HIFI_START_TIMEOUT_S bounds one start end to end: the worker's
# connect (up to HIFI_CONNECT_TIMEOUT_S), the bridge's teardown of the previous
# decoder, and ffmpeg's seek of an MKV over the swarm.
HIFI_RESTART_MIN_GAP_S = 3.0
HIFI_RESTART_MAX_GAP_S = 30.0
HIFI_START_TIMEOUT_S = 45.0
# The bridge's /connect is bounded by its CONNECT_TIMEOUT_S (10 s) plus one
# decoder teardown (TEARDOWN_MAX_S, 11 s); /start, /stop and /release by a
# teardown plus a bounded group stop. These are set above those bounds, so a
# timeout here means the bridge is wedged, not merely busy.
HIFI_CONNECT_TIMEOUT_S = 25.0
HIFI_CALL_TIMEOUT_S = 30.0
HIFI_AUDIO_DELAY_MS = int(os.environ.get("HIFI_AUDIO_DELAY_MS", "0"))
# How close to the end of the decoded track counts as the end of it. The last
# seconds of a film are the one place a stopped stream is not a fault, and a
# start this close to the end could not land anyway: the player will not take
# a first chunk less than its send-ahead floor (~1 s) from now.
HIFI_TRACK_END_S = 2.0

# No service credentials here any more. A catalogue key or a stream-index
# config string belongs to the provider that needs it, is entered in the
# browser, and is stored outside this directory. ENV is still read for the
# legacy values so stage-6 migration can offer to import them -- offer, not
# adopt: finding an old provider API key in a .env must never silently
# switch a service on.

# Cloudflare 403s the default "Python-urllib/x.y" agent outright, so every
# request has to carry a normal-looking UA. Found the hard way.
UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")

def http_json(url, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

# ---- ratings ---------------------------------------------------------------
# Cinematica used to download IMDb's whole daily ratings dataset on startup,
# because the old fixed catalogue couldn't sort by IMDb rating and didn't
# return an IMDb id on its list endpoints. That was a third-party bulk
# download nobody asked for, and a fresh install must not make it. Ratings
# now arrive named, on the entry itself, from whichever provider supplied
# the title -- Cinemeta carries imdbRating inline, and a metadata provider
# that wants the dataset can still fetch it itself if its owner turns that
# on.
#
# Named, because "the rating" stopped meaning anything once the catalogue became
# pluggable: MIN_RATING is an IMDb floor on an IMDb ten-point scale and must not
# be applied to some other provider's number.

def rating_of(entry, name="imdb"):
    """(value, votes) for a named rating, or None. votes may be None."""
    r = ((entry or {}).get("ratings") or {}).get(name)
    if not r:
        return None
    v = r.get("value")
    return (v, r.get("votes")) if v is not None else None


def own_rating(entry):
    """(value, votes) of the catalogue's own rating -- the first one it reports
    that is not IMDb's -- else IMDb's, else None.

    This is what `vote`/`votes` always meant to the clients: the catalogue's
    number, shown on the detail pages and used by rank() when a title has no
    IMDb rating. The contract files it under the catalogue's own name.
    """
    names = [n for n in ((entry or {}).get("ratings") or {}) if n != "imdb"]
    for name in names + ["imdb"]:
        r = rating_of(entry, name)
        if r:
            return r
    return None


def passes_rating_floor(entry):
    """Whether a title clears the quality floor for the balanced/recent sorts.

    A title with NO imdb rating passes. That is deliberate and it is a change:
    the old code dropped anything the dataset did not know about, which with a
    provider that reports no ratings at all would empty the grid completely.
    A missing optional rating must never be the reason a film cannot be played.
    """
    ir = rating_of(entry, "imdb")
    if not ir:
        return True
    value, votes = ir
    if value < MIN_RATING:
        return False
    return not (votes is not None and votes < MIN_IMDB_VOTES)


# ---- audio-track language matching, written against real ffprobe output -----
# ffprobe writes ISO 639-2 ("eng", and both the bibliographic and terminological
# forms exist for some languages), the listing layer and PREF_LANG speak 639-1.
LANG_TAGS = {
    "en": ("en", "eng"), "es": ("es", "spa"), "ru": ("ru", "rus"),
    "fr": ("fr", "fre", "fra"), "de": ("de", "ger", "deu"), "it": ("it", "ita"),
    "ja": ("ja", "jpn"), "ko": ("ko", "kor"), "pt": ("pt", "por"),
    "hi": ("hi", "hin"), "zh": ("zh", "chi", "zho"), "sv": ("sv", "swe"),
    "nl": ("nl", "dut", "nld"), "pl": ("pl", "pol"), "tr": ("tr", "tur"),
    "uk": ("uk", "ukr"),
}
LANG_NAME = {
    "en": "English", "es": "Spanish", "ru": "Russian", "fr": "French",
    "de": "German", "it": "Italian", "ja": "Japanese", "ko": "Korean",
    "pt": "Portuguese", "hi": "Hindi", "zh": "Chinese", "sv": "Swedish",
    "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "uk": "Ukrainian",
}

def audio_has_lang(tags, want):
    """Does this file carry an audio track in `want` (a 639-1 code)?

    "und" is an untagged track, which is no evidence either way -- plenty of
    English rips tag nothing at all -- so a file whose tracks are all "und"
    passes. Only a file that names its languages and does not name this one
    fails. "en-US"-style tags are matched on their first subtag."""
    known = [t for t in tags if t and t != "und"]
    if not known:
        return True
    alts = set(LANG_TAGS.get(want, (want,)))
    return any(t.split("-")[0] in alts for t in known)

def audio_track_for(tags, want):
    """Index of the first audio track in `want`, else 0 -- the track the conversion should keep."""
    alts = set(LANG_TAGS.get(want, (want,)))
    for i, t in enumerate(tags):
        if t and t != "und" and t.split("-")[0] in alts:
            return i
    return 0

def required_mbps(gb, runtime_min):
    if not gb or not runtime_min:
        return None
    return gb * 8192.0 / (runtime_min * 60.0)

def sustainable_mbps(seeders):
    """What the link can be expected to hold for this source.

    seeders=None means there is no swarm to reason about -- a direct HTTP
    source, where throughput is the link's and the origin's, not a peer
    count's. That gets the base budget with no swarm bonus, rather than being
    treated as a zero-peer torrent and rejected.
    """
    if seeders is None:
        return SUSTAIN_MBPS
    return SUSTAIN_MBPS * (SEED_BONUS if seeders >= WELL_SEEDED else 1.0)

# Measured on this TV: AC3/E-AC3/DTS play cleanly, but AAC 5.1 has to be
# software-decoded and resampled by VLC and its audio clock drifts badly --
# "timing screwed", inserted silence, audible break-up while video stays fine.
# YTS releases are both the smallest (so the bitrate budget favours them) and
# AAC 5.1, so the size preference was actively selecting the broken format.
RE_AC3  = re.compile(r"\b(ac3|eac3|e-?ac-?3|ddp|dd\+|dd[0-9]|dolby ?digital|truehd|atmos|dts)", re.I)
RE_AAC  = re.compile(r"\baac", re.I)          # matches AAC, AAC5.1, AAC2.0

def audio_kind(c):
    blob = (c.get("display") or "") + " " + (c.get("tag") or "")
    if RE_AC3.search(blob):
        return "ac3"
    if RE_AAC.search(blob):
        return "aac"
    return "?"

def score(c, runtime_min=None, relax=False, kind="movie"):
    """Higher is better. Encodes everything the TV and the link can actually do.
    relax=True builds the FALLBACK tail: 1080p allowed, looser peer floor.
    Better to drop to 1080p than to run out of candidates and play nothing.
    kind="tv" relaxes rules that only make sense for a single film: packs are
    the norm for a series and share a swarm across episodes, 1080p episodes
    run ~1GB, and 4K series releases are rare enough not to demand them."""
    # Unknown is not zero. A torrent index reports peers and sizes; a direct
    # HTTP source usually reports neither, and scoring "unknown" as 0 meant a
    # sub-1GB penalty, a failed peer floor and a budget check against a
    # zero-peer swarm -- three separate reasons to reject every single
    # candidate an HTTP-only provider could ever return.
    seeders = c.get("seeders")
    gb = c.get("gb")
    transport = c.get("transport") or ("torrent" if c.get("infoHash") else None)

    # HEVC_ONLY, FOURK_ONLY and the small-file penalty are PREFERENCES, not
    # capabilities. They are on by default because this panel is 4K and a big
    # torrent index offers hundreds of releases of the same film, so there is
    # always another candidate to move to.
    #
    # A modest provider has no such abundance -- a public-domain index carries
    # one 1080p H.264 print of a 1921 film and nothing else -- and enforcing a
    # preference absolutely there rejects the entire catalogue. That is what
    # `relax` already exists for: best_stream() runs a strict pass, then a
    # relaxed pass to build the fallback tail. FOURK_ONLY honoured `relax`;
    # HEVC_ONLY and the sub-1GB penalty did not, so no amount of relaxing could
    # rescue a non-HEVC film and a provider-independent Cinematica would show
    # an empty grid. All three now relax together.
    #
    # AV1 is not in that set: that is a decoder the panel lacks, and no
    # preference relaxes a file that cannot be played.
    if c["codec"] == "AV1":          return -1
    if HEVC_ONLY and not relax and c["codec"] != "HEVC": return -1
    # Burnt into the picture, so no player setting can escape them.
    if REJECT_HARDSUB and c.get("hardsub"): return -1
    # A listing that names its languages and does not name ours is a foreign
    # dub. A listing that names none is NOT rejected -- most English releases
    # carry no flag at all, and probe_media() checks the file itself later.
    langs = c.get("langs") or []
    if REJECT_LANG and langs and PREF_LANG not in langs and "multi" not in langs:
        return -1
    if seeders is not None and seeders < (MIN_SEEDERS // 2 if relax else MIN_SEEDERS):
        return -1
    if transport is None:            return -1       # nothing playable to point at
    if FOURK_ONLY and not relax and not c["is4k"] and kind != "tv":
        return -1  # 4K or nothing
    if gb is not None and gb > MAX_GB_4K:
        if kind == "tv" and c["pack"]:
            # Some indexes report the whole-torrent size for packs; treat it as
            # unknown here -- the probe corrects it later.
            gb = None
        else:
            return -1
    # can the link actually keep up with this file's bitrate?
    req = required_mbps(gb, runtime_min)
    if req is not None:
        c["req_mbps"] = round(req, 1)
        c["thin"] = req < (LOW_MBPS_4K if c["is4k"] else LOW_MBPS_HD)
        budget = sustainable_mbps(seeders)
        c["budget_mbps"] = round(budget, 1)
        if req > budget:
            return -1
    s = 0.0
    c["audio"] = audio_kind(c)
    if c["audio"] == "ac3":
        s_audio = 260          # decodes cleanly on this panel
    elif c["audio"] == "aac":
        s_audio = -240         # drifts and breaks up; avoid unless nothing else
    else:
        s_audio = 0
    s += s_audio
    s += 400 if c["is4k"] else 100
    s += 120 if c["codec"] == "HEVC" else (60 if c["codec"] == "H264" else 0)
    if kind == "tv":
        s += 20 if c["pack"] else 0          # packs share a swarm across episodes
    else:
        s -= 150 if c["pack"] else 0         # packs rank high on seeders but often stall
    s += min(seeders, 400) * 0.4 if seeders is not None else 0
    # Only a claimed size can be suspiciously small, and only when a big
    # release was the expectation. An unstated size says nothing, and in the
    # relaxed pass a small file is often the only print that exists.
    if kind != "tv" and not relax and gb is not None and gb < 1.0: s -= 100
    return s

def best_stream(identity, runtime_min=None, kind="movie", season=None, episode=None):
    """Ask the streams provider for this title's candidates, then run the same
    strict/relaxed split as before: a strict pass demands the panel's
    preferences (4K, HEVC, native audio...), a relaxed pass builds the
    fallback tail so a run of candidates that fail strict scoring degrades to
    a watchable stream instead of nothing playing at all.

    Per-provider throttling (a stream index that rate-limits can answer a
    429, and that used to be cached as "no usable stream" for three hours --
    silently deleting films from the catalogue) now lives in
    gateway._invoke(), which applies it to every provider automatically
    instead of relying on one hand-wired call site to remember to ask for it.

    Raises contract.ProviderError on failure -- callers (get_stream(),
    get_stream_tv()) already catch it and know how to turn a real "not
    found"/"unsupported" answer into a cached soft error versus a transport
    failure into a short retry.
    """
    res = gateway.streams(identity, season, episode)
    cands = res.get("candidates") or []
    rejected = dict(res.get("rejected") or {})
    for c in cands:
        c["score"] = score(c, runtime_min, kind=kind)
    strict = sorted([c for c in cands if c["score"] > 0], key=lambda c: -c["score"])
    # Fallback tail: same title at 1080p / lower peer floor, so a run of dead
    # strict candidates degrades to a watchable stream instead of failing
    # outright.
    seen = {c["key"] for c in strict}
    tail = []
    for c in cands:
        if c["key"] in seen:
            continue
        sc = score(c, runtime_min, relax=True, kind=kind)
        if sc > 0:
            c = dict(c); c["score"] = sc; c["fallback"] = True
            tail.append(c)
    tail.sort(key=lambda c: -c["score"])
    return strict + tail, len(cands), rejected

def probe_full(url_internal):
    """Everything ffprobe knows about the file, video and audio alike.

    probe_media() only ever asked about audio, because the TV panel's question
    was always "can this thing decode the sound" -- video went out as-copied to
    a box with a hardware decoder for everything a source is likely to carry.
    A browser
    has no such guarantee: it needs the video codec, its profile and level, and
    the pixel format before it can say whether it can play the file at all, so
    this widens the same ffprobe call by dropping -select_streams a and asking
    about every stream, instead of duplicating the docker-exec/timeout/JSON
    plumbing a second time for a video-only probe.

    Same call shape as before -- -rw_timeout, -v error, -analyzeduration,
    -probesize, -of json, timeout=120 -- so there is exactly one place that
    knows how to reach into the ffmpeg container. Never raises: a probe that
    throws mid-transcode-decision is worse than one that answers "unknown".
    """
    empty = {"format_name": "", "duration": None, "video": None, "audio": [], "langs": []}
    try:
        r = subprocess.run(
            ["docker", "exec", FFMPEG_CTR, FFPROBE, "-rw_timeout", "30000000", "-v", "error",
             "-show_entries",
             "stream=index,codec_type,codec_name,profile,level,pix_fmt,width,height,channels:"
             "stream_tags=language:format=format_name,duration",
             "-of", "json",
             "-analyzeduration", "5000000", "-probesize", "5000000", url_internal],
            capture_output=True, text=True, timeout=120)
        d = json.loads(r.stdout or "{}")
        streams = d.get("streams") or []
        fmt = d.get("format") or {}
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        langs = [((s.get("tags") or {}).get("language") or "und").strip().lower() or "und"
                 for s in audio]
        try:
            dur = float(fmt.get("duration") or 0) or None
        except (TypeError, ValueError):
            dur = None
        return {"format_name": fmt.get("format_name") or "", "duration": dur,
                "video": video, "audio": audio, "langs": langs}
    except Exception:
        return empty

def probe_media(url_internal):
    """(codec of the first audio track, duration in seconds, audio languages, per-track codecs).

    Duration comes back from the same ffprobe call because it is free here and
    the alternative was expensive: when the catalogue has no runtime for a film, the
    transcode regulator fell back to dividing by a one-minute film and computed
    a bitrate ~100x too high. JSON output rather than the flat format -- with
    two -show_entries sections the line order depends on whether the stream was
    found at all, which is exactly the positional parsing this codebase avoids
    elsewhere.

    ALL audio streams, not a:0: the language question is about the file's set of
    tracks, and one extra -show_entries field costs nothing on a call that is
    already being made. Tracks with no language tag come back as "und".

    Now a thin view over probe_full(): there is only one ffprobe call shape to
    maintain, and this just reshapes its audio list into the tuple callers expect.
    """
    full = probe_full(url_internal)
    audio = full["audio"]
    codec = (audio[0].get("codec_name") or "").strip().lower() or None if audio else None
    codecs = [(s.get("codec_name") or "").strip().lower() or None for s in audio]
    return codec, full["duration"], full["langs"], codecs

def probe_gop(url_internal, window=40):
    """Median gap between keyframes, in seconds, over the first `window` seconds.

    Only the first window seconds: the pre-buffer has already pulled that part
    of the file into the local cache before this is ever called, so reading it
    again costs a couple of seconds of ffprobe time and no extra network
    traffic -- probing the whole file would mean fetching parts nothing else
    needs yet, just to answer a question the first few keyframes already settle.

    The answer picks a segment length. A stream that is being copied rather
    than re-encoded can only be cut on keyframe boundaries, so a segment grid
    finer than the keyframe interval is a promise the copy can't keep -- every
    segment would actually start early or late at the nearest keyframe instead.
    """
    try:
        r = subprocess.run(
            ["docker", "exec", FFMPEG_CTR, FFPROBE, "-rw_timeout", "30000000", "-v", "error",
             "-select_streams", "v:0", "-skip_frame", "nokey",
             "-read_intervals", "%%+%d" % window,
             "-show_entries", "frame=pts_time", "-of", "csv=p=0",
             "-analyzeduration", "5000000", "-probesize", "5000000", url_internal],
            capture_output=True, text=True, timeout=120)
        times = sorted(float(line) for line in (r.stdout or "").splitlines() if line.strip())
        if len(times) < 2:
            return None
        gaps = sorted(b - a for a, b in zip(times, times[1:]))
        n = len(gaps)
        mid = n // 2
        median = gaps[mid] if n % 2 else (gaps[mid - 1] + gaps[mid]) / 2
        if not math.isfinite(median) or median <= 0:
            return None
        return median
    except Exception:
        return None

def probe_keyframes(url_internal, t, window=30):
    """Keyframe presentation times, in seconds, in a window ENDING at t.

    Same ffprobe shape as probe_gop above, and windowed for the same
    reason: this is called once per packager run, against a source that may
    be a torrent still filling in, and reading the whole file to list every
    keyframe would pull down parts nothing is going to watch yet just to
    answer a question about one point in the film.

    The window ends at t because the only keyframe this has to find is the
    one ffmpeg's input seek will land on -- the last one AT OR BEFORE t. A
    window shorter than the GOP finds nothing, so this asks for `window`
    seconds of lead-in, comfortably more than grid()'s largest segment. The
    end of that window is absolute, not a duration -- see
    keyframe_probe_cmd, where getting that wrong hid the one keyframe this
    whole call exists to find.
    Returns [] on any failure; anchor_time() treats that as "assume the
    grid position", which is what this code did before it asked at all.

    The timeout is short on purpose, and much shorter than probe_gop's. That
    one runs once while the film is being prepared and the viewer is already
    watching a progress message; this one runs inside bx_restart, on the
    request thread of the segment fetch that IS the seek, so every second it
    spends is a second of the viewer staring at a stalled scrubber. Giving
    up costs at most one GOP of anchor accuracy -- worth far less than the
    wait.
    """
    end = float(t)
    start = max(0.0, end - window)
    if end <= start:
        return []
    try:
        r = subprocess.run(
            ["docker", "exec", FFMPEG_CTR, FFPROBE]
            + browser_play.keyframe_probe_cmd(url_internal, start, end),
            capture_output=True, text=True, timeout=20)
        return browser_play.parse_keyframe_times(r.stdout)
    except Exception:
        return []

def tc_key(c):
    """The 40-hex id a pick's audio conversion is filed and served under.

    A torrent's infoHash, exactly as before. A direct HTTP source has none --
    it used to go through as the string "None", and /audio/None is refused by
    the route's 40-hex check, so the TV was handed a URL that could only 400.
    Its contract.source_key() has the same shape for exactly this reason.
    """
    if c.get("infoHash"):
        return c["infoHash"]
    return c.get("key") or contract.source_key("http", url=c.get("url"))

def audio_url(c):
    """The transcoding endpoint for this pick."""
    idx = c.get("fileIdx")
    # PUBLIC_HOST, not a name baked in here: this URL is opened by the player on
    # the TV, so it has to be a name that box can resolve.
    return "http://%s:%d/audio/%s%s" % (
        PUBLIC_HOST, PORT, tc_key(c), ("/%d" % idx) if idx is not None else "")

# ---- direct sources, and the credential boundary ----------------------------
# A provider may hand back a plain HTTP URL that needs request headers -- an
# Authorization, a Referer, a signed cookie. Those are credentials, and four
# separate consumers would otherwise need to carry them: ffprobe, the
# transcoder, the TV's player, and the Sendspin bridge's decoder.
#
# None of them do. Core registers the source here and publishes it as a local
# /src/<key> URL; the proxy below is the only place the real URL and its
# headers exist. Every consumer keeps working on a plain, credential-free URL
# exactly as it did when every source was a torrent, which is why none of the
# playback or audio code had to change to support this.
_sources = {}        # key -> {"url":..., "headers":{...}, "at": ts}
TTL_SOURCE = 12 * 3600

def register_source(c):
    """Remember a direct source's real URL and headers; return its local key."""
    if not c or c.get("transport") != "http" or not c.get("url"):
        return None
    key = c.get("key") or contract.source_key("http", url=c["url"])
    with _lock:
        _sources[key] = {"url": c["url"], "headers": dict(c.get("headers") or {}),
                         "at": time.time()}
        _evict(_sources, TTL_SOURCE, MAX_STREAM_ENTRIES)
    return key

def source_for(key):
    with _lock:
        e = _sources.get(key)
        if e:
            # Touch it: a film that is still playing must not have its own
            # source evicted out from under it by an hour of browsing.
            e["at"] = time.time()
        return e

def stream_url(c):
    """The URL the rest of the system plays. Never carries a credential."""
    if c.get("transport") == "http":
        key = register_source(c)
        return "http://%s:%d/src/%s" % (PUBLIC_HOST, PORT, key)
    idx = c.get("fileIdx")
    # fileIdx is genuinely absent on some streams; omit it and let the server pick
    return f"{STREMIO}/{c['infoHash']}" + (f"/{idx}" if idx is not None else "")

def stream_url_internal(c):
    """Same source, reached from inside this box -- what ffprobe and the
    transcoder open. The proxy is local either way, so an HTTP source is the
    same URL; only the Stremio path differs between internal and public."""
    if c.get("transport") == "http":
        return "http://127.0.0.1:%d/src/%s" % (PORT, register_source(c))
    idx = c.get("fileIdx")
    return f"{STREMIO_IN}/{c['infoHash']}" + (f"/{idx}" if idx is not None else "")

def stream_url_public(c):
    """The same source as a RELATIVE url, for a browser.

    stream_url() bakes in PUBLIC_HOST and, for a torrent, the streaming server's
    own port -- both correct for the TV, which is on the LAN, and both useless to
    a browser that reached this page over a tailnet address. A relative url rides
    whatever origin the page was actually opened on, and carries no credential.
    """
    if c.get("transport") == "http":
        return "/src/%s" % register_source(c)
    idx = c.get("fileIdx")
    return "/t/%s" % c["infoHash"] + (("/%d" % idx) if idx is not None else "")

# ---- caches -----------------------------------------------------------------
# RLock, not Lock. Every thread in a 22-thread dump was blocked on this lock with
# none holding it in a visible frame, which means the holder was itself blocked
# taking it a second time -- a permanent self-deadlock that froze the whole
# server. Re-entrancy makes that class of bug impossible rather than fatal.
_lock   = threading.RLock()
# (_pool below replaces the old flat _movies cache)
_genres = {"at": 0, "data": [], "tag": None}
_genres_tv = {"at": 0, "data": [], "tag": None}    # TV genre ids differ from movie ids
# Every key below carries the producing role's gateway.cache_tag() ("<provider
# id>@<config rev>") as well as the id it caches -- a provider swap or a
# config edit (new API key, different index) must never keep serving results
# gathered under the old one, so the tag is part of the key, not a value
# checked after the fact.
_streams= {}        # "<tag>@<id>" (or "<tag>@tv:{id}:{s}:{e}") -> {"at":ts,"pick":c|None,"count":n,"err":str|None}
_tvdet    = {}      # "<tag>@<id>" -> {"at":ts, ...series detail...}, 24h TTL
_tvseason = {}      # "<tag>@<id>:<n>" -> {"at":ts,"episodes":[...]}, 24h TTL

def _gw_kind(kind):
    """server.py's own convention is "movie"/"tv"; the contract speaks
    "movie"/"series" -- bridge the two here rather than push the contract's
    vocabulary through every call site."""
    return contract.KIND_SERIES if kind == "tv" else contract.KIND_MOVIE

def _evict(cache, ttl, cap):
    """Drop expired entries, then cap what's left to the newest `cap` by "at".
    Call with _lock held -- this mutates the dict in place."""
    now = time.time()
    for k in [k for k, v in cache.items() if now - v.get("at", 0) > ttl]:
        del cache[k]
    if len(cache) > cap:
        oldest = sorted(cache.items(), key=lambda kv: kv[1].get("at", 0))
        for k, _ in oldest[:len(cache) - cap]:
            del cache[k]

def pool_served():
    """Films served across every view. Snapshot under the lock: get_page()
    inserts into _pool while this iterates, and a bare sum() over it would raise
    "dictionary changed size during iteration" -- turning the endpoint both UIs
    poll on a timer into a 500."""
    with _lock:
        vals = list(_pool.values())
    return sum(len(v["served"]) for v in vals)

def get_genres(kind="movie"):
    cache = _genres_tv if kind == "tv" else _genres
    tag = gateway.cache_tag(contract.ROLE_CATALOGUE)
    with _lock:
        if cache["data"] and cache.get("tag") == tag and time.time() - cache["at"] < 24 * 3600:
            return cache["data"]
    g = gateway.genres(_gw_kind(kind))
    with _lock:
        cache.update(at=time.time(), data=g, tag=tag)
    return g

def tv_detail(tid):
    """Series detail, cached 24h the same way movie streams are. A series'
    IMDb id lives under its external_ids -- unlike a movie's, it is never a
    top-level field -- so it is read from there, whatever the provider."""
    tag = gateway.cache_tag(contract.ROLE_METADATA)
    key = "%s@%s" % (tag, tid)
    with _lock:
        e = _tvdet.get(key)
        if e and time.time() - e["at"] < 24 * 3600:
            return e
    entry = gateway.details(tid, contract.KIND_SERIES)
    ext = entry.get("external_ids") or {}
    tr = own_rating(entry)    # the series page's ★, as before providers
    e = {"at": time.time(), "id": entry.get("id"), "local_id": entry.get("local_id"),
         "kind": "tv",
         "title": entry.get("title"), "overview": entry.get("overview"),
         "tagline": entry.get("tagline"), "year": entry.get("year"),
         "runtime": entry.get("runtime"),
         "vote": tr[0] if tr else None, "votes": tr[1] if tr else None,
         "first_air": entry.get("first_air"), "last_air": entry.get("last_air"),
         "status": entry.get("status"),
         "genres": entry.get("genres") or [],
         "seasons": entry.get("seasons") or [],
         "backdrop": entry.get("backdrop"), "poster": entry.get("poster"),
         "ratings": entry.get("ratings") or {},
         "external_ids": ext,
         "imdb_id": ext.get("imdb")}
    with _lock:
        _tvdet[key] = e
        _evict(_tvdet, 24 * 3600, MAX_STREAM_ENTRIES)
    return e

def tv_season(tid, n):
    """One season's episodes, cached 24h. A provider lists a season's whole
    episode roster well before it airs, air date and all, so unaired
    episodes are dropped rather than offered as playable."""
    key = "%s@%s:%s" % (gateway.cache_tag(contract.ROLE_METADATA), tid, n)
    with _lock:
        e = _tvseason.get(key)
        if e and time.time() - e["at"] < 24 * 3600:
            return e
    d = gateway.episodes(tid, n)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    episodes = []
    for ep in d.get("episodes") or []:
        air = ep.get("air")
        if not air or air > today:
            continue
        tr = own_rating(ep)   # each episode row's ★, as before providers
        episodes.append({"season": ep.get("season"), "episode": ep.get("episode"),
                          "name": ep.get("name"), "overview": ep.get("overview"),
                          "runtime": ep.get("runtime"), "air": air,
                          "still": ep.get("still"), "vote": tr[0] if tr else None,
                          "ratings": ep.get("ratings") or {}})
    e = {"at": time.time(), "episodes": episodes}
    with _lock:
        _tvseason[key] = e
        _evict(_tvseason, 24 * 3600, MAX_STREAM_ENTRIES)
    return e

def tv_job_parts(job):
    """(title_id, season, episode) from a "tv:{id}:{s}:{e}" job id.

    rsplit from the right, not split from the left: a title id is now
    provider-qualified ("cinemeta:tt0903747") and carries its own colon, so
    unpacking four fields off a left split silently mis-parsed every episode
    job the moment ids stopped being bare numbers.
    """
    rest = job[3:] if job.startswith("tv:") else job
    tid, s, e = rest.rsplit(":", 2)
    return tid, int(s), int(e)


def next_episode(tid, s, e):
    """(season, episode) that follows (s, e) for autoplay, or None at the
    end of the show. Prefers the next aired episode in the same season;
    falls back to episode 1 of the next season if that season exists and
    has at least one aired episode. Provider errors are swallowed -- a
    failed lookup just means autoplay does not fire, not a broken heartbeat."""
    try:
        after = [ep["episode"] for ep in tv_season(tid, s)["episodes"]
                 if ep["episode"] > e]
        if after:
            return s, min(after)
        if any(sn["n"] == s + 1 for sn in tv_detail(tid)["seasons"]):
            nxt = [ep["episode"] for ep in tv_season(tid, s + 1)["episodes"]]
            if nxt:
                return s + 1, min(nxt)
        return None
    except Exception:
        return None

_pool = {}     # genre key -> {"cands":[...], "ready":[...], "cursor":int, "at":ts}
# One lock per pool key, not the global _lock -- get_page's cursor/buf/served
# bookkeeping (and the network calls resolve_chunk makes while filling them)
# must be serialised PER VIEW so two requests for the same sort+genre can't
# duplicate or drop films, but different views still run concurrently. Lock
# objects are never evicted even though _pool entries are -- swapping the lock
# out from under a thread that is still inside it would reopen the same race.
_pool_locks = {}

def _pool_lock(key):
    with _lock:
        lk = _pool_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _pool_locks[key] = lk
        return lk

# What a cold view is doing right now, per pool key, so a client can show a
# closing ring instead of a blank grid: a biased film pool is ~60 browse
# pages and takes minutes, and to a guest a blank wall looks like the
# service is broken. Read under _lock only -- never the pool lock, which the
# build holds.
_progress = {}

def _note(key, **kw):
    if not key:
        return
    with _lock:
        p = _progress.setdefault(key, {"started": time.time()})
        p.update(kw)
        p["at"] = time.time()

def view_progress(key):
    """{"stage","fraction","label","elapsed"} for the progress endpoint."""
    with _lock:
        p = dict(_progress.get(key) or {})
        warm = key in _pool and _pool[key].get("served")
    if not p:
        return {"stage": "ready" if warm else "starting", "fraction": 1.0 if warm else 0.0,
                "label": "" if warm else "Gathering the hottest streams", "elapsed": 0}
    stage = p.get("stage", "starting")
    done, total = p.get("done", 0), max(1, p.get("total", 1))
    if stage == "gathering":
        frac = 0.8 * min(1.0, done / total)
        label = "Gathering the hottest streams"
    elif stage == "checking":
        frac = 0.8 + 0.2 * min(1.0, done / max(1, RESOLVE_FIRST + RESOLVE_CHUNK))
        label = "Checking what's actually streamable"
    elif stage == "ready":
        frac, label = 1.0, ""
    else:
        frac, label = 0.0, "Gathering the hottest streams"
    return {"stage": stage, "fraction": round(frac, 3), "label": label,
            "elapsed": round(time.time() - p.get("started", time.time()), 1)}

def _key(ids, sort="top", ex=None, kind="movie", bias=False):
    ex = ex or []
    base = "%s|%s|%s" % (sort or "top", ",".join(map(str, ids)) or "all",
                         ",".join(map(str, ex)) or "-")
    # tv gets its own namespace so the two kinds never collide
    tagged = ("tv:" if kind == "tv" else "") + ("bias:" if bias else "") + base
    # Prefixed with the catalogue provider's cache_tag: a provider swap or a
    # config edit must never keep serving a pool gathered under the old one.
    return "%s|%s" % (gateway.cache_tag(contract.ROLE_CATALOGUE), tagged)

def this_year():
    return time.localtime().tm_year

def recency_bonus(year):
    try:
        age = this_year() - int(year)
    except (TypeError, ValueError):
        return 0.0
    return RECENCY_W * max(0.0, 1.0 - age / RECENCY_SPAN)

def home_bonus(c):
    """The bias's lift for a home-country title (HOME_W). Only a biased pool
    tags candidates "home" (by the tier that fetched them -- a catalogue's
    browse results carry no origin_countries worth reading per-entry), so
    it is 0 everywhere else."""
    return HOME_W if c.get("home") else 0.0

def build_pool(ids, sort="top", ex=None, kind="movie", bias=False, key=None, errs=None):
    """Candidate list ordered by rating. A catalogue provider rarely sorts by
    IMDb rating and often does not expose an IMDb id on its list endpoints,
    so the pool is ordered by whatever rating it reports and each film's REAL
    IMDb rating is attached once resolved, then used to order within each
    page.

    kind="tv" browses the series catalogue instead: series need a lower
    vote-count floor than film (TV_MIN_VOTES) since a provider's TV vote
    counts typically run much lower, and "recent" filters on first-air date
    rather than release date.

    bias=True builds the pool in tiers (HOME_COUNTRIES at the normal floor
    and everything else at the big floor, both in BIAS_LANG, then any
    language at WORLD_MIN_VOTES -- see BIAS) and merges them by rating plus
    home bonus: the pool resolves in order, so tiers appended one after the
    other would make the first pages all home tier and the last pages all
    world tier.

    Every tier here is expressed as browse filters, and not every provider
    can apply all of them -- see the supports_filters() check below, which
    drops a tier rather than let it silently issue the same query as another.
    """
    cands = []
    # "balanced" needs both ends of the catalogue in the pool, otherwise the
    # recency bonus has no recent films to lift. Pull half from each.
    sources = [("top", POOL_MAX)] if sort == "top" else \
              [("recent", POOL_MAX)] if sort == "recent" else \
              [("top", POOL_MAX // 2), ("recent", POOL_MAX // 2)]
    is_tv = kind == "tv"
    gw_kind = _gw_kind(kind)
    top_vote_floor = TV_MIN_VOTES if is_tv else 500
    recent_vote_floor = TV_MIN_VOTES if is_tv else 200
    # (origin countries or None, top floor, recent floor, home?, BIAS_LANG only?)
    tiers = [(None, top_vote_floor, recent_vote_floor, False, False)]
    supported = gateway.supports_filters()
    if bias and "min_votes" not in supported:
        # Every bias tier below differs from the single base tier (and from
        # each other) ONLY by its vote-count floor. A provider that cannot
        # filter by votes would issue the identical, unfiltered query for
        # all of them, and the per-block dedup further down would then
        # silently collapse them into one -- disabling the bias with no
        # error anywhere. Degrade to the base tier instead; home_bonus below
        # still applies per-entry, so a home title is still favoured on
        # merge even without a dedicated tier for it.
        bias = False
    if bias:
        big = TV_BIG_MIN_VOTES if is_tv else MOVIE_BIG_MIN_VOTES
        # a recent big-tier title with half the floor is already a hit
        tiers = [(None, big, max(recent_vote_floor, big // 2), False, True)]
        if HOME_COUNTRIES and "origin_countries" in supported:
            tiers.insert(0, (HOME_COUNTRIES, top_vote_floor, recent_vote_floor, True, True))
        # else: the home tier's only distinguishing filter is
        # origin_countries -- without it the tier would issue the same query
        # as the one above and get deduped into it, so it is left out rather
        # than spending a whole tier's page budget on a no-op.
        if WORLD_MIN_VOTES:
            # no halved floor here: it would let a two-year-old anime in
            tiers.append((None, WORLD_MIN_VOTES, WORLD_MIN_VOTES, False, False))
    # progress, in browse pages: a tier that runs dry early jumps ahead by the
    # pages it did not need, so the ring only ever closes, never reopens
    share = {cap: min(30, -(-cap // 20)) for _, cap in sources}
    pages_total = sum(share[cap] for _, cap in sources) * len(tiers)
    pages_base = 0
    _note(key, stage="gathering", done=0, total=pages_total)
    for src, cap in sources:
        block = []
        in_block = set()   # a title clearing two tiers keeps its first (home) copy
        for countries, top_floor, recent_floor, home, lang in tiers:
            page = 1
            taken = 0
            while taken < cap and page <= 30:
                _note(key, done=pages_base + page - 1)
                floor = recent_floor if src == "recent" else top_floor
                filters = {}
                if "min_votes" in supported:
                    filters["min_votes"] = floor
                if countries:
                    filters["origin_countries"] = list(countries)
                if lang and BIAS_LANG:
                    filters["original_language"] = BIAS_LANG
                if src == "recent":
                    filters["released_after"] = "%d-01-01" % (this_year() - RECENT_YEARS)
                if ids:
                    filters["genre_ids"] = [str(g) for g in ids]
                if ex:
                    filters["exclude_genre_ids"] = [str(g) for g in ex]
                try:
                    d = gateway.browse(gw_kind, page, PAGE, sort, filters)
                # Not `as ex`: that is the excluded-genres argument, and Python
                # deletes an except target when the block ends -- the next
                # tier's `if ex:` then raised UnboundLocalError and 500ed the
                # very wall this handler exists to keep alive.
                except contract.ProviderError as perr:
                    # A provider failure must never 500 a browse, so this page
                    # is abandoned like an empty one -- but it is REMEMBERED.
                    # Swallowing it outright left /api/movies answering 200 with
                    # an empty list, which looks exactly like "your filters
                    # matched nothing" and sent people hunting through their
                    # genre settings while the real problem was an add-on that
                    # had stopped answering.
                    if errs is not None:
                        errs.append(perr)
                    break
                res = d.get("entries") or []
                if not res:
                    break
                before = taken
                for r in res:
                    if r["id"] in in_block:
                        continue
                    in_block.add(r["id"])
                    c = _api_entry(r, kind)
                    c["home"] = home
                    block.append(c)
                    taken += 1
                # A real add-on that ignores page/skip returns the same page
                # forever; without this, a page that adds nothing new burns
                # up to 30 pointless fetches per tier instead of stopping.
                if taken == before:
                    break
                page += 1
            pages_base += share[cap]
        if len(tiers) > 1:
            # merge the tiers by merit, then hold this source to its share of
            # the pool so a "balanced" pool still gets its recent half.
            # Merit is the catalogue's own rating (vote, from _api_entry()):
            # a list entry has no IMDb rating to go by, and sorting on one
            # scored every title 0 + home bonus, so the home tier filled the
            # whole pool and the rest of the world was cut off.
            block.sort(key=lambda c: (c.get("vote") or 0) + home_bonus(c),
                       reverse=True)
            block = block[:cap]
        cands.extend(block)
    _note(key, done=pages_total)   # tiers usually run dry early: close the gathering arc
    # de-dup, keep first occurrence
    seen, out = set(), []
    for c in cands:
        if c["id"] in seen:
            continue
        seen.add(c["id"]); out.append(c)
    return out[:POOL_MAX]

def view_key(genres=None, sort="top", exclude=None, kind="movie", bias=None):
    """The normalised (ids, ex, bias, pool key) for a browse request -- one
    place, so /api/movies and /api/movies/progress can never disagree."""
    sort = sort if sort in SORTS else "top"
    ids = sorted(set(int(g) for g in (genres or []) if str(g).strip()))
    ex  = sorted(set(int(g) for g in (exclude or []) if str(g).strip()) - set(ids))
    # bias: None means "the server's default" (BIAS)
    bias = BIAS if bias is None else bool(bias)
    return ids, ex, bias, _key(ids, sort, ex, kind, bias)

def _api_entry(e, kind):
    """A catalogue entry in the clients' vocabulary, which is the one they
    were written against before providers were split out:

      * kind "tv", not the contract's "series". Both the TV app and the web
        page open the seasons-and-episodes page on kind == "tv"; given
        "series" they opened every series as a film.
      * vote/votes flat on the entry: the catalogue's own rating, which the
        tiles show and rank() falls back to when there is no IMDb rating.
        The contract files it by name under ratings, where no client looks.
      * numeric genre ids as numbers: the TV's model is List<Int>, and the
        contract turns every id into a string.
    """
    e = dict(e)
    e["kind"] = kind
    e["genre_ids"] = [int(g) if str(g).isdigit() else g for g in (e.get("genre_ids") or [])]
    if e.get("vote") is None:
        tr = own_rating(e)
        if tr:
            e["vote"], e["votes"] = tr
    return e

def _tile(m, r):
    """Entry `m` plus its resolved stream entry `r`, as a wall/search tile.

    A list entry has no IMDb id, so its IMDb rating comes from the details
    get_stream()/get_stream_tv() fetched -- which is where the pre-provider
    server took both from, too. Merged into m's ratings before anything reads
    them, so the rating floor and rank() see the same numbers the tile shows.
    """
    m = dict(m)
    m["ratings"] = dict(m.get("ratings") or {}, **(r.get("ratings") or {}))
    ir = rating_of(m, "imdb")
    m["imdb"] = {"rating": ir[0], "votes": ir[1],
                 "id": (m.get("external_ids") or {}).get("imdb") or r.get("imdb_id")} if ir else None
    m["stream"] = {"pick": r["pick"], "count": r.get("count"), "url": stream_url(r["pick"])}
    return m

def get_page(genres=None, offset=0, limit=None, sort="top", exclude=None, kind="movie", bias=None):
    """
    Return `limit` playable films (or, kind="tv", series) starting at
    `offset`, ordered by real IMDb rating. Resolving happens lazily as you
    scroll.

    Ordering needs care: candidates arrive in the catalogue's own order, which
    only approximates IMDb. Sorting each batch in isolation made the grid
    sawtooth (an 8.9 appearing after a 6.7). Instead we keep a lookahead buffer
    of resolved-but-unserved films, sort THAT by IMDb rating, and serve the top
    slice. Once served, a film's position is frozen, so scrolling never
    reshuffles what you have already seen.
    """
    limit = limit or PAGE
    sort = sort if sort in SORTS else "top"
    ids, ex, bias, key = view_key(genres, sort, exclude, kind, bias)
    # Everything below reads and mutates this view's cursor/buf/served, and
    # resolve_chunk() makes the network calls that fill them. A per-key lock
    # serialises that against other requests for the SAME view (so pages can
    # never duplicate or drop films) while a different sort/genre combination
    # still runs fully concurrently.
    with _pool_lock(key):
        with _lock:
            e = _pool.get(key)
        stale = (not e) or (time.time() - e["at"] > TTL_LIST)
        if stale:
            perr = []
            cands = build_pool(ids, sort, ex, kind, bias, key, errs=perr)
            # Hold the reference rather than re-indexing _pool below: _evict()
            # caps by count as well as age, so a concurrent rebuild for another
            # view could delete this key between the two lock acquisitions and
            # turn an ordinary page request into a 500.
            st = {"cands": cands, "served": [], "buf": [], "cursor": 0,
                  "at": time.time(),
                  # Only worth reporting when it actually cost the reader
                  # something: a tier that failed after the pool already filled
                  # is not what the grid being empty is about.
                  "err": (contract.redact(perr[0].message, gateway.secret_values())
                          if perr and not cands else None)}
            with _lock:
                _pool[key] = st
                _evict(_pool, TTL_LIST, MAX_POOL_ENTRIES)
        else:
            st = e

        def resolve_chunk(size=RESOLVE_CHUNK):
            chunk = st["cands"][st["cursor"]:st["cursor"] + size]
            if not st["served"]:
                _note(key, stage="checking", done=st["cursor"], total=len(st["cands"]))
            st["cursor"] += len(chunk)
            if not chunk:
                return
            # entry=x: get_stream() builds the identity straight from the
            # catalogue's own entry when it already carries the IMDb id and
            # runtime, and fetches details when it does not.
            with ThreadPoolExecutor(max_workers=3) as ex:
                if kind == "tv":
                    results = list(ex.map(lambda x: get_stream_tv(x["id"], 1, 1), chunk))
                else:
                    results = list(ex.map(lambda x: get_stream(x["id"], entry=x), chunk))
            for m, r in zip(chunk, results):
                if not r.get("pick"):
                    continue
                m = _tile(m, r)
                if sort in ("balanced", "recent") and not passes_rating_floor(m):
                    # a recency bonus must never be a route in for badly-rated
                    # films -- but passes_rating_floor() already lets a title
                    # with NO imdb rating through, so a provider that never
                    # reports ratings at all cannot empty the whole pool the
                    # way a hard floor used to.
                    continue
                m["boost"] = round((recency_bonus(m.get("year")) if sort == "balanced" else 0)
                                   + (home_bonus(m) if bias else 0), 2)
                st["buf"].append(m)

        def rank(x):
            im = x.get("imdb")
            base = im["rating"] if im else (x.get("vote") or 0)
            if sort == "balanced":
                base += recency_bonus(x.get("year"))
            if bias:
                base += home_bonus(x)
            return base

        # serve from `served` if this page was already decided
        while len(st["served"]) < offset + limit and st["cursor"] < len(st["cands"]):
            # keep a lookahead of 2 pages so the sort has something to choose from
            while len(st["buf"]) < limit * 2 and st["cursor"] < len(st["cands"]):
                # A cold view resolved RESOLVE_CHUNK candidates before serving
                # anything, so the very first row cost ~19s. Resolve a smaller
                # first batch: the pool is already ordered by the catalogue's
                # own rating, so the top few are the same films either way,
                # and every later pass is full size -- which keeps the IMDb
                # re-sort choosing from a wide field, and happens ahead of the
                # reader rather than in front of them. If the small batch
                # yields too few playable films the loop simply goes round
                # again at full size.
                resolve_chunk(RESOLVE_FIRST if not st["served"] and not st["buf"]
                              else RESOLVE_CHUNK)
            if not st["buf"]:
                break
            st["buf"].sort(key=rank, reverse=True)
            take = st["buf"][:limit]
            st["buf"] = st["buf"][limit:]
            st["served"].extend(take)
        # pool exhausted: flush whatever is left, still rating-ordered
        if st["cursor"] >= len(st["cands"]) and st["buf"] and len(st["served"]) < offset + limit:
            st["buf"].sort(key=rank, reverse=True)
            st["served"].extend(st["buf"])
            st["buf"] = []

        _note(key, stage="ready")
        # st["buf"] holds resolved films from the lookahead that are still owed to the reader
        more = st["cursor"] < len(st["cands"]) or len(st["served"]) > offset + limit or bool(st["buf"])
        return (st["served"][offset:offset + limit], more, st["cursor"],
                len(st["cands"]), st.get("err"))

# "no usable stream" is a real answer about a film and keeps the full TTL. A
# transport failure (429, a Cloudflare 403, a timeout) is not an answer at
# all, and caching it for TTL_STREAM drops the film out of the grid for three
# hours -- resolve_chunk() silently skips anything without a pick. That is
# what TTL_FAIL is for. get_stream()/get_stream_tv() decide which bucket a
# contract.ProviderError falls into by its .code: contract.CACHEABLE_ERRORS
# ("not found"/"unsupported") is a real answer and gets the soft string
# below; anything else is a transport failure and gets a harder one instead.
# The bucket is now carried on the entry as "soft" rather than inferred by
# string-matching the message. Those were two different questions wearing one
# answer: how long to cache, and what to tell the user. Matching on the text
# meant that reporting a provider's real reason -- which is the only useful
# thing to show -- silently reclassified a real answer as a transport failure.
SOFT_ERRS = ("no usable stream",)

def entry_ttl(e):
    e = e or {}
    err = e.get("err")
    if not err:
        return TTL_STREAM
    if "soft" in e:
        return TTL_STREAM if e["soft"] else TTL_FAIL
    return TTL_FAIL if err not in SOFT_ERRS else TTL_STREAM

def stream_miss(count, rejected):
    """Why nothing was playable, when the provider did answer.

    "no usable stream" on its own is true and useless. If forty sources came
    back and every one was an unsupported transport, or every one scored below
    the quality floor, that is what the admin needs to read.
    """
    if not count:
        return "no usable stream"
    if rejected:
        why = ", ".join("%s x%d" % (r, n) for r, n in
                        sorted(rejected.items(), key=lambda kv: -kv[1])[:3])
        return "no usable stream (%d returned; %s)" % (count, why)
    return "no usable stream (%d returned, none met the quality rules)" % count


def search_movies(q, limit=24, on_found=None, on_movie=None, kind="movie"):
    """Find films (or, kind="tv", series) by title, franchise/theme, or the
    people in them -- whatever the catalogue provider's own search covers.
    A single gateway.search() call replaces what used to be three separate
    indexes (title, keyword->discover, person->discover) combined by hand;
    the provider's relevance order is trusted as-is and not re-ranked.

    Results with no playable stream are dropped, so candidates are resolved in
    chunks until `limit` playable films are found rather than resolving a fixed
    number and returning however few survive.
    """
    is_tv = kind == "tv"
    try:
        cands = (gateway.search(_gw_kind(kind), q, SEARCH_POOL).get("entries") or [])[:SEARCH_POOL]
    except contract.ProviderError:
        cands = []

    if on_found:
        on_found(len(cands))
    out, i = [], 0
    while i < len(cands) and len(out) < limit:
        chunk = cands[i:i + 8]
        i += len(chunk)
        # Iterate the map generator rather than list()-ing it: it yields in the
        # order submitted, as each completes, so a result reaches the page the
        # moment it resolves instead of waiting for its whole chunk. Order is
        # relevance order, so it must not be shuffled by completion time.
        fn = (lambda x: get_stream_tv(x["id"], 1, 1)) if is_tv else (lambda x: get_stream(x["id"], entry=x))
        with ThreadPoolExecutor(max_workers=3) as ex:
            for m, r in zip(chunk, ex.map(fn, chunk)):
                if not r.get("pick"):
                    continue                  # unplayable: hidden, not shown greyed
                m = _tile(_api_entry(m, kind), r)
                m["boost"] = 0
                out.append(m)
                if on_movie:
                    on_movie(m)          # reaches the page the moment it resolves
                if len(out) >= limit:
                    break
    return out, len(cands), i

def get_stream(mid, force=False, entry=None):
    """`entry`, when the caller already holds this title's catalogue entry (a
    browse/search result), is used to build the identity directly instead of
    a details() round trip -- this used to fetch the film's details fresh on
    EVERY stream lookup, even for a film the pool had just resolved from its
    own listing. When the caller has nothing cheaper, a details() lookup
    still happens below; there is no cache for it here because there never
    was one."""
    key = "%s@%s" % (gateway.cache_tag(contract.ROLE_STREAMS), mid)
    with _lock:
        e = _streams.get(key)
        if e and not force and time.time() - e["at"] < entry_ttl(e):
            return e
    try:
        det = entry
        # A list entry stands in for the details only when it carries what the
        # lookup needs: the IMDb id (the key a stream index is most likely to
        # accept, and for some the only one) and the runtime (the bitrate
        # check's divisor). A catalogue's browse and search results commonly
        # carry neither, and trusting them sent every film to the streams
        # provider id-less -- an empty wall, a Play that failed, and a
        # calibration with nothing to measure. Before providers were split out,
        # every film's details were fetched here.
        if det is None or not (det.get("external_ids") or {}).get("imdb") \
                or not det.get("runtime"):
            det = gateway.details(mid, contract.KIND_MOVIE)
        ext = det.get("external_ids") or {}
        identity = {"id": mid, "local_id": det.get("local_id"), "kind": contract.KIND_MOVIE,
                    "title": det.get("title"), "year": det.get("year"),
                    "runtime": det.get("runtime"), "external_ids": ext}
        # No short-circuit on a missing imdb id: the streams provider may
        # accept the catalogue's own id just fine, and a title that happens
        # to have no IMDb id must still get a real attempt, not an
        # automatic "no imdb_id".
        ranked, n, rejected = best_stream(identity, det.get("runtime"))
        e = {"at": time.time(), "pick": (ranked[0] if ranked else None),
             # picks is what the TV plays, ranked by score() and cut to ATTEMPTS.
             # picks_all keeps the relaxed tail too, because a browser may need an
             # H.264 candidate that the TV's HEVC-first ranking pushed past the cut.
             "picks": ranked[:ATTEMPTS], "picks_all": ranked[:25], "count": n,
             "rejected": rejected,
             "imdb_id": ext.get("imdb"), "runtime": det.get("runtime"), "title": det.get("title"),
             # A list entry has no IMDb id, so no IMDb rating either: the
             # wall takes it from the details fetched here (_tile()).
             "ratings": det.get("ratings") or {},
             "soft": True,
             "err": None if ranked else stream_miss(n, rejected)}
    except contract.ProviderError as ex:
        # Cacheable or not, say what actually happened. Hiding a provider's
        # reason behind "no usable stream" is how a whole catalogue came to
        # look unplayable with nothing anywhere explaining it.
        e = {"at": time.time(), "pick": None, "count": 0, "rejected": {}, "imdb_id": None,
             "soft": ex.code in contract.CACHEABLE_ERRORS,
             "err": "%s: %s" % (ex.code, ex.message)}
    except Exception as ex:
        e = {"at": time.time(), "pick": None, "count": 0, "rejected": {}, "imdb_id": None,
             "err": f"{type(ex).__name__}: {ex}"}
    with _lock:
        _streams[key] = e
        _evict(_streams, TTL_STREAM, MAX_STREAM_ENTRIES)
    return e

def get_stream_tv(tid, s, e, force=False, entry=None):
    """Mirrors get_stream() for one episode. Shares the SAME _streams dict as
    films -- same TTLs, entry_ttl(), _evict(), MAX_STREAM_ENTRIES -- just keyed
    by season+episode so a show's other episodes don't collide with each
    other or with a film of the same id. `entry`, like get_stream()'s, lets a
    caller that already holds the series' catalogue entry skip tv_detail();
    when it does not, tv_detail() is already a 24h cache, not a fresh call."""
    key = "%s@%s" % (gateway.cache_tag(contract.ROLE_STREAMS), "tv:%s:%s:%s" % (tid, s, e))
    with _lock:
        ent = _streams.get(key)
        if ent and not force and time.time() - ent["at"] < entry_ttl(ent):
            return ent
    try:
        det = entry if entry is not None else tv_detail(tid)
        ext = det.get("external_ids") or {}
        ep = next((x for x in tv_season(tid, s)["episodes"] if x.get("episode") == e), None)
        runtime = (ep.get("runtime") if ep else None) or det.get("runtime") or 45
        ep_name = (ep.get("name") if ep else None) or ""
        title = f"{det.get('title')} · S{s:02d}E{e:02d}" + (f" · {ep_name}" if ep_name else "")
        identity = {"id": tid, "local_id": det.get("local_id"), "kind": contract.KIND_SERIES,
                    "title": det.get("title"), "year": det.get("year"), "runtime": runtime,
                    "external_ids": ext}
        # Same rule as get_stream(): no imdb id is not a reason to skip the
        # lookup, only a reason it might fail.
        ranked, n, rejected = best_stream(identity, runtime, kind="tv", season=s, episode=e)
        ent = {"at": time.time(), "pick": (ranked[0] if ranked else None),
               "picks": ranked[:ATTEMPTS], "picks_all": ranked[:25], "count": n,
               "rejected": rejected,
               "imdb_id": ext.get("imdb"), "runtime": runtime, "title": title,
               "ratings": det.get("ratings") or {},
               "soft": True,
               "err": None if ranked else stream_miss(n, rejected),
               "kind": "tv", "season": s, "episode": e}
    except contract.ProviderError as ex:
        ent = {"at": time.time(), "pick": None, "count": 0, "rejected": {}, "imdb_id": None,
               "soft": ex.code in contract.CACHEABLE_ERRORS,
               "err": "%s: %s" % (ex.code, ex.message),
               "kind": "tv", "season": s, "episode": e}
    except Exception as ex:
        ent = {"at": time.time(), "pick": None, "count": 0, "rejected": {}, "imdb_id": None,
               "err": f"{type(ex).__name__}: {ex}", "kind": "tv", "season": s, "episode": e}
    with _lock:
        _streams[key] = ent
        _evict(_streams, TTL_STREAM, MAX_STREAM_ENTRIES)
    return ent

def net_load():
    try:
        d = json.load(open(NET_FILE))
    except Exception:
        return {}
    # Early samples were bare floats with no peer count. A rate cannot be
    # interpreted without one, so they are dropped rather than guessed at.
    d["samples"] = [x for x in (d.get("samples") or []) if isinstance(x, dict)]
    return d

def net_save(d):
    try:
        tmp = NET_FILE + ".tmp"
        json.dump(d, open(tmp, "w"), indent=2)
        os.replace(tmp, NET_FILE)
    except Exception:
        pass

_net = net_load()

def record_rate(bps, seeders=None):
    """Remember what a real swarm delivered, WITH the peer count that produced it.

    Throughput is governed by how many peers serve that particular file, not by
    link speed: a popular release saturates the connection while a thinly-seeded
    one crawls on the same link. A rate recorded without its seeder count cannot
    be interpreted, so it is not worth recording.
    """
    mbps = (bps or 0) * 8 / 1048576.0
    if mbps <= 0:
        return
    with _lock:
        s = _net.setdefault("samples", [])
        s.append({"mbps": round(mbps, 2), "seeders": int(seeders or 0),
                  "at": int(time.time())})
        del s[:-NET_SAMPLES]
        snap = dict(_net)        # dump a snapshot: serialising the live dict
    net_save(snap)               # let another thread mutate it mid-write

def ordinary_samples():
    """Rates achieved by ORDINARY swarms — those below WELL_SEEDED.

    Well-seeded films are excluded deliberately. score() already grants
    SEED_BONUS (1.5x) to them, so letting them lift the base budget counts the
    same advantage twice, and the budget would then start admitting large
    thinly-seeded releases that cannot sustain themselves. SUSTAIN_MBPS is the
    base case: what a film with unremarkable peer support manages.
    """
    with _lock:
        raw = list(_net.get("samples") or [])
    return sorted(x["mbps"] for x in raw
                  if isinstance(x, dict) and x.get("seeders", 0) < WELL_SEEDED)

def sustain_from_samples():
    """A low percentile of ordinary-swarm throughput: a rate most films can meet,
    not the best one ever managed."""
    s = ordinary_samples()
    if len(s) < NET_MIN_OBS:
        return None
    return s[min(len(s) - 1, int(len(s) * NET_PCT))]

def browser_playing():
    """Whether a browser session is currently watching something.

    A pause still counts: the viewer is sitting in front of it, and treating a
    paused film as idle is what would let cache_watch() empty the cache under
    them. Only a session that has stopped sending heartbeats is gone.
    """
    with _lock:
        return (_bx["token"] is not None and _bx["state"] != "ended"
                and time.time() - _bx["at"] < BX_IDLE)

def playing_now():
    """Cheap guard: measuring saturates the link, so it must yield to a film.

    The TV is not the only player any more, so a browser watching counts too.
    """
    try:
        return tv_playback_state() in (2, 3) or browser_playing()
    except Exception:
        return False

def link_probe():
    """Sustained single-stream HTTP throughput, in Mbps. Best of the endpoints,
    so one slow CDN cannot understate the link."""
    best = 0.0
    for url in NET_URLS:
        if playing_now():
            print("netcheck: abandoned, playback started", flush=True)
            return best
        host = url.split("/")[2]
        for attempt in range(1, NET_TRIES + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                t0 = time.time(); got = 0
                with urllib.request.urlopen(req, timeout=20) as r:
                    while time.time() - t0 < NET_SECS:
                        c = r.read1(262144)
                        if not c:
                            break
                        got += len(c)
                el = max(time.time() - t0, 0.001)
                mbps = got / el * 8 / 1048576.0
                print("netcheck: %s -> %.1f Mbps" % (host, mbps), flush=True)
                best = max(best, mbps)
                break
            except Exception as e:
                # A dropped transfer says nothing about the link's speed, so
                # retry rather than recording a zero.
                print("netcheck: %s attempt %d failed: %s" % (host, attempt, e), flush=True)
    return best

def stremio_set(**vals):
    try:
        req = urllib.request.Request(STREMIO_IN + "/settings",
                                     data=json.dumps(vals).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print("netcheck: could not set %s: %s" % (list(vals), e), flush=True)
        return False

def net_apply(quiet=False):
    """Push the current profile into the running config."""
    global SUSTAIN_MBPS
    mbps = _net.get("mbps")
    conns = _net.get("conns")
    if mbps:
        SUSTAIN_MBPS = float(mbps)
    if conns:
        stremio_set(btMaxConnections=int(conns))
    if not quiet:
        print("netcheck: applied sustain=%s Mbps conns=%s (%s)"
              % (mbps, conns, _net.get("source")), flush=True)

_net_busy = {"on": False, "since": 0.0}
_cal = {"on": False, "done": 0, "want": 0, "msg": ""}
# Probing saturates the link by design, so any uncached catalogue call made
# while it runs times out -- which showed up as a completely blank film
# list. It waits for the app to be genuinely idle instead of merely waiting
# its turn at startup.
IDLE_SECS = float(os.environ.get("NET_IDLE_SECS", 45))
IDLE_WAIT = float(os.environ.get("NET_IDLE_WAIT", 1800))
_last_req = {"at": 0.0}
# Polled endpoints are not "use": the page asks for health, now-playing and
# calibration progress on timers, and counting those as activity would mean a
# calibration aborted itself the moment anyone watched it happen.
# The TV app's heartbeat belongs here too, and more strongly than the rest: it
# never stops while the app is running, so counting it as use would mean the link
# was never idle and the calibration never ran at all.
# The browser heartbeat is bookkeeping, not someone browsing -- the same
# reasoning as the TV app's heartbeat above -- so it must not keep
# app_in_use() permanently true and starve the link calibration. (The route
# itself does not exist yet; adding the path now keeps the two in one place.)
POLL_PATHS = ("/api/health", "/api/nowplaying", "/api/netcheck",
              "/api/player/heartbeat", "/api/bx/beat")

# Static files served under /static/<name> -> (file under static/, content type).
STATIC = {
    "/static/alfa-slab-one.ttf": ("alfa-slab-one.ttf", "font/ttf"),
    # hls.js, vendored and pinned. Only fetched by browsers without native HLS,
    # so Safari and iOS never pay for it. sha256
    # a12e7ee1cd64a69dcdb314157e45dafcba705bfb0b1440b7935cb265d374423e --
    # the only provenance trail a minified bundle gets in a repo with no
    # package manager; it is dist/hls.min.js as published to npm, and this
    # digest was checked against the one jsdelivr publishes for that exact
    # file before the bytes were committed. A new version gets a NEW PATH,
    # never a new body here.
    "/static/hls-1.7.3.min.js": ("hls-1.7.3.min.js", "text/javascript"),
}

def note_request(path):
    if path not in POLL_PATHS:
        _last_req["at"] = time.time()

def idle_for():
    return time.time() - _last_req["at"]

def app_in_use():
    """Someone is actually browsing. Measuring saturates the link, so stop."""
    return idle_for() < IDLE_SECS

def wait_until_idle(limit=IDLE_WAIT):
    """Hold off until nobody has touched the app for IDLE_SECS."""
    t0 = time.time()
    while time.time() - t0 < limit:
        if playing_now():
            return False
        if idle_for() > IDLE_SECS:
            return True
        time.sleep(5)
    return False

def cached_hashes():
    """Torrents Stremio already holds. Sampling one would measure the disk."""
    try:
        r = subprocess.run(["docker", "exec", FFMPEG_CTR, "sh", "-c",
                            "ls /stremio-server/stremio-cache 2>/dev/null"],
                           capture_output=True, text=True, timeout=30)
        return {d for d in (r.stdout or "").split() if contract.RE_HASH40.match(d)}
    except Exception:
        return set()

def sample_swarm(pick, secs=None):
    """Download from one real swarm for a few seconds and return bytes/sec.

    The same measurement probe_and_buffer() makes during playback, done
    deliberately rather than waiting for it to happen.
    """
    url = stream_url(pick)
    secs = secs or CAL_SECS
    try:
        # From the start, which is what a player reads and what peers prioritise.
        # It is uncached because cached torrents are skipped before we get here --
        # reading cached data measured the NVMe, not the link, and reported
        # 269 Mbps on an 87 Mbps connection.
        req = urllib.request.Request(url,
                headers={"Range": "bytes=0-%d" % (CAL_MB * 1048576 - 1),
                         "User-Agent": UA})
        t0 = time.time(); got = 0
        with urllib.request.urlopen(req, timeout=DEAD_SECS + 10) as r:
            while time.time() - t0 < secs:
                c = r.read1(262144)
                if not c:
                    break
                got += len(c)
                if got == 0 and time.time() - t0 > DEAD_SECS:
                    return 0.0
        el = max(time.time() - t0, 0.001)
        return got / el
    except Exception as e:
        print("calibrate: %s failed: %s" % (pick.get("infoHash", "?")[:8], e), flush=True)
        return 0.0

def calibrate():
    """Measure ordinary swarms up front so the very first film is sized on real
    evidence instead of a guess.

    Ordinary means seeders below WELL_SEEDED: those are the swarms the base
    budget has to respect. Well-seeded films are excluded here for the same
    reason they are excluded from the running average -- score() already gives
    them SEED_BONUS, and sizing the base case on them would admit large
    thinly-seeded releases that stutter.
    """
    t0 = time.time()
    _cal.update(on=True, done=0, want=CAL_FILMS, msg="Finding films to test\u2026")
    try:
        # Deliberately NOT get_page(): that holds the browse pool's lock for the
        # whole resolve, and calibration takes minutes -- the phone's grid needs
        # the same lock and simply stopped loading until this finished. Resolve
        # a private handful instead; get_stream() only takes the shared lock in
        # short bursts and its results are cached for the grid anyway.
        cands = []
        held = cached_hashes()
        try:
            # A fixed page sorted by rating returned the identical top 20 every
            # single run, so calibration re-measured the same swarms -- which by
            # then were sitting in Stremio's cache. Random page, random order.
            d = gateway.browse(contract.KIND_MOVIE, page=random.randint(1, 25),
                                page_size=20, sort="top", filters={"min_votes": 200})
            entries = d.get("entries") or []
            random.shuffle(entries)
            entries = entries[:20]
        except contract.ProviderError as e:
            _cal["msg"] = "Could not reach the catalogue: %s" % e.message
            return
        for i, ent in enumerate(entries):
            if len(cands) >= CAL_FILMS or time.time() - t0 > CAL_MAX / 2:
                break
            if playing_now():
                _cal["msg"] = "Paused — you started watching"
                return
            _cal["msg"] = "Finding films to test (%d of %d)\u2026" % (i + 1, len(entries))
            try:
                pk = get_stream(ent["id"], entry=ent).get("pick")
            except Exception:
                continue
            if not pk or not (MIN_SEEDERS <= (pk.get("seeders") or 0) < WELL_SEEDED):
                continue
            if pk.get("infoHash") in held:
                continue          # already on disk: would measure the NVMe
            cands.append(pk)
        if not cands:
            _cal["msg"] = "No ordinary swarms available to measure"
            print("calibrate: nothing with %d..%d seeders to sample"
                  % (MIN_SEEDERS, WELL_SEEDED), flush=True)
            return
        for pk in cands:
            if _cal["done"] >= CAL_FILMS or time.time() - t0 > CAL_MAX:
                break
            if playing_now():
                # A film wins. Browsing does not: the page is held behind the
                # calibration overlay while this runs, so "in use" cannot happen
                # -- and aborting on it meant calibration never finished at all.
                _cal["msg"] = "Paused — you started watching"
                print("calibrate: abandoned, playback started", flush=True)
                break
            _cal["msg"] = ("Measuring swarm %d of %d\u2026"
                           % (_cal["done"] + 1, CAL_FILMS))
            bps = sample_swarm(pk)
            if bps > 0:
                record_rate(bps, pk.get("seeders"))
                _cal["done"] += 1
                print("calibrate: %s (%d seeders) -> %.1f Mbps"
                      % (pk.get("infoHash", "?")[:8], pk.get("seeders") or 0,
                         bps * 8 / 1048576.0), flush=True)
        _cal["msg"] = "Measured %d swarms" % _cal["done"]
    finally:
        _cal["on"] = False
        # Nothing sampled is worth keeping: these are films picked at random to
        # measure the link, not anything the owner asked for.
        try:
            cache_clear()
        except Exception:
            pass

def net_check():
    """Measure the link and retune. Returns the profile."""
    _net_busy.update(on=True, since=time.time())
    try:
        return _net_check()
    finally:
        _net_busy["on"] = False

def _net_check():
    return net_derive(link_probe())

def net_retune():
    """Recompute from samples already gathered, without re-probing. Used after
    calibration, when a fresh HTTP probe would measure a link still busy with
    the swarms just sampled."""
    return net_derive(float(_net.get("http_mbps") or 0), quiet=True)

def net_derive(http_mbps, quiet=False):
    observed = sustain_from_samples()
    if observed is not None:
        mbps, source = observed, "observed"       # real swarms, the honest signal
    elif http_mbps:
        mbps, source = http_mbps * NET_FRACTION, "estimated"
    else:
        mbps, source = SUSTAIN_MBPS, "unchanged"
    if http_mbps and source == "estimated":
        # Cap a guess at the link, but never a measurement: observed rates ARE
        # real throughput, and the probe can read low if it ran while torrents
        # were active. A measurement beats an inference about the same thing.
        mbps = min(mbps, http_mbps * 0.8)
    mbps = round(max(SUSTAIN_MIN, min(SUSTAIN_MAX, mbps)), 1)
    conns = int(max(CONNS_MIN, min(CONNS_MAX, 60 + http_mbps))) if http_mbps \
            else int(_net.get("conns") or 90)
    # Computed BEFORE the lock: ordinary_samples() takes _lock itself, so calling
    # it here acquired the same lock twice in one thread. Against the plain Lock
    # this was in place with, that self-deadlocked on the first derive and every
    # other thread then queued behind it -- a 22-thread pile-up and a dead server.
    n_ord = len(ordinary_samples())
    with _lock:
        _net.update(at=time.time(), mbps=mbps, conns=conns,
                    http_mbps=round(http_mbps, 1), source=source,
                    n_samples=len(_net.get("samples") or []),
                    n_ordinary=n_ord)
        snap = dict(_net)
    net_save(snap)
    net_apply(quiet=quiet)
    return dict(_net)

def net_full(force=False):
    """Probe the link, and calibrate against real swarms if we still lack the
    ordinary-swarm evidence the budget needs -- or if explicitly asked to.

    A forced recalibration starts from nothing. The point of asking for one is to
    describe the connection and the torrent scene AS THEY ARE, so averaging the
    new measurements with old ones would defeat it -- move the server to another
    link and the previous network would still dominate the budget. With a
    20-sample window and 4 samples a run it would have taken five consecutive
    recalibrations to flush. Everything measured afterwards, including real films
    as they are watched, then accumulates on top of the fresh baseline.
    """
    if force:
        with _lock:
            dropped = len(_net.get("samples") or [])
            _net["samples"] = []
        if dropped:
            print("netcheck: recalibrating from scratch, %d old sample(s) discarded"
                  % dropped, flush=True)
    net_check()
    if (force or len(ordinary_samples()) < NET_MIN_OBS) and not playing_now():
        calibrate()
        net_retune()
    return dict(_net)

def cache_size_apply():
    """Cap Stremio's cache, retrying while its container is still coming up.

    This runs in its own thread at boot, which on a reboot is well before the
    stremio container has bound its port: the single attempt it used to make
    died with a broken pipe and the cap was then simply never applied for the
    whole life of the service. Retry, stop at the first success, and say so once
    if it never takes.
    """
    want  = int(CACHE_GB * 1024 ** 3)
    tries = 10
    gap   = 15
    for attempt in range(1, tries + 1):
        if stremio_set(cacheSize=want):
            print("cache: capped at %g GB" % CACHE_GB, flush=True)
            return
        if attempt < tries:
            time.sleep(gap)
    print("cache: could not cap at %g GB after %d attempts (%ds), leaving "
          "Stremio's own setting alone" % (CACHE_GB, tries, tries * gap), flush=True)

def cache_clear():
    """Empty Stremio's torrent cache, skipping anything still in use.

    Deliberate: Stremio hoards whole films so a resume is instant, which meant
    18 GB across 21 titles already watched. The disk is worth more than the
    re-download."""
    try:
        raw = urllib.request.urlopen(STREMIO_IN + "/stats.json", timeout=10).read()
        active = set(json.loads(raw or "{}"))
    except Exception:
        active = set()
    try:
        r = subprocess.run(["docker", "exec", FFMPEG_CTR, "sh", "-c",
                            "ls /stremio-server/stremio-cache 2>/dev/null"],
                           capture_output=True, text=True, timeout=30)
        dirs = [d for d in (r.stdout or "").split() if contract.RE_HASH40.match(d)]
    except Exception as e:
        print("cache: could not list:", e, flush=True)
        return
    freed = 0
    for h in dirs:
        if h in active:
            if playing_now():
                continue                  # still streaming: leave it alone
            # Stremio keeps an engine open long after its last reader has
            # gone -- a stopped conversion, a calibration sample -- and an
            # open engine kept its files from ever being cleared. Nothing is
            # playing, so ask Stremio to drop it (verified: GET /{hash}/remove
            # answers {} and the hash leaves stats.json at once).
            try:
                urllib.request.urlopen(STREMIO_IN + "/" + h + "/remove", timeout=10).read()
            except Exception:
                continue
        try:
            subprocess.run(["docker", "exec", FFMPEG_CTR, "rm", "-rf",
                            "/stremio-server/stremio-cache/" + h],
                           capture_output=True, timeout=60)
            freed += 1
        except Exception:
            pass
    if freed:
        print("cache: cleared %d torrent(s), %d still active" % (freed, len(active)), flush=True)

def cache_watch():
    """Clear the cache when a film finishes, on the playing -> idle edge.

    Debounced to 3 consecutive idle samples (60s): even with the buffering
    fix in tv_playback_state(), a missed heartbeat can still read as idle for
    a beat, and firing on the very first one tore down a film still playing.
    """
    was = False
    idle_count = 0
    while True:
        time.sleep(20)
        try:
            # A browser watching counts as playing too, or the cache gets torn
            # down out from under a film someone is watching in the browser.
            now = tv_playback_state() in (2, 3) or browser_playing()
            if now:
                idle_count = 0
            elif was or idle_count:
                idle_count += 1
            if idle_count == 3:
                amid, _aj = active_job()
                if amid is not None:
                    # A job is still working -- most likely the next episode
                    # buffering off the same swarm this one just left. Clearing
                    # now would pull the rug out from under it; try again on
                    # the next sample instead.
                    idle_count = 2
                else:
                    print("cache: playback ended", flush=True)
                    # The converter does not know the film has ended -- it is fed
                    # by the swarm, not the player -- and while it runs, the swarm
                    # stays open and the cache cannot clear. Stop it first.
                    transcode_stop_all()
                    if SENDSPIN_ENABLED:
                        _hifi_release()
                    cache_clear()
                    idle_count = 0
            was = now
        except Exception:
            pass

# cid -> ts of the last snap refresh, so channel_watch() only re-fetches a
# channel's avatar/banner/subscriber count once a day, not on every sweep.
_channel_snap_checked = {}

def channel_watch():
    """Keep every followed channel's stored "latest" videos, and once a day
    its snap (avatar/banner/subscribers/description), current -- so the wall
    and the channel page are always served from disk and never wait on a
    live provider round trip. Same poll-and-sleep shape as cache_watch()."""
    time.sleep(60)
    while True:
        if not gateway.available(contract.ROLE_CHANNELS):
            time.sleep(CHANNEL_POLL_MIN * 60)
            continue
        now = time.time()
        for cid in shelf.followed_ids():
            try:
                r = gateway.channel_latest(cid)
                shelf.set_latest(cid, r["videos"])
                if now - _channel_snap_checked.get(cid, 0) > 24 * 3600:
                    shelf.set_snap(cid, gateway.channel_details(cid))
                    _channel_snap_checked[cid] = now
            except contract.ProviderError as ex:
                print("channels: %s failed for %s: %s" % (ex.op, cid, ex.message), flush=True)
            except Exception as ex:
                print("channels: unexpected error for %s: %s" % (cid, ex), flush=True)
            time.sleep(1)
        # forced: the throttle would otherwise leave a sweep unsaved until the
        # next one, CHANNEL_POLL_MIN later
        shelf.save(force=True)
        time.sleep(CHANNEL_POLL_MIN * 60)

def net_auto():
    """Apply the stored profile, and calibrate ONLY if it never has been.

    Not on a timer, and not on every start. Probing saturates the link for
    minutes, and a link measured once is still measured -- re-running it unasked
    is all cost and no information. Recalibrating is the button's job.
    netprofile.json is what makes "once" mean once; delete it to force a fresh one.
    """
    try:
        if _net.get("mbps"):
            net_apply(quiet=True)
        have = len(ordinary_samples())
        if have >= NET_MIN_OBS:
            print("netcheck: already calibrated from %d ordinary swarms, "
                  "nothing to do" % have, flush=True)
            return
        if playing_now():
            print("netcheck: skipped, something is playing", flush=True)
            return
        # Wait for the app to be idle before saturating the link. HTTP probe
        # first, on an idle link: running it after calibration measured a link
        # still busy with the swarms just sampled, 13 Mbps against 87, which
        # then capped the budget.
        if not wait_until_idle():
            print("netcheck: deferred, app in use", flush=True)
            return
        net_full()
    except Exception as e:
        print("netcheck failed:", e, flush=True)

def prefetch():
    try:
        get_genres()
        get_page(None, 0, PAGE)     # warm the first page
    except Exception as e:
        print("prefetch failed:", e, flush=True)
    # Only now assess the link. Both the HTTP probe and the swarm sampling
    # saturate the connection for minutes, and starting them first starved every
    # catalogue call behind them: genres timed out, the page's init sequence died
    # with it, and the interface came up completely empty. Measuring after the app is
    # warm means the grid is already usable and served from cache while it runs.
    net_auto()

# ---- TV control -------------------------------------------------------------
def adb(*args, timeout=25):
    return subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)

def adb_state():
    """Returns one of: off | device | unauthorized | offline | absent.

    "off" is not an adb state at all: it means the phone remote was never turned
    on, and it is returned WITHOUT running adb. /api/health calls this on every
    poll, and an install that has no TV to pair with should not be shelling out
    to a binary it may not even have.
    """
    if not ADB_ENABLED:
        return "off"
    r = adb("devices")
    for line in r.stdout.splitlines():
        if line.startswith(ADB_TV):
            parts = line.split()
            return parts[1] if len(parts) > 1 else "absent"
    return "absent"

def adb_ready(hard=False):
    """
    Bring the adb session back without destroying the pairing.

    NEVER disconnect on 'unauthorized' -- that state means the TV is waiting for
    someone to accept the on-screen prompt, and tearing the session down just
    re-issues it. Only a genuinely 'offline' transport benefits from a reset.
    """
    if not ADB_ENABLED:
        return False, "phone remote is off"
    st = adb_state()
    if st == "device":
        return True, "connected"
    if st == "unauthorized":
        return False, "TV is waiting for you to accept the USB-debugging prompt on screen"
    if st == "offline" or hard:
        adb("disconnect", ADB_TV)
        time.sleep(0.5)
    adb("connect", ADB_TV)
    for _ in range(6):                       # connect returns before the handshake
        time.sleep(1.0)
        st = adb_state()
        if st == "device":
            return True, "reconnected"
        if st == "unauthorized":
            return False, "TV is waiting for you to accept the USB-debugging prompt on screen"
    return False, f"adb state: {st}"

def wake_app():
    """Bring the TV app to the foreground over adb, then wait for it to show up
    on the heartbeat channel.

    adb has no way to hand the app anything -- no URL, no extras -- because the
    app only trusts a play command that arrives over its own heartbeat, not
    whatever an intent happened to carry. All adb does here is get it on screen;
    LEANBACK_LAUNCHER is the category Android TV launches home-screen apps with,
    which is what the app is registered as.
    """
    if not ADB_ENABLED:
        return False, "phone remote is off"
    ok, msg = adb_ready()
    if not ok:
        return False, f"TV not reachable: {msg}"
    # Wake the panel BEFORE the start. A TV in standby accepts "am start"
    # perfectly happily and brings the app up on a screen that is off, so this
    # returns "woken", the film plays, and the room sees nothing. KEYCODE_WAKEUP
    # is what turns the panel on, and it is a no-op on a TV already awake.
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "am", "start", "-n", PLAYER, "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LEANBACK_LAUNCHER")
    deadline = time.time() + APP_WAKE_SECS
    while time.time() < deadline:
        if app_fresh():
            print("app: woken by adb", flush=True)
            return True, "woken"
        time.sleep(0.5)
    return False, "The TV app did not start"

def tv_stop():
    # There is no external player left to force-stop -- the app IS the player,
    # and force-stopping it would just dump whoever is looking at the TV back to
    # the launcher for no reason. If it is fresh, /api/stop already queued it a
    # `stop` command over the heartbeat channel before calling here. What always
    # has to happen regardless is killing any orphaned conversion: an ffmpeg left
    # running keeps pulling the swarm even with nothing left to play it back.
    transcode_stop_all()
    if SENDSPIN_ENABLED:
        _hifi_release()
    return True, "stopped"

# ---- the TV app: registry and command channel --------------------------------
# The product plays films in a native app ON the TV, which is why none of this
# goes near adb. The app cannot be dialled into -- the server owns no route to
# it, it may be asleep, and its address is nobody's business -- so the connection
# runs the other way: the app posts its own state and long-polls the SAME request
# for the next command. That gives a play latency well under a second without the
# server ever needing to reach the TV, and it costs one connection, not a socket
# server of its own.
APP_STATES = ("idle", "buffering", "playing", "paused", "ended", "error")
_app     = None      # the one connected app, or None
_app_cmd = None      # the command it has not acked yet
# Monotonic: lets the app tell a redelivery from a new order. Seeded from the
# clock rather than 0 because the app dedupes on the seq being DIFFERENT from
# the last one it handled, and that memory survives a server restart: a fresh
# process counting from 1 issued commands the app had already seen the numbers
# of, and they were silently ignored. Seconds since the epoch only ever goes up,
# including across restarts.
_app_seq = int(time.time())
# The last heartbeat that said something was actually on screen, kept so a lost
# poll does not read as the end of the film. See APP_GRACE / tv_playback_state().
_app_last_play = {"state": None, "at": 0.0}
# Built on _lock rather than a lock of its own, so the registry and the command
# queue are one piece of state: a heartbeat decides whether to sleep while
# holding exactly the lock a queued command must take before it can wake anyone.
_app_cv  = threading.Condition(_lock)

def app_fresh():
    """The connected app, if its last heartbeat is recent enough to believe."""
    with _lock:
        if _app and time.time() - _app["seen_at"] < APP_TTL:
            return dict(_app)
    return None

def app_cmd(kind, **fields):
    """Queue one command for the app, replacing anything it has not taken yet.

    Replace rather than queue: the only reason an unacked command exists is that
    the app has not polled in the last instant, and an app about to be told to
    play a different film has no use for the previous order. Returns the seq.

    Every command carries an expiry, because nothing else retires one: an app
    that is off when the order is queued would otherwise take it on whenever it
    next appears, however many hours later.
    """
    global _app_cmd, _app_seq
    with _app_cv:
        _app_seq += 1
        cmd = {"seq": _app_seq, "type": kind, "expires_at": time.time() + APP_CMD_TTL}
        cmd.update(fields)
        _app_cmd = cmd
        _app_cv.notify_all()
        return _app_seq

def app_cmd_clear(seq):
    """Drop the pending command, but only if it is still the one `seq` queued.

    For a caller that has given up on its own order -- launch() timing out or
    being superseded. Guarded by the seq so it can never eat a NEWER command
    queued in the meantime, which is exactly what a supersede has just done.
    """
    global _app_cmd
    with _app_cv:
        if _app_cmd is not None and _app_cmd.get("seq") == seq:
            _app_cmd = None
            return True
    return False

def _cmd_wire(c):
    """The command as the app sees it. expires_at is the server's own
    bookkeeping and means nothing to a client that dedupes on seq."""
    if not c:
        return None
    return {k: v for k, v in c.items() if k != "expires_at"}

def _secs(v):
    """A position/duration the app reported, or None. Never raises on rubbish."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None

# ---- sendspin: hi-fi (bit-perfect) audio over the bridge sidecar -----------
# The bridge, not this process, owns the DAC and the ffmpeg feeding it. All
# network I/O to it happens on _ss_worker's own thread so a slow or dead
# bridge can never block the heartbeat handler or be reached while _lock (==
# _app_cv's lock) is held -- see the callers below, which only ever put() here.
_hifi = {"on": False, "gen": int(time.time()), "t0_us": None, "clock_offset_us": 0,
         "streaming": False, "connected": False, "src": None, "aidx": 0,
         # Centre mode: the TV's own speakers play the film's centre channel
         # and the Sendspin player gets L/R with the centre taken out. "centre"
         # is the TV's setting off the heartbeat; "film_centre" is what the
         # film on screen was started with, fixed for that film so the bridge's
         # cache and the TV's own decode never disagree mid-film.
         "centre": False, "film_centre": False,
         "last_restart": 0.0, "err_s": None, "player_url": None,
         "pending_since": 0.0, "seek_seq": None, "delay_ms": HIFI_AUDIO_DELAY_MS,
         # Why the last start did not produce audio, for the TV to show instead
         # of a silent film; cleared the moment audio goes live.
         "last_error": None, "fail_count": 0,
         # delay_ms is what the viewer asked for; applied_delay_ms is what the
         # bridge says its timeline carries. err_s uses the applied one, so the
         # picture never chases a trim the sound has not taken on yet.
         "applied_delay_ms": HIFI_AUDIO_DELAY_MS, "delay_at": 0.0,
         # The last trim the TV reported. Only a CHANGE to it is applied: the
         # TV repeats its stored value on every beat, which would otherwise
         # undo a trim set from anywhere else within a second.
         "tv_delay_ms": None,
         # The player's own volume (0-100) as the bridge last reported it, for
         # the TV's volume badge; None until a /status or /volume says.
         "volume": None,
         # The bridge's decoder supply counters (stalls, queued audio), so a
         # dropout can be blamed on the source or the player, not guessed at.
         "supply": None, "cache": None,
         # The job whose audio src is: a heartbeat for any other job (the
         # previous film still on screen while this one buffers) starts nothing.
         "job": None,
         # This film's audio has played out: the track is over, the player has
         # been let go, and the last frames on screen are not a reason to start
         # anything. Cleared by the next timeline -- see _hifi_track_over().
         "done": False}


def _hifi_invalidate():
    """Retire the timeline before queuing I/O, including in-flight old replies."""
    _hifi["gen"] += 1
    _hifi["t0_us"] = None
    _hifi["streaming"] = False
    _hifi["pending_since"] = 0.0
    _hifi["err_s"] = None
    # Every caller is starting a timeline over: a seek, a new film, a stream
    # that died. Whatever finished before does not speak for this one.
    _hifi["done"] = False


def _hifi_release():
    """The film is over or the TV has gone: the timeline dies now, under
    _lock, and the bridge is told afterwards on the worker thread."""
    with _lock:
        _hifi_invalidate()
        _hifi["connected"] = False
        _hifi["fail_count"] = 0
        _hifi["last_error"] = None
        # A heartbeat that was already on its way when the film was stopped
        # must not start the audio again: there is no film.
        _hifi["src"] = None
        _hifi["job"] = None
        _hifi["cache"] = None
        _hifi["supply"] = None
        # Music Assistant gets the player back and may change its volume.
        _hifi["volume"] = None
    _ss_q.put(("release",))


def _hifi_track_over(position_s):
    """Whether this film's audio has run out: the bridge holds the whole track
    and the picture is at or past the end of it. Called under _lock.

    The end of a film reaches the state machine as a stream that stopped with
    its decoder finished, which is indistinguishable from an audio failure
    until you look at the cache. Read as a failure it starts the audio again,
    at a position with no audio behind it, and the bridge answers that by
    creating a stream for it -- which puts the player back into PLAYING for a
    push that ends with nothing sent. The player keeps that state: this is the
    Sendspin client still playing in Music Assistant after Fight Club ended on
    2026-09-17, where a stop mid-film always released it cleanly.

    Only ever true in the last DECODE_LEAD_S of a film: `complete` means
    ffmpeg reached the end of the track, which it cannot do earlier."""
    cache = _hifi["cache"] or {}
    if not cache.get("complete") or cache.get("src") != _hifi["src"]:
        return False
    end_s = cache.get("end_s")
    return end_s is not None and position_s >= end_s - HIFI_TRACK_END_S


def _hifi_fail(msg):
    """Record why audio is not running; the next start waits longer for it."""
    _hifi["last_error"] = msg
    _hifi["fail_count"] += 1
    _hifi["pending_since"] = 0.0
    _hifi["streaming"] = False
    _hifi["t0_us"] = None


def _hifi_restart_gap_s():
    return min(HIFI_RESTART_MIN_GAP_S * (2 ** _hifi["fail_count"]), HIFI_RESTART_MAX_GAP_S)


def _hifi_set_delay(ms, persist=False):
    """Clamp and apply the lip-sync trim. Called under _lock from the heartbeat
    (no persistence there: the TV keeps its own copy) and from the API. The
    bridge moves the SOUND by this much, rather than the TV moving the picture,
    so it is heard as soon as the queued audio drains."""
    ms = max(-2000, min(5000, int(ms)))
    if ms != _hifi["delay_ms"]:
        _hifi["delay_ms"] = ms
        _hifi["delay_at"] = time.time()
        print("hifi: audio delay %+d ms" % ms, flush=True)
        _ss_q.put(("delay", ms))
    if persist:
        _now["hifi_delay_ms"] = ms
        _now_save(_now)
    return ms
_ss_q = queue.Queue()

def _hifi_apply_status(body):
    """Fold a bridge /status into _hifi. Only the gen this process last asked
    for has a timeline; anything else the bridge reports is either a stream we
    already retired or proof that the bridge lost ours (it restarted, or the
    player dropped), in which case the next playing heartbeat starts afresh."""
    now = time.time()
    with _lock:
        _hifi["connected"] = bool(body.get("connected"))
        _hifi["clock_offset_us"] = body.get("clock_offset_us") or 0
        supply = body.get("supply")
        if isinstance(supply, dict):
            prev = _hifi["supply"] or {}
            if supply.get("stalls", 0) > prev.get("stalls", 0):
                print("hifi: decoder supply stalled %d times (max %.0f ms), %.0f ms queued"
                      % (supply.get("stalls", 0), supply.get("max_stall_ms") or 0,
                         supply.get("ahead_ms") or 0), flush=True)
            _hifi["supply"] = supply
        _hifi["cache"] = body.get("cache")
        if body.get("delay_ms") is not None:
            _hifi["applied_delay_ms"] = body["delay_ms"]
        if body.get("volume") is not None:
            _hifi["volume"] = body["volume"]
        pending = _hifi["pending_since"] > 0
        live = _hifi["streaming"] and _hifi["t0_us"] is not None
        if body.get("gen") != _hifi["gen"]:
            if live:
                # Our stream is gone from the bridge without us stopping it.
                print("sendspin: bridge lost gen=%d (reports gen=%s), audio will restart"
                      % (_hifi["gen"], body.get("gen")), flush=True)
                _hifi_invalidate()
                _hifi["last_error"] = "audio bridge restarted"
            # Pending: our /start is still on its way to the bridge. Left alone;
            # HIFI_START_TIMEOUT_S is the way out if it never lands.
            return
        if body.get("streaming"):
            t0 = body.get("t0_us")
            if t0 is not None and (pending or not live):
                _hifi["t0_us"] = t0
                _hifi["streaming"] = True
                _hifi["pending_since"] = 0.0
                _hifi["last_error"] = None
                _hifi["fail_count"] = 0
            elif t0 is not None and t0 != _hifi["t0_us"]:
                # The bridge's timeline moved under the same gen: either a
                # lip-sync trim we asked for (quiet, it is doing as it was
                # told) or its source stalled and the library rebased. Follow
                # it either way: the TV corrects against where the audio is.
                if now - _hifi["delay_at"] > 5:
                    print("hifi: timeline moved %+.0f ms"
                          % ((t0 - _hifi["t0_us"]) / 1000.0), flush=True)
                _hifi["t0_us"] = t0
            return
        if live:
            cache = body.get("cache") or {}
            if body.get("connected") and cache.get("complete") and not cache.get("error"):
                # The push ran off the end of a track the bridge had decoded
                # in full: the film's audio is over, not broken. Retired, but
                # not recorded as a failure and not restarted -- the next
                # playing heartbeat reads the same cache through
                # _hifi_track_over() and lets the player go.
                print("sendspin: stream gen=%d reached the end of the track at %.1fs"
                      % (_hifi["gen"], cache.get("end_s") or 0.0), flush=True)
                _hifi_invalidate()
            else:
                why = "player disconnected" if not body.get("connected") else "decoder ended"
                print("sendspin: stream gen=%d stopped (%s), audio will restart"
                      % (_hifi["gen"], why), flush=True)
                _hifi_invalidate()
                _hifi["last_error"] = why
        elif pending and not body.get("pending"):
            print("sendspin: start gen=%d died before any audio, will retry"
                  % _hifi["gen"], flush=True)
            _hifi_fail("decoder died before any audio")
        elif pending and now - _hifi["pending_since"] > HIFI_START_TIMEOUT_S:
            print("sendspin: start gen=%d produced no audio in %.0fs, will retry"
                  % (_hifi["gen"], HIFI_START_TIMEOUT_S), flush=True)
            _hifi_fail("no audio within %.0f s" % HIFI_START_TIMEOUT_S)

def _ss_call(path, body=None, timeout=3.0):
    """Talk to the bridge. Never raises -- a bridge that is down, slow, or
    answers with garbage must not take out the worker thread. /status and
    /players are the only GETs on this API; everything else is POST, empty
    body or not.
    Returns (status, dict); status 0 means the request itself never landed."""
    method = "GET" if path in ("/status", "/players") else "POST"
    data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
    req = urllib.request.Request(SENDSPIN_BRIDGE + path, data=data, method=method,
                                  headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as ex:
        try:
            return ex.code, json.loads(ex.read().decode("utf-8", "replace") or "{}")
        except Exception:
            return ex.code, {"error": str(ex)}
    except Exception as ex:
        return 0, {"error": str(ex)}

def _ss_worker():
    """The only thread that ever calls the bridge. Drains _ss_q; when nothing
    is queued for 5s (queue.get(timeout=5) is the tick) and a hifi film is
    actually playing, it polls GET /status instead so streaming/connected/t0_us
    stay current even with no action pending."""
    while True:
        with _lock:
            pending = _hifi["pending_since"] > 0
        try:
            # A pending start is polled briskly: t0 is what the TV is waiting on.
            action = _ss_q.get(timeout=0.5 if pending else 5)
        except queue.Empty:
            action = None
        try:
            if action is None:
                with _lock:
                    on = _hifi["on"]
                    pending = _hifi["pending_since"] > 0
                if not on or (not pending and tv_playback_state() not in (2, 3)):
                    continue
                status, body = _ss_call("/status")
                if status == 200:
                    _hifi_apply_status(body)
                continue
            kind = action[0]
            if kind == "connect":
                with _lock:
                    player_url = _hifi["player_url"]
                payload = {"url": player_url} if player_url else {}
                status, body = _ss_call("/connect", payload, timeout=HIFI_CONNECT_TIMEOUT_S)
                with _lock:
                    _hifi["connected"] = status == 200 and bool(body.get("connected"))
            elif kind == "prepare":
                # The film is still pre-buffering: get the player and start
                # decoding the track into the bridge's cache now, so the first
                # playing heartbeat is answered from the cache, not by ffmpeg
                # seeking the swarm.
                _, src, aidx, gen = action
                with _lock:
                    if _hifi["gen"] != gen or _hifi["src"] != src:
                        continue
                    player_url = _hifi["player_url"]
                    centre = _hifi["film_centre"]
                status, body = _ss_call("/connect", {"url": player_url}, timeout=HIFI_CONNECT_TIMEOUT_S)
                with _lock:
                    _hifi["connected"] = status == 200 and bool(body.get("connected"))
                    if status != 200:
                        _hifi["last_error"] = str(body.get("error") or "bridge answered %s" % status)
                        print("sendspin: connect failed: %s" % _hifi["last_error"], flush=True)
                    if _hifi["gen"] != gen or _hifi["src"] != src:
                        continue
                status, body = _ss_call("/prepare", {"src": src, "aidx": aidx, "centre": centre},
                                        timeout=HIFI_CALL_TIMEOUT_S)
                if status != 200:
                    print("sendspin: prepare failed (%s): %s" % (status, body.get("error")), flush=True)
            elif kind == "start":
                _, src, aidx, start_s, gen, pos_at_us = action
                with _lock:
                    if _hifi["gen"] != gen:
                        continue
                    player_url = _hifi["player_url"]
                # Idempotent when already connected; recovers after either service
                # restarts or the hifi client temporarily drops off the network.
                status, body = _ss_call("/connect", {"url": player_url}, timeout=HIFI_CONNECT_TIMEOUT_S)
                with _lock:
                    if status == 200 and body.get("connected"):
                        _hifi["connected"] = True
                    if _hifi["gen"] != gen:
                        continue
                if status == 200:
                    with _lock:
                        delay_ms = _hifi["delay_ms"]
                        centre = _hifi["film_centre"]
                    status, body = _ss_call("/start", {"src": src, "aidx": aidx, "centre": centre,
                                                         "start_s": start_s, "gen": gen,
                                                         "pos_at_us": pos_at_us,
                                                         "delay_ms": delay_ms},
                                            timeout=HIFI_CALL_TIMEOUT_S)
                    if status in (200, 202):
                        with _lock:
                            _hifi["applied_delay_ms"] = delay_ms
                with _lock:
                    if _hifi["gen"] != gen:
                        continue
                    if status == 200 and body.get("t0_us") is not None:
                        _hifi["t0_us"] = body["t0_us"]
                        _hifi["pending_since"] = 0.0
                        _hifi["clock_offset_us"] = body.get("clock_offset_us") or 0
                        _hifi["streaming"] = True
                        _hifi["connected"] = True
                        _hifi["last_error"] = None
                        _hifi["fail_count"] = 0
                    elif status == 202:
                        _hifi["clock_offset_us"] = body.get("clock_offset_us") or 0
                    elif status == 0:
                        # A lost HTTP response does not prove the start failed.
                        # Keep it pending and discover the outcome through /status.
                        print("sendspin: start gen=%d: %s, waiting on /status"
                              % (gen, body.get("error")), flush=True)
                    else:
                        msg = str(body.get("error") or "bridge answered %s" % status)
                        print("sendspin: start gen=%d failed: %s" % (gen, msg), flush=True)
                        _hifi_fail(msg)
            elif kind == "delay":
                _, ms = action
                status, body = _ss_call("/delay", {"ms": ms}, timeout=HIFI_CALL_TIMEOUT_S)
                if status == 200 and body.get("delay_ms") is not None:
                    with _lock:
                        _hifi["applied_delay_ms"] = body["delay_ms"]
            elif kind == "stop":
                _ss_call("/stop", timeout=HIFI_CALL_TIMEOUT_S)
            elif kind == "release":
                _ss_call("/release", timeout=HIFI_CALL_TIMEOUT_S)
                with _lock:
                    _hifi["connected"] = False
            elif kind == "volume":
                _, payload = action
                status, body = _ss_call("/volume", payload)
                if status == 200 and body.get("volume") is not None:
                    with _lock:
                        _hifi["volume"] = body["volume"]
        except Exception as ex:
            print("sendspin: worker error: %s" % ex, flush=True)

def app_heartbeat(d):
    """One heartbeat in, the pending command out."""
    global _app, _app_cmd
    now   = time.time()
    aid   = str(d.get("id") or "")[:64]
    name  = str(d.get("name") or "")[:80]
    state = d.get("state") if d.get("state") in APP_STATES else "idle"
    job   = str(d.get("job")) if d.get("job") not in (None, "") else None
    err   = str(d.get("err") or "")[:300] or None
    hifi  = bool(d.get("hifi"))
    try:
        wait = min(max(float(d.get("wait") or 0), 0.0), 10.0)
    except (TypeError, ValueError):
        wait = 0.0
    sync = None
    hifi_status = None
    with _app_cv:
        prev = _app
        if not prev or now - prev["seen_at"] >= APP_TTL:
            print("app: connected — %s %s (%s)" % (name or "?",
                  d.get("version") or "?", aid), flush=True)
        elif prev["id"] != aid:
            # One app at a time, deliberately: there is one TV. A new id simply
            # takes over, but it is worth a line in the log -- two installs
            # fighting over the player would otherwise look like the server
            # issuing random stop commands.
            print("app: %s (%s) replaced %s (%s)" % (name or "?", aid,
                  prev["name"] or "?", prev["id"]), flush=True)
        _app = {"id": aid, "name": name,
                "version": str(d.get("version") or "")[:40],
                "state": state, "title": d.get("title"), "job": job,
                "position_s": _secs(d.get("position_s")),
                "duration_s": _secs(d.get("duration_s")),
                # not part of the state the rest of the server reads, but launch()
                # has to be able to say WHY the TV refused a film
                "err": err, "seen_at": now, "hifi": hifi}
        # Remember the last beat that had something on screen, and forget it the
        # moment the app says otherwise. tv_playback_state() reads this so one
        # lost poll does not read as the end of the film. "buffering" neither
        # confirms nor denies, so it leaves the memory alone.
        if state in ("playing", "paused"):
            _app_last_play.update(state=state, at=now)
        elif state in ("idle", "ended", "error"):
            _app_last_play.update(state=None, at=0.0)
        # Sendspin hifi: enqueue only, never call the bridge from in here --
        # this is _app_cv's lock (== _lock) and the bridge is a network call
        # on _ss_worker's thread. Sign convention: err_s = tv_position_s -
        # audio_pos_s, positive means the picture is ahead of the sound.
        _hifi["on"] = hifi
        # The TV names which Sendspin player is next to it; a change mid-film
        # is left alone (the current stream keeps its player) and only takes
        # effect on the next film, but a change while nothing is playing can
        # drop the old connection right away so the next connect() picks up
        # the new one instead of the bridge's stale default.
        _hifi["centre"] = bool(d.get("hifi_centre"))
        new_player = d.get("hifi_player") or None
        if new_player != _hifi["player_url"]:
            _hifi["player_url"] = new_player
            if _hifi["connected"] and state not in ("playing", "paused") and _hifi["job"] is None:
                # Not while a film is being prepared: its start re-connects to
                # the new player anyway, keeping the decoded cache.
                _ss_q.put(("release",))
        position_s = _app["position_s"]
        seek_seq = d.get("seek_seq")
        viewer_seek = (seek_seq is not None and _hifi["seek_seq"] is not None
                       and seek_seq != _hifi["seek_seq"])
        _hifi["seek_seq"] = seek_seq
        if hifi and d.get("hifi_delay_ms") is not None:
            try:
                tv_ms = max(-2000, min(5000, int(d["hifi_delay_ms"])))
                # First beat of a session: the TV's stored value is the truth.
                # After that only a change to it is an instruction.
                if tv_ms != _hifi["tv_delay_ms"]:
                    _hifi["tv_delay_ms"] = tv_ms
                    _hifi_set_delay(tv_ms)
            except (TypeError, ValueError):
                pass
        active_audio = _hifi["streaming"] or _hifi["pending_since"] > 0 or _hifi["t0_us"] is not None
        if hifi and state == "idle" and _hifi["job"] is not None and not active_audio:
            # Idle because the next film is still pre-buffering: the bridge is
            # connected and decoding it on purpose. Releasing now would throw
            # that cache away and leave the start to a cold decoder.
            pass
        elif not hifi or state in ("idle", "ended", "error"):
            if active_audio or _hifi["connected"]:
                _hifi_invalidate()
                _hifi["connected"] = False
                _ss_q.put(("release",))
            _hifi["fail_count"] = 0
            _hifi["last_error"] = None
        elif state in ("paused", "buffering"):
            if active_audio:
                _hifi_invalidate()
                _ss_q.put(("stop",))
        elif state == "playing" and position_s is not None and not _hifi["src"]:
            _hifi["last_error"] = "no audio source for this film"
        elif state == "playing" and position_s is not None and job != _hifi["job"]:
            # The previous film is still on screen while the next one buffers
            # (its src already points at the new one). Nothing to start yet.
            pass
        elif state == "playing" and position_s is not None:
            if viewer_seek:
                _hifi_invalidate()
                _hifi["fail_count"] = 0
            pending = _hifi["pending_since"] > 0
            if pending and now - _hifi["pending_since"] > HIFI_START_TIMEOUT_S:
                print("hifi: start gen=%d produced no audio in %.0fs, will retry"
                      % (_hifi["gen"], HIFI_START_TIMEOUT_S), flush=True)
                _hifi_fail("no audio within %.0f s" % HIFI_START_TIMEOUT_S)
                pending = False
            if not pending and (not _hifi["streaming"] or _hifi["t0_us"] is None):
                if _hifi["done"]:
                    # The audio is over and the player has been let go. The
                    # film still on screen is its last frames, not a reason to
                    # take the player again.
                    pass
                elif _hifi_track_over(position_s):
                    print("hifi: audio track finished at %.1fs, releasing the player"
                          % position_s, flush=True)
                    _hifi_invalidate()
                    _hifi["done"] = True
                    _hifi["connected"] = False
                    # src and job stay: the film is still on screen, and
                    # clearing them would put "no audio source for this film"
                    # over its last seconds.
                    _ss_q.put(("release",))
                elif viewer_seek or now - _hifi["last_restart"] > _hifi_restart_gap_s():
                    _hifi_invalidate()
                    _hifi["last_restart"] = now
                    _hifi["pending_since"] = now
                    # Start at the reported position. The TV follows the actual
                    # first-sample timestamp, with no guessed/adaptive lead.
                    print("hifi: start gen=%d at %.3fs%s" % (_hifi["gen"], position_s,
                          ", viewer seeked" if viewer_seek else ""), flush=True)
                    # The position and the moment it was true, on this clock:
                    # the bridge pins its timeline to exactly that and serves
                    # from its cache, so nothing has to be chased afterwards.
                    _ss_q.put(("start", _hifi["src"], _hifi["aidx"], position_s, _hifi["gen"],
                               time.monotonic_ns() // 1000))
            elif _hifi["streaming"] and _hifi["t0_us"] is not None:
                audio_pos_s = ((time.monotonic_ns() // 1000
                                + _hifi["clock_offset_us"] - _hifi["t0_us"]) / 1e6)
                # audio_pos already carries the trim (it is in t0), so taking
                # it off again leaves err as the true picture-to-sound error:
                # the trim moves the sound, and the TV holds its ground.
                err_s = round(position_s - audio_pos_s
                              - _hifi["applied_delay_ms"] / 1000.0, 3)
                _hifi["err_s"] = err_s
                sync = {"gen": _hifi["gen"], "audio_pos_s": round(audio_pos_s, 3), "err_s": err_s}
        if hifi and state in ("playing", "paused", "buffering"):
            # What the TV shows in place of a silently muted film.
            if sync is not None:
                hst = "live"
            elif state != "playing":
                hst = "stopped"
            elif _hifi["done"]:
                # The track ended before the picture did. Stopped, not failed:
                # nothing went wrong and there is nothing to wait for.
                hst = "stopped"
            elif _hifi["pending_since"] > 0:
                hst = "starting"
            elif _hifi["last_error"]:
                hst = "failed"
            else:
                hst = "starting"
            hifi_status = {"state": hst, "msg": _hifi["last_error"] if hst == "failed" else None}
            if _hifi["volume"] is not None:
                hifi_status["volume"] = _hifi["volume"]
        # An order that has been sitting here longer than APP_CMD_TTL is stale by
        # definition -- the app was away for it -- and delivering it now would
        # start a film for a request made in another part of the evening.
        if _app_cmd is not None and now >= _app_cmd.get("expires_at", 0):
            print("app: dropped expired %s command seq=%s" %
                  (_app_cmd.get("type"), _app_cmd.get("seq")), flush=True)
            _app_cmd = None
        # The app acks the command it has taken on. Until it does, the command is
        # redelivered on every heartbeat, so a response lost on the way to the TV
        # costs one poll rather than the whole play.
        ack = d.get("ack")
        try:
            _app["acked"] = int(ack) if ack is not None else (prev or {}).get("acked")
        except (TypeError, ValueError):
            _app["acked"] = (prev or {}).get("acked")
        if _app_cmd is not None and ack is not None and str(ack) == str(_app_cmd["seq"]):
            _app_cmd = None
        cmd = _cmd_wire(_app_cmd)
        if cmd is None and wait > 0:
            # Long-poll. The alternative is an app polling fast enough that a
            # play feels instant, which is a request every few hundred ms for as
            # long as the TV is on, all night, to say nothing has happened.
            deadline = now + wait
            while _app_cmd is None:
                left = deadline - time.time()
                if left <= 0:
                    break
                _app_cv.wait(left)
            # Anything that arrives during the wait was queued within the last
            # few seconds, so there is nothing to expire here.
            cmd = _cmd_wire(_app_cmd)
    # The shelf, outside the lock above for the reason shelf_note() gives.
    # The same report the TV sends for its own sake is the only thing that
    # knows where a film got to, so it is fed straight through -- including
    # "ended", which is what marks a FILM watched as well as an episode.
    # A position is not always reported with an ending; treat a missing one
    # as zero there rather than lose the fact that the thing finished.
    prev_state = (prev or {}).get("state")
    if job and state in ("playing", "paused", "ended"):
        shelf_note(job, position_s if position_s is not None
                        else (0.0 if state == "ended" else None),
                   _secs(d.get("duration_s")), state,
                   force_save=(state != prev_state))
    elif state == "idle" and prev_state in ("playing", "paused"):
        # Stopped without an ending. Nothing new to record, but whatever the
        # throttle is still holding belongs on disk now.
        shelf.save(force=True)
    # Outside the lock, and only for a job that exists: a film the TV could not
    # open must fail its job now, rather than leaving the phone on "Handing over
    # to the TV app" until the handoff deadline runs out.
    if state == "error" and job and err and job_get(job):
        job_set(job, stage="error", ok=False, msg=err)
    # Autoplay: catch the playing/paused -> ended edge exactly once (prev
    # is the heartbeat BEFORE this one, so a run of "ended" samples only
    # matches on the first) and, for an episode job with autoplay set,
    # hand the next one straight to start_play() without waiting for the
    # app or the phone to ask.
    if (job and job.startswith("tv:") and state == "ended"
            and prev and prev.get("state") in ("playing", "paused")):
        try:
            if (job_get(job) or {}).get("autoplay"):
                tid, s, e = tv_job_parts(job)
                nxt = next_episode(tid, s, e)
                if nxt:
                    ns, ne = nxt
                    def _resolve():
                        en = get_stream_tv(tid, ns, ne)
                        return en, en.get("runtime") or 45
                    print("autoplay: %s ended -> tv:%s:%d:%d" %
                          (job, tid, ns, ne), flush=True)
                    threading.Thread(target=start_play,
                                      args=(f"tv:{tid}:{ns}:{ne}", _resolve),
                                      kwargs={"autoplay": True},
                                      daemon=True).start()
                else:
                    print("autoplay: %s ended, no next episode" % job, flush=True)
        except Exception as ex:
            print("autoplay: %s failed: %s" % (job, ex), flush=True)
    reply = {"ok": True, "cmd": cmd}
    if sync is not None:
        reply["sync"] = sync
    if hifi_status is not None:
        reply["hifi_status"] = hifi_status
    return reply

# ---- pre-buffer jobs --------------------------------------------------------
# Stremio downloads sequentially on demand, so VLC starts playing while the
# engine is still finding peers -- which is exactly the first ~30s of stutter.
# Pulling a head start into the cache ourselves before launching the player
# removes it.
_jobs = {}      # movie id -> progress dict

# One transcode per stream, hard-capped overall. Without this, every player
# reconnect spawns another ffmpeg while the old one keeps running against a dead
# socket -- four of them at once starved the CPU and caused the very glitching
# the transcoder exists to prevent.
# Persisted, because it only ever lived in memory before and every service
# restart made a film that was plainly playing report as "Idle".
NOW_FILE = os.path.join(HERE, "nowplaying.json")
def _now_load():
    try:
        return json.load(open(NOW_FILE))
    except Exception:
        return {}
def _now_save(d):
    try:
        tmp = NOW_FILE + ".tmp"
        json.dump(d, open(tmp, "w"))
        os.replace(tmp, NOW_FILE)
    except Exception:
        pass
# Favourites / watched / resume, persisted beside nowplaying.json for the same
# reason: it is state about what this household is in the middle of, and a
# service restart (every deploy is one) must not lose where a film got to.
# shelf.py owns every rule about it; this file only says where the file lives
# and feeds it what the players report.
# CINEMATICA_SHELF exists for the test suite, which drives real heartbeats
# through this module: without somewhere else to point it, a test run on the Pi
# would write "Stub Movie 1" into the household's actual watch history.
SHELF_FILE = os.environ.get("CINEMATICA_SHELF") or os.path.join(HERE, "shelf.json")
shelf.init(SHELF_FILE)

def _pool_item(tid):
    """This title as the browse pool already holds it, or None.

    Memory only, deliberately: the two callers are a play start and the
    /api/movies pinned row, and neither is worth a network fetch. A pool item
    carries the poster/year/external ids a shelf snapshot wants AND the
    stream/rating/quality fields a wall tile wants, which the snapshot alone
    can never have.
    """
    with _lock:
        pools = list(_pool.values())
    for st in pools:
        for rows in (st.get("served"), st.get("cands")):
            for row in rows or ():
                if row.get("id") == tid:
                    return row
    return None

def shelf_pins(kind):
    """shelf.pins() with this process's own richer copy of a title preferred
    wherever it still has one. The stored snapshot carries only
    title/year/poster/imdb_id -- correct per the contract, and clients treat
    the rest as unknown -- but when the pool is warm there is no reason to
    make them: hand over the full tile instead."""
    out = []
    for item in shelf.pins(kind):
        rich = _pool_item(item.get("id"))
        out.append(shelf.decorate(dict(rich)) if rich is not None else item)
    return out

def _ep_view(tid, s, ep):
    """One episode row's shelf fields, tolerating an episode number a provider
    left unusable -- the row still goes out, just with nothing known about it."""
    n = ep.get("episode")
    if not isinstance(n, int):
        return {"watched": False, "progress": None, "resume_s": None}
    return shelf.episode_view(tid, s, n)

def _shelf_begin(jobid, entry, runtime_min):
    """Everything the shelf needs to know about a play, worked out ONCE when
    it starts and returned as fields to store on the job.

    All of it -- the runtime, the snapshot, a series' aired count and whether
    a next episode exists -- is either a provider call or a scan of the pool,
    and a heartbeat arrives every couple of seconds. Doing it here means the
    heartbeat path reads a dict and nothing else.
    """
    tid, s, e = shelf.parse_job(jobid)
    if tid is None:
        return {}
    # A play is the viewer coming back: whatever an earlier "done with this"
    # muted, this exact job records again from now on.
    shelf.begin(jobid)
    kind = "tv" if s is not None else "movie"
    out = {"shelf_kind": kind,
           # The true length, for note_progress: a film still converting
           # reports "converted so far" as its duration, which would make an
           # early position look like the end of the film.
           "runtime_s": (runtime_min * 60) if runtime_min else None}
    det = None
    if kind == "tv":
        try:
            det = tv_detail(tid)
        except Exception:
            det = None       # a snapshot is never worth failing a play for
        # aired: the seasons roster the detail already carries, specials
        # (season 0) left out. It is the FULL episode count rather than the
        # aired one, so for a show still airing it OVERSTATES what has aired
        # -- which can only hold "watched" back, never declare a series
        # finished early, and costs no per-season fetch on the play path.
        counts = [sn.get("episodes") for sn in ((det or {}).get("seasons") or [])
                  if sn.get("n")]
        if counts and all(isinstance(c, int) for c in counts):
            out["shelf_aired"] = sum(counts)
        out["shelf_has_next"] = next_episode(tid, s, e) is not None
    if shelf.snapshot_needed(tid):
        # Only when the shelf has nothing to render this title with yet.
        # For a series the 24h detail is both cheaper and better than a pool
        # scan; for a film the pool is the only thing here that knows its
        # poster, and the stream entry's title is the last resort. An episode
        # job's entry title is "Show · S01E02 · Name", which is not the
        # series' name, so it is never used as one.
        src = (det if kind == "tv" else None) or _pool_item(tid) or {}
        out["shelf_snap"] = {
            "title": src.get("title") or ((entry or {}).get("title")
                                          if kind == "movie" else None),
            "year": src.get("year"),
            "poster": src.get("poster"),
            "imdb_id": ((src.get("external_ids") or {}).get("imdb")
                        or src.get("imdb_id") or (entry or {}).get("imdb_id"))}
    return out

def shelf_note(job, pos_s, dur_s, state, force_save=False):
    """One player report -> the shelf.

    NEVER call this holding _lock (_app_cv's lock is the same one): shelf has
    a lock of its own and save() writes to disk, and the heartbeat handler is
    the last place in this server that should be doing file I/O under a lock
    every other request needs.

    save() is self-throttled to once every FLUSH_S, so a run of "playing"
    beats costs nothing; force is for the moments that are actually worth a
    write -- a pause, an ending, a stop.
    """
    j = job_get(job)
    start_s = j.get("start_s")
    # Resume guard. A TV that has been told to start at start_s still reports
    # 0 for the beats before it seeks, and recording those would overwrite the
    # very resume point the viewer just used with the start of the film.
    if start_s is not None and pos_s is not None and pos_s < start_s - 10:
        return
    shelf.note_progress(job, pos_s, dur_s, state,
                        runtime_s=j.get("runtime_s"),
                        snap=j.get("shelf_snap"), kind=j.get("shelf_kind"),
                        aired=j.get("shelf_aired"),
                        has_next=j.get("shelf_has_next"))
    shelf.save(force=force_save)

def _start_s(query):
    """The `t=<seconds>` resume offset off a play route's query string: a
    float at or above zero, or None. Rubbish is ignored rather than refused --
    a resume offset that cannot be read should start the film from the
    beginning, not fail it."""
    raw = urllib.parse.parse_qs(query).get("t", [None])[0]
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, v) if math.isfinite(v) else None

_now = _now_load()        # what was last handed to the player
if _now.get("hifi_src"):
    # Restart mid-film: the first playing heartbeat with hifi on restarts the
    # audio from the TV's position, exactly as after a pause.
    _hifi["src"] = _now["hifi_src"]
    _hifi["aidx"] = int(_now.get("hifi_aidx") or 0)
    _hifi["film_centre"] = bool(_now.get("hifi_centre"))
    _hifi["job"] = _now.get("hifi_job")
if _now.get("hifi_delay_ms") is not None:
    _hifi["delay_ms"] = max(-2000, min(5000, int(_now["hifi_delay_ms"])))
_tv_cache = {"at": 0, "state": None}
_transcodes = {}          # infoHash -> Popen
_tc_lock = threading.Lock()
MAX_TRANSCODES = int(os.environ.get("MAX_TRANSCODES", 2))
# The transcoder was a live pipe started when the player connected, so it raced
# from cold exactly when Stremio was still downloading hard and VLC had an empty
# buffer -- about a minute of glitching every time. It now writes to a file
# during the buffer phase and gets a head start before the player ever connects.
TC_HOST  = os.path.join(HERE, "transcode")          # host side of the bind mount
TC_CTR   = os.environ.get("TC_CTR", "/transcode")   # same dir inside the container
# Built at full speed, so this costs seconds of wall time, not TC_HEAD seconds.
TC_HEAD  = int(os.environ.get("TRANSCODE_HEAD_SECS", 60))
# How far ahead of playback the regulator keeps the conversion once it starts.
TC_LEAD  = int(os.environ.get("TRANSCODE_LEAD_SECS", 180))
# Resume once the lead has drained to this fraction of TC_LEAD. Was a hardcoded
# 0.6, which meant ~72s of playback had to drain before ffmpeg was let go again
# -- so it ran in ~27s sprints separated by ~72s of SIGSTOP, with its HTTP
# connection to Stremio sitting idle throughout. A narrow band stop-starts more
# often but never leaves the input idle long enough to go stale.
TC_BAND  = float(os.environ.get("TRANSCODE_LEAD_BAND", 0.9))
TC_KEEP  = int(os.environ.get("TRANSCODE_KEEP", 0))   # cache is emptied per film, so keep none

# The on-demand browser HLS packager: one ffmpeg per session, remuxing the
# source into fMP4 segments a grid-index at a time, paced against the real
# playhead instead of an estimate. See regulate_hls / bx_spawn below.
BX_DIR      = "bx_"                    # per-session directory prefix under TC_HOST
BX_SEG_WAIT = float(os.environ.get("BX_SEG_WAIT", 45))   # long-poll ceiling per segment
BX_LEAD     = float(os.environ.get("BX_LEAD", 300))      # seconds ahead of the playhead
BX_BEHIND   = float(os.environ.get("BX_BEHIND", 600))    # seconds kept behind it
BX_SEEK_DEBOUNCE = float(os.environ.get("BX_SEEK_DEBOUNCE", 0.25))
BX_LOOKAHEAD = int(os.environ.get("BX_LOOKAHEAD", 8))    # segments past the frontier that just wait

def tv_playback_state(max_age=6):
    """3 = playing, 2 = paused, anything else idle.

    One source of truth for everything downstream -- playing_now(), cache_watch(),
    the calibration guard and /api/nowplaying all read it. The TV app wins when
    it is connected because it IS the player: its own state beats anything
    inferred from outside it, and it is already in hand, so no cache is wanted
    here. The dumpsys read below is the fallback, and it keeps the cache: adb is
    not free and the phone polls this every few seconds.
    """
    app = app_fresh()
    if app:
        if app["state"] == "buffering" and app.get("job"):
            # A fresh 4K open or a mid-film rebuffer reports "buffering", not
            # "playing" -- without this it read as a playing -> idle edge and
            # cache_watch() tore down the live transcode and cache under it.
            return 3
        return {"playing": 3, "paused": 2}.get(app["state"])
    # Not fresh is not the same as not playing. APP_TTL is one missed poll, and
    # a film plainly still on screen behind a brief blip used to fall straight
    # through to None here: the phone's now-playing strip flapped, and worse,
    # cache_watch() read the playing -> idle edge and emptied the cache under a
    # film that was still being watched. Believe the last beat that reported
    # something on screen until APP_GRACE; a beat saying idle/ended/error clears
    # that memory immediately, so a real stop is still instant.
    last = dict(_app_last_play)
    if last["state"] and time.time() - last["at"] < APP_GRACE:
        return {"playing": 3, "paused": 2}.get(last["state"])
    if not ADB_ENABLED:
        return None
    now = time.time()
    if now - _tv_cache["at"] < max_age:
        return _tv_cache["state"]
    st = None
    try:
        # dumpsys lists a session per app. Taking the first one meant anything
        # else playing on the TV -- YouTube, the built-in player -- was reported
        # as Cinematica playback, with the last launched title attached to it.
        # `packages=` (plural, in the uid line) deliberately does not match.
        # The app does not register a MediaSession today, so this branch only
        # matters if it ever does -- until then app_fresh() above always wins.
        r = adb("shell", "dumpsys media_session | grep -E 'package=|state=PlaybackState'")
        pkg = None
        for line in (r.stdout or "").splitlines():
            mp = re.search(r"\bpackage=(\S+)", line)
            if mp:
                pkg = mp.group(1).strip().rstrip(",")
                continue
            ms = re.search(r"state=PlaybackState \{state=(\d+)", line)
            if ms and pkg == PLAYER_PKG:
                st = int(ms.group(1))
                break
    except Exception:
        st = None
    _tv_cache.update(at=now, state=st)
    return st

def _kill_ctr(name):
    """Kill the ffmpeg INSIDE the container.

    The host-side Popen is only the docker-exec client; killing that leaves the
    real process converting happily away (_ctr_pid's docstring says as much, but
    the reaper signalled the client anyway). A process the regulator has
    SIGSTOPped also ignores TERM until it is resumed -- and suspended is its
    normal steady state -- so CONT has to come first or the kill is a no-op.
    """
    if not name:
        return
    _kill_pid(_ctr_pid(name))

def _ctr_ffmpegs():
    """(pid, output name) of every ffmpeg in the container writing to /transcode."""
    try:
        r = subprocess.run(["docker", "exec", FFMPEG_CTR, "ps", "-eo", "pid,args"],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or "ffmpeg" not in parts[1] or TC_CTR + "/" not in parts[1]:
            continue
        rel = parts[1].rsplit(TC_CTR + "/", 1)[-1].split()[0]
        # The first path component, not the whole relative path: a browser session
        # writes /transcode/bx_<token>/s000001.m4s and is registered under the
        # DIRECTORY name. Existing .ts outputs sit directly in /transcode and have
        # no slash, so they come through this unchanged.
        name = rel.split("/", 1)[0]
        out.append((parts[0], name))
    return out

def _kill_orphans(keep_name=None):
    """Kill conversions the registry does not know about.

    The registry lives in this process. After a restart -- every deploy is one
    -- an ffmpeg started by the previous process is still converting a film
    nobody is watching, holding the swarm open so the cache can never clear,
    and /api/stop cannot reach it because it was never registered here. Seen
    for real: 16 minutes of conversion after the film had been stopped.
    """
    with _tc_lock:
        known = {v.get("name") for v in _transcodes.values() if isinstance(v, dict)}
    for pid, name in _ctr_ffmpegs():
        if name in known or name == keep_name:
            continue
        print("transcode: killing orphan %s (pid %s)" % (name, pid), flush=True)
        _kill_pid(pid)

def _kill_pid(pid):
    if not pid:
        return
    for sig in ("-CONT", "-TERM"):
        try:
            subprocess.run(["docker", "exec", FFMPEG_CTR, "kill", sig, pid],
                           capture_output=True, timeout=15)
        except Exception:
            pass
    # The doomed process still has its own regulate_lead thread running, which
    # can SIGSTOP it again between the CONT and the TERM -- and a TERM delivered
    # to a stopped process just sits there pending, forever. SIGKILL cannot be
    # blocked and lands even while stopped, so fall back to it. Observed for
    # real: two ffmpegs converting one film, the older writing to an inode that
    # transcode_begin had already unlinked.
    for _ in range(8):
        if _ctr_stopped(pid) is None:
            return                       # gone
        time.sleep(0.25)
    try:
        subprocess.run(["docker", "exec", FFMPEG_CTR, "kill", "-KILL", pid],
                       capture_output=True, timeout=15)
        print("transcode: %s needed SIGKILL" % pid, flush=True)
    except Exception:
        pass

def _stop(ent):
    """Stop one transcode completely: container process first, then the client."""
    if not isinstance(ent, dict):
        return
    _kill_ctr(ent.get("name"))
    proc = ent.get("proc")
    try:
        proc.kill(); proc.wait(timeout=5)
    except Exception:
        pass
    lg = ent.get("log")
    if lg:
        try:
            lg.close()
        except Exception:
            pass

def tc_name(ih, idx):
    return "%s_%s.ts" % (ih, idx if idx is not None else "x")

def tc_cleanup(keep_name=None):
    """A 4K transcode is about the size of the source, so do not hoard them."""
    try:
        files = [os.path.join(TC_HOST, f) for f in os.listdir(TC_HOST) if f.endswith(".ts")]
        files.sort(key=os.path.getmtime, reverse=True)
        with _tc_lock:
            busy = {v.get("name") for v in _transcodes.values() if isinstance(v, dict)}
        for old in files[TC_KEEP:]:
            if os.path.basename(old) in busy or os.path.basename(old) == keep_name:
                continue
            os.remove(old)
    except Exception:
        pass
    # Second pass: a browser session directory left over from a previous
    # process (a deploy, a crash) or an earlier session in THIS process holds
    # a whole remux's worth of segments and is otherwise never revisited, so
    # it has to be swept the same way the orphaned .ts files above are.
    try:
        with _lock:
            live_token = _bx.get("token")
        live_dir = (BX_DIR + live_token) if live_token else None
        for name in os.listdir(TC_HOST):
            if not name.startswith(BX_DIR) or name == live_dir:
                continue
            full = os.path.join(TC_HOST, name)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
    except Exception:
        pass

def transcode_begin(ih, idx, src_internal, bytes_per_sec, mid, gen=None, aidx=0):
    """Start the conversion to disk and wait until it is TC_HEAD seconds ahead.
    Returns the file name on success, else None."""
    os.makedirs(TC_HOST, exist_ok=True)
    name = tc_name(ih, idx)
    host_path = os.path.join(TC_HOST, name)
    tc_cleanup(keep_name=name)
    # No -re here. Flat out it ran ~21x and pinned Stremio at 200% CPU, load 10,
    # which corrupted the audio it was producing. At -re (1x) it had no slack at
    # all and starved the player instead. So neither fixed rate works: it sprints
    # to build a lead, then regulate_lead() suspends it until the lead is spent.
    # Deliberately minimal. This exact command measured ZERO audio glitches the
    # first time it was tried. Adding -fflags +genpts, -af aresample=async=1 and
    # -muxdelay/-muxpreload/-max_interleave_delta to "tighten pacing" made it
    # worse every time since: aresample inserts and drops samples to force sync
    # and genpts rewrites timestamps, which mangles audio the source had right.
    # Do not reintroduce them without measuring.
    # aidx is the track the language check accepted, not necessarily the first.
    cmd = ["docker", "exec", FFMPEG_CTR, FFMPEG, "-hide_banner", "-loglevel", "error",
           "-i", src_internal,
           "-map", "0:v:0", "-map", "0:a:%d" % aidx, "-c:v", "copy",
           "-c:a", "ac3", "-b:a", "448k",
           "-f", "mpegts", "-y", "%s/%s" % (TC_CTR, name)]
    # Belt and braces before unlinking: if anything is still writing this exact
    # output and the registry has lost track of it, removing the file would
    # orphan it onto a deleted inode where it burns CPU and disk unseen.
    _kill_ctr(name)
    try:
        os.remove(host_path)
    except OSError:
        pass
    proc = transcode_start(ih, cmd, name=name)
    want = max(12 * 1048576, int((bytes_per_sec or 0) * TC_HEAD))
    def start_regulator():
        threading.Thread(target=regulate_lead,
                         args=(ih, host_path, bytes_per_sec, proc),
                         daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < HARD_CAP_SECS:
        if gen is not None and superseded(gen):
            _stop({"proc": proc, "name": name})   # a newer play owns the TV now
            return None
        sz = os.path.getsize(host_path) if os.path.exists(host_path) else 0
        if sz >= want:
            # hand the regulator the lead target and let playback begin
            start_regulator()
            return name
        if proc.poll() is not None:
            return name if sz > 4 * 1048576 else None   # finished early = short file
        # This burst is part of the wait before playback, so it belongs in the
        # same progress bar as the source buffering rather than looking stalled.
        job_set(mid, stage="encoding", pct=min(99, int(sz * 100 / max(want, 1))),
                got=sz, target=want,
                msg="Converting audio — %d/%d MB ready" % (sz // 1048576, want // 1048576))
        time.sleep(0.5)
    # HARD_CAP_SECS elapsed without reaching the head target. Playback can still
    # start on a partial head -- but the regulator MUST start too. Returning here
    # without it left ffmpeg running flat out for the rest of the film, which the
    # comment in this function records as ~21x, Stremio pinned at 200% CPU, load
    # 10, and corrupted audio: the exact thing the regulator exists to prevent.
    if os.path.exists(host_path) and os.path.getsize(host_path) > want // 2:
        start_regulator()
        return name
    # Giving up with less than half a head: nobody is returning `name`, so
    # nothing downstream will ever stop it. Take it out of the registry and
    # kill it here rather than leave an unregulated ffmpeg running flat out.
    with _tc_lock:
        ent = _transcodes.pop(ih, None)
    _stop(ent if isinstance(ent, dict) else {"proc": proc, "name": name})
    return None

def _ctr_stopped(pid):
    """Whether the container process is really SIGSTOPped, read from the process
    table rather than remembered. The regulator tracked this in a local flag,
    which anything suspending or resuming ffmpeg from outside silently desynced
    -- after which it believed ffmpeg was still suspended and never suspended it
    again, letting it run unregulated for the whole film."""
    try:
        r = subprocess.run(["docker", "exec", FFMPEG_CTR, "ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=15)
        st = (r.stdout or "").strip()
        return st.startswith("T") if st else None
    except Exception:
        return None

def _ctr_pid(name):
    """PID of the ffmpeg writing `name`, as seen INSIDE the container -- host
    PIDs are the docker-exec client, and signalling those does nothing."""
    try:
        r = subprocess.run(["docker", "exec", FFMPEG_CTR, "pgrep", "-f", name],
                           capture_output=True, text=True, timeout=20)
        pids = [x for x in (r.stdout or "").split() if x.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None

def out_seconds(host_path):
    """Seconds of content actually in the converted file, read from the file
    itself. The alternative -- inferring the playhead from the SOURCE release's
    average bitrate -- is wrong by however much AC3-in-MPEG-TS differs from the
    original (~4.4% on the first film measured), and that error accumulates
    linearly: ~240s adrift after 90 minutes, enough to hold ffmpeg suspended
    while the player had already run dry."""
    try:
        r = subprocess.run(["docker", "exec", FFMPEG_CTR, FFPROBE, "-v", "error",
                            "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                            "%s/%s" % (TC_CTR, os.path.basename(host_path))],
                           capture_output=True, text=True, timeout=25)
        return float((r.stdout or "").strip())
    except Exception:
        return None

def regulate_lead(ih, host_path, bytes_per_sec, proc):
    """Keep the conversion roughly TC_LEAD seconds ahead of playback: suspend it
    when it gets further ahead than that, resume when the lead is spent. Gives a
    full cushion immediately, then costs almost no CPU."""
    if not bytes_per_sec or bytes_per_sec <= 0:
        return
    pid = _ctr_pid(os.path.basename(host_path))
    if not pid:
        return
    started = time.time()
    stopped = False
    measured = {"at": 0.0, "dur": None}
    try:
        while proc.poll() is None:
            # Re-measure the real converted duration periodically; between
            # measurements fall back to the bitrate estimate, which is fine over
            # a 10s window even though it drifts badly over an hour.
            now = time.time()
            if now - measured["at"] > 10:
                d = out_seconds(host_path)
                if d is not None:
                    measured.update(at=now, dur=d)
                else:
                    measured["at"] = now
                real = _ctr_stopped(pid)
                if real is not None and real != stopped:
                    print("transcode: %s externally %s — resyncing"
                          % (ih[:8], "suspended" if real else "resumed"), flush=True)
                    stopped = real
                    _tc_flag(ih, suspended=real)
            size = os.path.getsize(host_path) if os.path.exists(host_path) else 0
            if measured["dur"] is not None:
                # `started` predates the player launching by however long adb and
                # VLC take, so this under-states the lead slightly -- erring
                # toward keeping ffmpeg running, which is the safe direction.
                lead = (measured["dur"] + (now - measured["at"])) - (now - started)
            else:
                played = (now - started) * bytes_per_sec
                lead = (size - played) / bytes_per_sec
            if not stopped and lead > TC_LEAD:
                subprocess.run(["docker", "exec", FFMPEG_CTR, "kill", "-STOP", pid],
                               capture_output=True, timeout=15)
                stopped = True
                _tc_flag(ih, suspended=True)     # /audio/ must not read this as EOF
                print("transcode: suspend %s lead=%.0fs" % (ih[:8], lead), flush=True)
            elif stopped and lead < TC_LEAD * TC_BAND:
                subprocess.run(["docker", "exec", FFMPEG_CTR, "kill", "-CONT", pid],
                               capture_output=True, timeout=15)
                stopped = False
                _tc_flag(ih, suspended=False)
                print("transcode: resume  %s lead=%.0fs" % (ih[:8], lead), flush=True)
            time.sleep(2.0)
    except Exception:
        pass
    finally:
        if stopped:                       # never leave it suspended
            try:
                subprocess.run(["docker", "exec", FFMPEG_CTR, "kill", "-CONT", pid],
                               capture_output=True, timeout=15)
            except Exception:
                pass
        _tc_flag(ih, suspended=False)
        # Only when it really has exited -- bailing out of the loop on an
        # exception must not tear down a conversion the player is still reading.
        if proc.poll() is not None:
            transcode_end(ih, proc)

def transcode_start(key, cmd, name=None):
    with _tc_lock:
        old = _transcodes.pop(key, None)
        for k, v in list(_transcodes.items()):
            if v["proc"].poll() is not None:
                _transcodes.pop(k, None)    # already exited
        while len(_transcodes) >= MAX_TRANSCODES:
            k, v = next(iter(_transcodes.items()))
            _transcodes.pop(k, None)
            _stop(v)
        # ffmpeg runs with -loglevel error, and its stderr went to DEVNULL --
        # so every decode error, short read and mux warning it has ever reported
        # was discarded. Audio faults were undiagnosable as a direct result.
        log = None
        if name:
            try:
                log = open(os.path.join(TC_HOST, os.path.splitext(name)[0] + ".log"), "wb")
            except OSError:
                log = None
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=(log or subprocess.DEVNULL), bufsize=0)
        _transcodes[key] = {"proc": proc, "name": name, "at": time.time(), "log": log}
    # Outside _tc_lock, as transcode_stop_all() already documents: _stop() is
    # several docker execs plus up to 2s of polling, and transcode_alive() is
    # called from the /audio/ streaming loop, which would block behind it.
    # Unconditional, not only when the host-side client is alive: that client is
    # just the docker-exec front end and can exit while the container process
    # converts happily on (see _ctr_pid).
    if old:
        _stop(old)                          # same stream restarting
    return proc

def transcode_end(key, proc):
    """Drop a finished conversion from the registry. Called from regulate_lead
    once ffmpeg has exited on its own -- before this it was dead code, which is
    half of why nothing ever cleaned up after a film change."""
    with _tc_lock:
        ent = _transcodes.get(key)
        if isinstance(ent, dict) and ent.get("proc") is proc:
            _transcodes.pop(key, None)
        else:
            ent = None
    if ent:
        _stop(ent)

def _tc_flag(key, **kw):
    with _tc_lock:
        ent = _transcodes.get(key)
        if isinstance(ent, dict):
            ent.update(kw)

def transcode_suspended(key):
    """True while regulate_lead is deliberately holding ffmpeg with SIGSTOP.
    Suspended is its normal steady state, not a stall."""
    with _tc_lock:
        ent = _transcodes.get(key)
        return bool(ent and ent.get("suspended"))

def transcode_alive(key):
    with _tc_lock:
        ent = _transcodes.get(key)
    return bool(ent and ent["proc"].poll() is None)

_writing_cache = {}
def transcode_writing(ih, name):
    """True while ANYTHING is still writing this output.

    transcode_alive() only knows about the host-side Popen, which is the
    docker-exec front end and can exit while the container process converts on.
    Trusting it alone made the server declare a half-written file complete: it
    then sent a Content-Length of the partial size, the player stopped dead on
    it and reconnected at byte 0, and the film appeared to loop.

    The host-side check is the fast path; the container is only consulted when
    that says no, and at most every few seconds, because this is called from the
    /audio/ streaming loop.
    """
    if transcode_alive(ih):
        return True
    now = time.time()
    at, live = _writing_cache.get(name, (0.0, False))
    if now - at > 3:
        live = _ctr_pid(name) is not None
        _writing_cache[name] = (now, live)
    return live

def transcode_stop_all(keep=None):
    """Stop every conversion except `keep`.

    Switching films used to leave the previous film's ffmpeg converting the
    whole movie to completion for nobody: transcode_end() was never called,
    tv_stop() only touched the player, and MAX_TRANSCODES only noticed at
    overflow -- where it killed the docker-exec client and left the container
    process running regardless. Popped under the lock, killed outside it: each
    _stop() is a couple of docker execs and must not block transcode_alive().
    """
    with _tc_lock:
        doomed = [(k, v) for k, v in list(_transcodes.items()) if k != keep]
        for k, _ in doomed:
            _transcodes.pop(k, None)
        kept = _transcodes.get(keep) if keep is not None else None
        keep_name = kept.get("name") if isinstance(kept, dict) else None
    for k, ent in doomed:
        _stop(ent)
        print("transcode: stopped %s (superseded)" % k, flush=True)
    _kill_orphans(keep_name)
    # With the torrent cache emptied after every film, a converted file has
    # nothing to resume against, so it is not worth the gigabytes either.
    tc_cleanup(keep_name)

def job_get(mid):
    with _lock:
        return dict(_jobs.get(str(mid)) or {})

# One play job at a time. Two in parallel raced everything downstream: two
# buffers competing for the same constrained link, two ffmpegs writing a single
# output path, and -- the one that actually bit -- whichever job finished
# probing LAST would force-stop the other film and take the TV, minutes after
# the user had given up on it.
JOB_ACTIVE = ("starting", "buffering", "encoding", "launching")
JOB_STALE  = int(os.environ.get("JOB_STALE", 120))  # no update this long -> worker is gone
_play_gen  = 0        # bumped per accepted play; an older job stands down
# A play request has no job to cancel while it is still resolving streams --
# get_stream() can block for seconds before job_set() ever runs -- so a
# cancel landing in that window has to reach it some other way than
# active_job(). _cancel_gen counts accepted cancels; _play_inflight counts
# play requests currently between entry and job_set().
_cancel_gen = 0
_play_inflight = 0

# One browser session at a time, mirroring the one-play-job rule above. The
# token is minted by the server rather than the page: a token the page chose
# could be replayed from a bookmarked url, and this one is also the segment
# path, so it is the only thing standing between a stale tab and a live
# session's ffmpeg.
#
# _bx is also the SINGLE answer to "is a browser watching something right
# now" -- browser_playing(), claim_owner()'s browser rows, cache_watch()
# (via playing_now()) and /api/bx/beat's own match check all read it, and
# nothing else. That is deliberate: active_job()/_jobs cannot serve as that
# answer, because a job that has finished publishing is stage="playing",
# which is NOT in JOB_ACTIVE -- active_job() stops seeing it the moment
# playback actually starts, exactly when "is a browser watching" needs to
# start being true. publish() (in run_browser_job) is what populates this
# for EVERY mode, direct included, precisely so cache_watch() does not
# clear the cache out from under a direct-played film ~60s in for want of
# a live heartbeat that was never going to exist until the first
# /api/bx/beat arrived.
_bx = {"token": None, "job": None, "gen": 0, "at": 0.0, "state": "idle",
       "pos": 0.0, "dur": None, "title": None,
       # Packager fields, added alongside the existing heartbeat shape rather
       # than replacing it -- api/stop and the heartbeat route only know the
       # keys above, and must keep working unchanged.
       "dir": None, "seg": None, "anchor": None, "frontier": None,
       "seek_gen": 0, "pending_anchor": None, "timescales": None,
       # run_anchor is the REAL presentation time the current ffmpeg run
       # starts at -- the keyframe at or before anchor*seg, not anchor*seg
       # itself (see bx_spawn). Every segment file in the session directory
       # belongs to the current run, so this one number re-times all of
       # them at serve time.
       "run_anchor": 0.0,
       "n_segs": None, "proc_key": None, "src": None, "plan": None}
BX_IDLE = float(os.environ.get("BX_IDLE", 60))

def bx_begin(token, src_internal, plan, seg, duration, mid, gen):
    """Start a browser HLS session: make its directory, record the state the
    routes below read, and kick off the first ffmpeg at the front of the
    film. Returns True on success, False if the directory could not be made
    or the first ffmpeg failed to start -- either way there is nothing yet
    for the browser to fetch.
    """
    sess_dir = os.path.join(TC_HOST, BX_DIR + token)
    try:
        os.makedirs(sess_dir, exist_ok=True)
    except OSError:
        return False
    n_segs = math.ceil(duration / seg) if duration and seg else 0
    with _lock:
        _bx.update(token=token, job=mid, gen=gen, at=time.time(), state="starting",
                   pos=0.0, dur=duration,
                   dir=sess_dir, seg=seg, anchor=0, frontier=-1,
                   seek_gen=0, pending_anchor=None, timescales=None,
                   run_anchor=0.0,
                   n_segs=n_segs, proc_key="bx:" + token, src=src_internal, plan=plan)
    return bx_spawn(token, 0)

def bx_spawn(token, k0):
    """Start the ffmpeg for this session anchored at segment k0: the source
    is fed from k0*seg onward, so a seek to segment k0 is exactly a restart
    of ffmpeg at that offset.

    -ss before -i is an INPUT seek, and -copyts is never passed (see the
    init.mp4 comment above BX_DIR), so ffmpeg resets the seeked stream's own
    presentation clock to (near) zero at the seek point and counts up from
    there for as long as this process keeps running. -start_number k0 only
    renames the OUTPUT FILES this run writes -- s000100.m4s and so on -- onto
    their real place on the absolute grid; it does not tell ffmpeg to stamp
    an offset into the bytes it writes. So the files on disk are correctly
    named while everything inside them still starts its clock at zero. The
    server corrects that at serve time -- see the segment route below, and
    the reasoning next to its delta calculation.

    Registered through transcode_start with key "bx:"+token and name
    "bx_"+token, so MAX_TRANSCODES, _stop(), transcode_stop_all() and
    _kill_orphans() all manage this exactly like a TV transcode, with no
    special case anywhere in that machinery for what a browser session is.
    ffmpeg's own ff.m3u8 is written but never served -- the server hands out
    its own VOD playlist instead (see vod_playlist / the index.m3u8 route).
    """
    with _lock:
        if _bx["token"] != token:
            return False
        sess_dir, seg, src = _bx["dir"], _bx["seg"], _bx["src"]
        plan = _bx["plan"] or {}
    if not sess_dir or not seg or not src:
        return False
    name = BX_DIR + token
    aidx = int(plan.get("aidx") or 0)
    # Every segment file left in this directory was written by the run that
    # is being replaced, on a different anchor, so its bytes carry a
    # different zero point -- and the whole re-timing below rests on one
    # number covering every file present. Clearing them is what makes that
    # invariant true instead of merely likely: whatever survives here would
    # otherwise be served with the new run's anchor added to the old run's
    # clock. It also fixes the frontier, which reads file existence and
    # used to count a previous run's leftovers as this run's progress.
    bx_clear_segments(sess_dir)
    # Where this run's clock will actually start. -ss before -i is an input
    # seek and the video is copied, so ffmpeg cannot begin at k0*seg unless
    # a keyframe happens to sit exactly there; it begins at the last
    # keyframe at or before it and calls that moment zero. Probing for that
    # keyframe is the only way to know how far back "zero" really is, and
    # k0 == 0 is the one case that needs no probe -- the run starts at the
    # start of the film, so its zero is the film's zero.
    if k0 <= 0:
        run_anchor = 0.0
    else:
        run_anchor = browser_play.anchor_time(
            probe_keyframes(src, k0 * seg), k0, seg)
    # The argv itself is built in browser_play.segment_cmd -- a pure
    # function with no docker/host detail -- precisely so a test can assert
    # "-c:v copy" is unconditional and no video encoder ever sneaks in.
    # Only the docker-exec/ffmpeg-binary prefix belongs here.
    out_dir = "%s/%s" % (TC_CTR, name)
    playlist = "%s/ff.m3u8" % out_dir
    cmd = ["docker", "exec", FFMPEG_CTR, FFMPEG] + browser_play.segment_cmd(
        src, out_dir, playlist, k0, seg, plan, aidx)
    # Published BEFORE the process that will write the segments, not after.
    # The serve path re-times whatever is on disk by _bx["run_anchor"], so
    # any moment where ffmpeg is writing this run's segments while _bx still
    # names the previous run's anchor is a moment a segment can be served
    # with the wrong timeline -- and once it is in the player's buffer, that
    # is not something a later correction can take back.
    with _lock:
        if _bx["token"] != token:
            return False
        _bx.update(anchor=k0, frontier=k0 - 1, run_anchor=run_anchor)
    transcode_start("bx:" + token, cmd, name=name)
    with _lock:
        if _bx["token"] != token:
            return False   # the session moved on while ffmpeg was starting
    threading.Thread(target=regulate_hls, args=(token,), daemon=True).start()
    return True


def bx_clear_segments(sess_dir):
    """Delete this session's segment files, leaving init.mp4 alone.

    init.mp4 deliberately survives: it is identical for every anchor (that
    is exactly why -copyts is never passed -- see segment_cmd) and the
    browser fetched it once, at the top of the session, and will not fetch
    it again.
    """
    try:
        names = os.listdir(sess_dir)
    except OSError:
        return
    for fn in names:
        if fn.startswith("s") and fn.endswith(".m4s"):
            try:
                os.remove(os.path.join(sess_dir, fn))
            except OSError:
                pass

def bx_restart(token, k):
    """Reposition the packager to segment k. This IS how a scrub is served
    -- see the segment route below, the only caller, which already collapsed
    a burst of seek requests into this one call via seek_gen and
    BX_SEEK_DEBOUNCE before ever reaching here.
    """
    with _lock:
        if _bx["token"] != token:
            return False
        my_gen = _bx["seek_gen"]
    name = BX_DIR + token
    with _tc_lock:
        ent = _transcodes.get("bx:" + token)
    _kill_ctr(name)
    if isinstance(ent, dict):
        _stop(ent)
    with _lock:
        # _kill_ctr/_stop above is several blocking docker execs; if a newer
        # seek landed while the old ffmpeg was dying, that request owns the
        # restart now and this stale one must not spawn ffmpeg at the wrong
        # place -- exactly the abandoned-seek case seek_gen exists to catch.
        if _bx["token"] != token or _bx["seek_gen"] != my_gen:
            return False
    return bx_spawn(token, k)

def bx_stop_all(reason=None):
    """Stop the browser session's ffmpeg, reset _bx to idle, and remove the
    session directory. The counterpart to transcode_stop_all() for the
    packager. `reason` is only for the log line -- there is exactly one
    browser session at a time, so there is nothing to compare it against.
    """
    with _lock:
        token = _bx["token"]
        sess_dir = _bx["dir"]
        _bx.update(token=None, job=None, gen=0, at=0.0, state="idle",
                   pos=0.0, dur=None, title=None,
                   dir=None, seg=None, anchor=None, frontier=None,
                   seek_gen=0, pending_anchor=None, timescales=None,
                   run_anchor=0.0,
                   n_segs=None, proc_key=None, src=None, plan=None)
    if token:
        with _tc_lock:
            ent = _transcodes.pop("bx:" + token, None)
        _kill_ctr(BX_DIR + token)
        if isinstance(ent, dict):
            _stop(ent)
        print("bx: stopped %s (%s)" % (token, reason or "?"), flush=True)
    if sess_dir:
        shutil.rmtree(sess_dir, ignore_errors=True)

def bx_timescales(token):
    """{track_id: timescale} for this session, parsed from init.mp4 once.

    A bare fMP4 segment carries no moov, so the tick rate each track counts
    its tfdt in can only come from the init segment -- and it is the same
    for every run and every segment of the session, so it is read once and
    kept on _bx from then on.

    BUG this used to be a field nothing ever wrote. _bx carried a
    "timescales" key that bx_begin, bx_restart and bx_stop_all all set to
    None and no code path ever filled in, so the segment route's "no
    timescales yet" guard was permanently true and EVERY segment request
    came back 503. Only the tests populated it, by hand, which is exactly
    why nothing caught it. It is computed here, from the file, so there is
    no field left to forget to write.

    Returns None when init.mp4 is not on disk yet or will not parse. The
    caller must treat that as not-ready rather than assuming a timescale:
    guessing wrong stamps a time that looks entirely plausible and is not.
    """
    with _lock:
        if _bx["token"] != token:
            return None
        cached = _bx["timescales"]
        sess_dir = _bx["dir"]
    if cached:
        return cached
    if not sess_dir:
        return None
    try:
        with open(os.path.join(sess_dir, "init.mp4"), "rb") as f:
            data = f.read()
        found = browser_play.track_timescales(data)
    except Exception:
        return None
    if not found:
        return None
    with _lock:
        if _bx["token"] != token:
            return None
        _bx["timescales"] = found
    return found

def bx_frontier(token):
    """The highest complete segment index for this session, or anchor - 1 if
    none are complete yet. Complete means the file exists AND its successor
    exists too -- the HLS muxer only finalizes segment k the instant it opens
    k+1, so reading s%06d.m4s while it is still the file ffmpeg is writing
    hands back a truncated fragment. Returns None if the token no longer
    matches the live session.
    """
    with _lock:
        if _bx["token"] != token:
            return None
        sess_dir, anchor = _bx["dir"], _bx["anchor"]
    if not sess_dir or anchor is None:
        return None
    try:
        names = os.listdir(sess_dir)
    except OSError:
        return anchor - 1
    have = set()
    for n in names:
        m = re.match(r"^s(\d{6})\.m4s$", n)
        if m:
            have.add(int(m.group(1)))
    best = anchor - 1
    # >= anchor only: a segment left behind by a PREVIOUS anchor (before a
    # seek restarted ffmpeg) is not something this run is still producing,
    # and counting it would tell the pacer below the run is further along
    # than it really is.
    for k in have:
        if k >= anchor and (k + 1) in have and k > best:
            best = k
    return best

def _bx_trim(token, pos, frontier):
    """Delete segments the viewer is well behind. Bounded only below --
    never at or ahead of the frontier, which is the one region a rewind
    seek, or ffmpeg itself, might still need.
    """
    with _lock:
        if _bx["token"] != token:
            return
        sess_dir, seg = _bx["dir"], _bx["seg"]
    if not sess_dir or not seg:
        return
    try:
        names = os.listdir(sess_dir)
    except OSError:
        return
    for n in names:
        m = re.match(r"^s(\d{6})\.m4s$", n)
        if not m:
            continue
        k = int(m.group(1))
        if k >= frontier:
            continue
        if browser_play.seg_start(k, seg) < pos - BX_BEHIND:
            try:
                os.remove(os.path.join(sess_dir, n))
            except OSError:
                pass

def regulate_hls(token):
    """Pace the packager's ffmpeg the way regulate_lead paces the TV
    transcode -- SIGSTOP once it is comfortably ahead, SIGCONT once the lead
    has drained -- but against the browser's OWN reported position instead
    of an estimate. regulate_lead never had that: the TV pipe has no notion
    of "where the player actually is", only how many bytes it has produced
    and how long it has been running, so it measures a proxy and lives with
    the proxy's error. A browser session heartbeats its real currentTime
    every few seconds (_bx["pos"], _bx["at"]), so the lead here is measured
    against the number the player is actually showing, not guessed from a
    bitrate.
    """
    key = "bx:" + token
    name = BX_DIR + token
    pid = None
    # The caller starts this thread immediately after transcode_start, with
    # no wait loop first (unlike regulate_lead, whose caller already waited
    # for a head start to build) -- so the container process may not be
    # visible to pgrep yet. Give it a moment rather than bailing out cold.
    for _ in range(20):
        pid = _ctr_pid(name)
        if pid:
            break
        time.sleep(0.25)
    if not pid:
        return
    started = time.time()
    stopped = False
    try:
        while True:
            with _tc_lock:
                ent = _transcodes.get(key)
            proc = ent.get("proc") if isinstance(ent, dict) else None
            if proc is None or proc.poll() is not None:
                break
            with _lock:
                if _bx["token"] != token:
                    break   # session moved on; nothing left here to regulate
                seg, anchor, at, pos = _bx["seg"], _bx["anchor"], _bx["at"], _bx["pos"]
            frontier = bx_frontier(token)
            if frontier is None:
                break
            now = time.time()
            if at:
                playhead = pos
            else:
                # No heartbeat has arrived yet: assume real-time playback
                # from the anchor, the same wall-clock fallback regulate_lead
                # uses before it has a real measurement either.
                playhead = anchor * seg + (now - started)
            lead = browser_play.seg_start(frontier + 1, seg) - playhead
            real = _ctr_stopped(pid)
            if real is not None and real != stopped:
                print("bx: %s externally %s -- resyncing"
                      % (token[:8], "suspended" if real else "resumed"), flush=True)
                stopped = real
                _tc_flag(key, suspended=real)
            if not stopped and lead > BX_LEAD:
                subprocess.run(["docker", "exec", FFMPEG_CTR, "kill", "-STOP", pid],
                               capture_output=True, timeout=15)
                stopped = True
                _tc_flag(key, suspended=True)
                print("bx: suspend %s lead=%.0fs" % (token[:8], lead), flush=True)
            elif stopped and lead < BX_LEAD * TC_BAND:
                subprocess.run(["docker", "exec", FFMPEG_CTR, "kill", "-CONT", pid],
                               capture_output=True, timeout=15)
                stopped = False
                _tc_flag(key, suspended=False)
                print("bx: resume  %s lead=%.0fs" % (token[:8], lead), flush=True)
            _bx_trim(token, playhead, frontier)
            time.sleep(2.0)
    except Exception:
        pass
    finally:
        if stopped:                       # never leave it suspended
            try:
                subprocess.run(["docker", "exec", FFMPEG_CTR, "kill", "-CONT", pid],
                               capture_output=True, timeout=15)
            except Exception:
                pass
            _tc_flag(key, suspended=False)

def active_job():
    """(mid, job) of the play job still working, if any."""
    now = time.time()
    with _lock:
        for k, j in _jobs.items():
            if j.get("stage") in JOB_ACTIVE and now - j.get("at", 0) < JOB_STALE:
                return k, dict(j)
    return None, None

def play_claim():
    """Claim the TV. Anything older sees superseded() and stands down -- the
    belt to active_job()'s braces, for a job that went stale while still alive."""
    global _play_gen
    with _lock:
        _play_gen += 1
        return _play_gen

def superseded(gen):
    with _lock:
        return gen is not None and gen != _play_gen

def claim_owner(owner, token=None):
    """Decide whether `owner` ("tv" or "browser") may take the player now.

    | incoming | current holder                                        | result |
    |----------|--------------------------------------------------------|--------|
    | tv       | nothing, or a TV job                                    | accept |
    | tv       | a live browser session, or an active browser job         | refuse |
    |          | still preparing (buffering/encoding, no heartbeat yet)   |        |
    | browser  | nothing                                                 | accept |
    | browser  | an active TV job, or tv_playback_state() in (2, 3)      | refuse |
    | browser  | a browser session with the same token                   | accept |
    | browser  | a live browser session with a different token           | refuse |
    | browser  | another browser's job still preparing                   | refuse |

    Cross-device is refused rather than superseded on purpose: a TV play
    silently taking over a film someone is watching on a phone in another
    room is the exact "silently interrupt" failure this whole scheme exists
    to prevent, and POST /api/stop remains the unambiguous way to take the
    player back from another device.

    Returns (True, None) to accept, or (False, message) to refuse -- the
    message is always "Another device is playing".
    """
    if owner == "tv":
        if browser_playing():
            return False, "Another device is playing"
        # A browser job that is still preparing has no heartbeat yet -- the
        # page cannot send one until the media is ready and the player is
        # attached -- so browser_playing() reads False for the 30-120s a
        # candidate takes to buffer, probe and convert. Without this check a
        # TV request landing in that window would supersede a film someone
        # is already sitting and waiting for.
        amid, aj = active_job()
        if amid is not None and (aj or {}).get("owner") == "browser":
            return False, "Another device is playing"
        return True, None
    # owner == "browser"
    amid, aj = active_job()
    if amid is not None:
        aj = aj or {}
        if aj.get("owner") != "browser":
            return False, "Another device is playing"
        # The same blind spot the TV branch above guards against, and it
        # needs guarding in this direction too: a browser job that is still
        # preparing has no _bx session yet, so browser_playing() below reads
        # False for the whole 30-120s a candidate takes to buffer, and a
        # second browser landing in that window took the player off the
        # viewer who was already sitting waiting for it.
        #
        # The token is what separates "this same attempt asking again" from
        # a different browser: it is minted per attempt, and a page starting
        # a fresh attempt releases its old session first (POST /api/bx/stop),
        # which ends that job and takes it out of active_job().
        if token is None or aj.get("otoken") != token:
            return False, "Another device is playing"
    if tv_playback_state() in (2, 3):
        return False, "Another device is playing"
    if browser_playing():
        with _lock:
            same = token is not None and _bx["token"] == token
        if not same:
            return False, "Another device is playing"
    return True, None

def job_set(mid, **kw):
    with _lock:
        key = str(mid)
        is_new = key not in _jobs
        j = _jobs.setdefault(key, {})
        j.update(kw)
        j["at"] = time.time()
        if is_new:               # only worth scanning the whole dict on growth
            _evict(_jobs, TTL_JOB, MAX_JOB_ENTRIES)

def buffer_target(pick, runtime_min):
    """Bytes needed for BUFFER_SECS of playback, from the release's own bitrate."""
    gb = pick.get("gb") or 0
    total = gb * 1024 * 1024 * 1024
    secs = (runtime_min or 0) * 60
    if total > 0 and secs > 0:
        want = int(total / secs * BUFFER_SECS)
    else:
        want = BUFFER_MIN
    return max(BUFFER_MIN, min(BUFFER_MAX, want, int(total) if total else BUFFER_MAX))

def fetch_tail(url, mid, label):
    """Pull the last TAIL_MB so the player's index read is already cached.

    Returns (ok, size) -- size is the real byte count read off the head
    request's Content-Range header, or None if it could not be read. That size
    is the release's true file size, which a candidate's own listing can get
    wrong (packs especially), so callers use it to correct the estimate."""
    size = None
    try:
        head = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": UA})
        with urllib.request.urlopen(head, timeout=45) as r:
            cr = r.headers.get("Content-Range") or ""
        size = int(cr.split("/")[-1]) if "/" in cr else None
        if not size or size <= 0:
            return False, size
        start = max(0, size - TAIL_MB * 1048576)
        job_set(mid, msg="%s — fetching the seek index…" % label)
        req = urllib.request.Request(url, headers={"Range": "bytes=%d-%d" % (start, size - 1),
                                                   "User-Agent": UA})
        got = 0
        t0 = time.time()
        # DEAD_SECS + 10 to match probe_and_buffer: a swarm that never sends a
        # byte is capped by the socket timeout, so 180s here meant a truly dead
        # candidate cost three minutes before the fast-abandon logic even ran.
        with urllib.request.urlopen(req, timeout=DEAD_SECS + 10) as r:
            while True:
                # read1, not read: read() blocks until the whole 256 KB buffer is
                # full, so a swarm trickling steadily never returns to the loop
                # and the checks below never run -- which is the exact stall this
                # is meant to catch. read1 returns whatever has arrived.
                c = r.read1(262144)
                if not c:
                    break
                el = time.time() - t0
                # Same rules as the main probe loop, including checking BEFORE
                # folding this chunk in -- otherwise got is never 0 here.
                if got == 0 and el > DEAD_SECS:
                    return False, size
                if el > TAIL_SECS:
                    return False, size
                got += len(c)
                job_set(mid, msg="%s — seek index %d/%d MB" % (label, got // 1048576, (size - start) // 1048576))
        return got > 0, size
    except Exception:
        return False, size

def probe_and_buffer(mid, pick, runtime_min, attempt, total, gen=None):
    """
    Fill the cache for one candidate. Returns (ok, bytes_got, rate).
    Gives up early if the swarm is dead or hopelessly slow, so a bad pick costs
    ~30s instead of handing the player something that stalls forever.
    """
    url = stream_url(pick)
    target = buffer_target(pick, runtime_min)
    req = required_mbps(pick.get("gb"), runtime_min)
    need_bps = (req / 8.0) * 1048576 if req else 0
    label = "%s %s %.1fGB" % (pick.get("tag"), pick.get("codec"), pick.get("gb") or 0)
    job_set(mid, stage="buffering", got=0, target=target, pct=0, speed=0,
            attempt=attempt, attempts=total, candidate=label, url=url,
            req_mbps=round(req, 1) if req else None,
            msg="Trying %d/%d: %s — connecting…" % (attempt, total, label))
    # tail first: it is small, and the player blocks on it at open -- and its
    # Content-Range header carries the file's real size, which a candidate's
    # own listing can get wrong (packs especially). Recompute the buffering
    # targets from that real size before the main loop below runs.
    _, real_size = fetch_tail(url, mid, "Trying %d/%d: %s" % (attempt, total, label))
    if real_size:
        pick["gb"] = real_size / (1024.0 ** 3)
        target = buffer_target(pick, runtime_min)
        req = required_mbps(pick.get("gb"), runtime_min)
        need_bps = (req / 8.0) * 1048576 if req else 0
    t0 = time.time(); got = 0; grew = False
    try:
        r_h = urllib.request.Request(url, headers={"Range": f"bytes=0-{BUFFER_MAX-1}",
                                                   "User-Agent": UA})
        with urllib.request.urlopen(r_h, timeout=DEAD_SECS + 10) as r:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                if got == 0 and time.time() - t0 > DEAD_SECS:
                    # still nothing at all after DEAD_SECS -- checked BEFORE
                    # folding this chunk in, otherwise got is never 0 here
                    return False, 0, 0.0
                got += len(chunk)
                el = max(time.time() - t0, 0.001)
                rate = got / el
                if superseded(gen):
                    return False, got, rate          # a newer play owns the TV now
                if el > HARD_CAP_SECS:
                    return False, got, rate          # absolute ceiling: see HARD_CAP_SECS
                if need_bps and el > PROBE_SECS and rate < need_bps * SLOW_RATIO:
                    return False, got, rate          # too slow, try the next one
                if need_bps and rate < need_bps and not grew and target < BUFFER_MAX and got > target * 0.6:
                    target = min(BUFFER_MAX, int(target * 1.8)); grew = True
                job_set(mid, got=got, target=target, speed=rate,
                        pct=min(99, int(got * 100 / target)),
                        msg="Trying %d/%d: %s — %d/%d MB at %.1f Mbps%s" % (
                            attempt, total, label, got // 1048576, target // 1048576,
                            rate * 8 / 1048576,
                            "  (slow — deeper cushion)" if grew else ""))
                if got >= target:
                    el = max(time.time() - t0, 0.001)
                    record_rate(got / el, pick.get("seeders"))
                    return True, got, got / el
    except Exception:
        pass
    el = max(time.time() - t0, 0.001)
    return (got >= BUFFER_MIN), got, got / el

def launch(url, mid, pick, title, gen):
    """Hand the finished URL to the TV app.

    The app is the only player there is now. If it is not already in the
    foreground and adb is available, wake_app() brings it there; once it is
    fresh, the rest of this is the same handoff over the heartbeat channel
    either way.
    """
    app = app_fresh()
    if not app and ADB_ENABLED:
        ok, msg = wake_app()
        if not ok:
            return False, msg
        app = app_fresh()
    if app:
        job_set(mid, msg="Handing over to the TV app…")
        # Resume: the app seeks there itself once the stream is open. Only
        # present when the play route was given t=, so autoplay-next -- which
        # never sets one -- cannot carry the previous episode's offset.
        start_s = job_get(mid).get("start_s")
        seq = app_cmd("play", job=str(mid), url=url, title=title, pick=pick,
                      transcoded=bool(pick.get("transcoded")),
                      hifi=bool(_hifi["on"] and SENDSPIN_ENABLED),
                      hifi_centre=bool(_hifi["on"] and SENDSPIN_ENABLED and _hifi["film_centre"]),
                      **({"start_s": start_s} if start_s is not None else {}))
        print("app: play %s seq=%d -> %s" % (mid, seq, app["name"] or app["id"]),
              flush=True)
        deadline = time.time() + APP_HANDOFF_SECS
        while time.time() < deadline:
            if superseded(gen):
                # Take the order back if it is still ours and unacked, or an app
                # that reappears later plays the film this request abandoned.
                # A newer play has queued its own command by now, and the seq
                # guard means that one is left exactly where it is.
                app_cmd_clear(seq)
                return False, "superseded"       # a newer play owns the TV now
            a = app_fresh()
            # A heartbeat going stale in here is NOT a failure: the app stops
            # polling while it opens the stream, and a 4K file takes a while to
            # come up. Only the deadline ends this wait.
            #
            # Trust the state only once the app has acked THIS command. A replay
            # of the film already on screen would otherwise be reported "playing"
            # by the heartbeat that predates the order, before the app has even
            # been told to start over.
            if a and a["job"] == str(mid) and (a.get("acked") or 0) >= seq:
                if a["state"] in ("buffering", "playing", "paused"):
                    return True, "playing"
                if a["state"] == "error":
                    return False, a["err"] or "The TV app could not play it"
            time.sleep(0.25)
        # Same as the supersede above: nobody is waiting for this film any more.
        app_cmd_clear(seq)
        return False, "The TV app did not start playback"
    return False, "No TV app is connected, and the phone remote is off"

def prepare_candidate(mid, pick, runtime_min, i, total, gen, tried):
    """Buffer one candidate, probe it, and check its audio language.

    Steps 1-9 of what run_play_job does per candidate, lifted out so the
    browser worker can run exactly the same preparation rather than a
    plausible-looking copy of it. Returns None when this candidate is out --
    too slow, wrong language, or superseded -- and the caller moves to the
    next one. `tried` is appended to in place, because the caller builds its
    final "Tried: ..." message from it.
    """
    ok, got, rate = probe_and_buffer(mid, pick, runtime_min, i, total, gen)
    tried.append("%s %.1fGB @ %.1f Mbps" % (pick.get("tag"), pick.get("gb") or 0,
                                            rate * 8 / 1048576))
    if not ok:
        job_set(mid, msg="Candidate %d/%d too slow — trying the next…" % (i, total))
        return None
    # Probe the real stream, never the release name. "Shawshank
    # [2160p x265 10bit FS97 Joy]" carries no audio token at all and is
    # actually DTS 5.1, which this panel has no decoder for.
    if superseded(gen):
        return None
    job_set(mid, msg="Checking the audio track…")
    # BUG this used to build the torrent URL by hand, which assumed every
    # candidate is a torrent. A provider can hand back a direct HTTP source
    # instead (transport == "http"), and normalise_candidate() sets infoHash
    # to None for those, so the hand-built URL came out as ".../None" and
    # ffprobe had nothing to probe. On the TV that just degraded silently
    # (no codec, no language check, no AC-3 fix); on the browser it made
    # decide() see no video codec at all and skip every HTTP candidate, so a
    # source that needed no server work at all was reported as unplayable.
    # stream_url_internal() already knows how to build this URL for both
    # transports -- for a torrent it is the exact same string this line used
    # to build by hand, so use it instead of duplicating the logic.
    internal = stream_url_internal(pick)
    acodec, adur, alangs, acodecs = probe_media(internal)
    # probe_media can block for up to two minutes. Re-check before the
    # next statements, which stop every other conversion and start one:
    # a superseded worker reaching them would kill its successor's live
    # transcode and leave an orphan of its own.
    if superseded(gen):
        return None
    # The file is the one that actually knows. score()'s language check
    # only spares us from probing the obvious rejects -- a release name
    # with no flag on it can still turn out to be a foreign dub, and
    # this is where that is found out. Mirrors the "too slow" path: move
    # on to the next candidate rather than failing the film.
    if REJECT_LANG and alangs and not audio_has_lang(alangs, PREF_LANG):
        job_set(mid, msg="Candidate %d/%d has no %s audio — trying the next…"
                         % (i, total, LANG_NAME.get(PREF_LANG, PREF_LANG)))
        return None
    # transcode_begin keeps whichever track passed the language check,
    # not track 0, so needs_fix has to be judged on that same track.
    aidx = audio_track_for(alangs, PREF_LANG) if REJECT_LANG else 0
    if acodecs and aidx < len(acodecs) and acodecs[aidx]:
        acodec = acodecs[aidx]
    return {"internal": internal, "acodec": acodec, "adur": adur, "alangs": alangs,
            "acodecs": acodecs, "aidx": aidx, "got": got, "rate": rate}

def run_play_job(mid, picks, runtime_min, title=None, gen=None):
    """Work down the ranked candidates until one actually streams."""
    def stand_down():
        if superseded(gen):
            job_set(mid, stage="error", ok=False,
                    msg="Superseded by a newer play request")
            return True
        return False
    try:
        picks = [p for p in (picks or []) if p]
        total = min(len(picks), ATTEMPTS)
        tried = []
        for i, pick in enumerate(picks[:ATTEMPTS], start=1):
            if stand_down():
                return
            prep = prepare_candidate(mid, pick, runtime_min, i, total, gen, tried)
            if prep is None:
                if stand_down():
                    return
                continue
            internal = prep["internal"]
            acodec, adur = prep["acodec"], prep["adur"]
            alangs, acodecs, aidx = prep["alangs"], prep["acodecs"], prep["aidx"]
            got, rate = prep["got"], prep["rate"]
            fidx = pick.get("fileIdx")
            needs_fix = bool(AUDIO_FIX and acodec and acodec not in NATIVE_AUDIO)
            if _hifi["on"] and SENDSPIN_ENABLED:
                # hifi: the bridge/DAC plays the audio straight off the
                # source file, so the TV only ever shows picture -- the AC3
                # conversion this release would otherwise need never happens.
                print("hifi: skipping AC3 conversion, TV plays video only", flush=True)
                needs_fix = False
            pick["audio_actual"] = acodec or "?"
            pick["audio_langs"] = alangs
            pick["audio_track"] = aidx
            pick["transcoded"] = needs_fix
            with _lock:
                if _hifi["on"] and SENDSPIN_ENABLED:
                    # The first "playing" heartbeat starts the bridge stream,
                    # not this job -- it has the one clock (server monotonic)
                    # and the TV's own reported position to line the start up
                    # against. A new source retires the old timeline now, so
                    # no verdict is ever computed against the previous film.
                    _hifi["src"] = internal
                    _hifi["aidx"] = aidx
                    _hifi["film_centre"] = _hifi["centre"]
                    _hifi["job"] = str(mid)
                else:
                    _hifi["src"] = None
                    _hifi["job"] = None
                _hifi_invalidate()
                _hifi["fail_count"] = 0
                _hifi["last_error"] = None
                if _hifi["src"]:
                    # Decode the audio while the film pre-buffers, so it is
                    # sitting in the bridge's cache before the TV shows a frame.
                    _ss_q.put(("prepare", internal, aidx, _hifi["gen"]))
            # Committed to this candidate, so every earlier conversion is dead
            # weight and must go before we start ours. No keep= exception for the
            # same infoHash: transcode_begin unlinks the output and starts fresh
            # regardless, so "keeping" that one only meant a replay of the film
            # already playing left the old ffmpeg alive, writing to an inode that
            # had just been deleted, while a second one wrote the real file.
            transcode_stop_all()
            if needs_fix:
                # Prefer the catalogue's runtime, fall back to what ffprobe
                # just read off the container, and only then to a nominal
                # feature length. Zero is NOT a safe fallback: regulate_lead()
                # bails on a non-positive rate, which leaves ffmpeg running
                # flat out -- the ~21x case the transcode_begin comment says
                # pinned Stremio and corrupted the very audio this exists to fix.
                secs = (runtime_min or 0) * 60 or (adur or 0)
                if secs <= 0:
                    secs = 110 * 60
                    print("transcode: no runtime for %s, assuming %d min"
                          % (mid, secs // 60), flush=True)
                bps = ((pick.get("gb") or 0) * 1024 ** 3) / secs
                job_set(mid, msg="%s audio — converting to AC3 before starting…" % acodec)
                name = transcode_begin(tc_key(pick), fidx, internal, bps, mid, gen, aidx=aidx)
                if not name:
                    job_set(mid, msg="Audio conversion failed — playing as-is")
                    pick["transcoded"] = False
                    url = stream_url(pick)
                else:
                    url = audio_url(pick)
            else:
                url = stream_url(pick)
            job_set(mid, stage="launching", pct=100, url=url,
                    msg=("Starting the player — %s audio, converting to AC3…" % acodec)
                        if needs_fix else
                        ("Starting the player — %s audio, native…" % (acodec or "unknown")))
            if stand_down():
                return
            pok, pmsg = launch(url, mid, pick, title, gen)
            # launch() stands aside for a newer play rather than failing the
            # film; the worker's own stand-down writes the right message.
            if not pok and stand_down():
                return
            if not pok and pick.get("transcoded"):
                # launch() failing does not stop the transcode it was waiting
                # on; nothing else will either, so the swarm stays open.
                transcode_stop_all()
            if pok:
                # kind/season/episode/next let UIs show "Next: S01E02" -- kept
                # separate from the fields above, which apply to films too.
                kind, season, episode, next_ep = "movie", None, None, None
                if str(mid).startswith("tv:"):
                    kind = "tv"
                    try:
                        tid, season, episode = tv_job_parts(str(mid))
                        nxt = next_episode(tid, season, episode)
                        next_ep = "S%02dE%02d" % nxt if nxt else None
                    except Exception:
                        pass
                _now.update(title=title or "Unknown", tag=pick.get("tag"),
                            audio=pick.get("audio_actual") or "?",
                            transcoded=bool(pick.get("transcoded")),
                            gb=pick.get("gb"), at=time.time(),
                            kind=kind, season=season, episode=episode, next=next_ep,
                            # What the bridge decodes for this film, so a service
                            # restart mid-film (every server push is one) can
                            # pick the audio back up instead of leaving the TV
                            # silent until the film is started again.
                            hifi_src=internal if (_hifi["on"] and SENDSPIN_ENABLED) else None,
                            hifi_aidx=pick.get("audio_track") or 0,
                            hifi_centre=_hifi["film_centre"],
                            hifi_job=str(mid) if (_hifi["on"] and SENDSPIN_ENABLED) else None)
                _now_save(_now)
            job_set(mid, stage="playing" if pok else "error", ok=pok, pick=pick,
                    msg=("Playing — %s %.1fGB, buffered %d MB at %.1f Mbps%s" % (
                            pick.get("tag"), pick.get("gb") or 0, got // 1048576,
                            rate * 8 / 1048576,
                            ("  (after %d dud%s)" % (i - 1, "" if i == 2 else "s")) if i > 1 else ""))
                         if pok else pmsg)
            return
        job_set(mid, stage="error", ok=False,
                msg="No candidate could stream. Tried: " + "; ".join(tried))
    except Exception as ex:
        # Without this, an uncaught error here (subprocess.TimeoutExpired from
        # adb() is realistic) kills the thread silently -- the job is stuck at
        # its last stage forever and the phone polls it with no way out.
        job_set(mid, stage="error", ok=False, msg=f"{type(ex).__name__}: {ex}")

def candidate_key(c):
    """The stable identity of one candidate: its own release key, falling
    back to its infoHash and then its url. This is the ONLY thing the
    skip contract (the browser retrying with {"skip": [...]}) may compare
    against -- job ids and list positions both change between a page's
    retries, so either would silently skip the wrong candidate, or none
    at all, the moment the ranking reorders.
    """
    return c.get("key") or c.get("infoHash") or c.get("url")

def browser_picks(entry, caps):
    """Candidates ordered so the ones a browser can actually decode are
    tried first, without changing WHICH candidates exist or their
    relative order within either group -- score() and best_stream()
    already decided that, and the TV's own ranking must not move because
    a browser asked.

    entry.get("picks_all") -- the full 25-deep relaxed tail -- is
    preferred over entry.get("picks") (the TV's top 5 by score()): a
    browser is commonly the one device that can decode plain H.264 that
    the TV's HEVC-first ranking pushed past the cut, and there is no
    reason to make it start from a shorter list than the TV does.

    This is only a pre-filter on the release's OWN metadata -- a
    candidate's "codec" field, as contract.normalise_candidate() sets it
    ("HEVC", "H264", "AV1" or "?" for unknown) -- cheap, and known before
    anything is probed. The real decision is browser_play.decide(), after
    prepare_candidate() and probe_full() run on the file itself; this
    only picks a better order to try candidates in. A "?" candidate is
    left exactly where it was rather than pushed to the back: an unknown
    codec is common for a perfectly playable file (plenty of providers
    just never report one), and there is nothing here that says it can't
    play -- only decide() finds that out.
    """
    picks = list(entry.get("picks_all") or entry.get("picks") or [])
    types = (caps or {}).get("types") or {}
    codec_name = {"HEVC": "hevc", "H264": "h264", "AV1": "av1"}
    def supported(c):
        name = codec_name.get(c.get("codec"))
        if name is None:
            return True    # "?" or missing -- possibly playable, not demoted
        ok, _cap_level = browser_play.video_supported(types, {"codec_name": name})
        return ok
    # sorted() is stable, so this only ever moves the unsupported candidates
    # to the back -- the relative order within "supported" and within
    # "unsupported" stays exactly best_stream()'s own order.
    return sorted(picks, key=lambda c: not supported(c))

def bx_verify_url(url_path):
    """A cheap sanity check on a "direct" URL before it is handed to the
    browser: a 1-byte Range request to this same process, over loopback.

    Without this, a candidate that decide() approved for direct playback
    but whose stream_url_public() target 404s (a stale /src/ key, a
    torrent route that never actually resolved) would only be discovered
    by the <video> element itself, minutes into what looked like a
    successful play. Loopback rather than PUBLIC_HOST: this runs on the
    same box that will serve the real request and must not depend on the
    browser's own network path -- a tailnet address or a VPN route this
    process cannot exercise from inside a docker-exec -- to prove it.
    """
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (PORT, url_path),
            headers={"Range": "bytes=0-0", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= getattr(r, "status", 200) < 400
    except urllib.error.HTTPError as ex:
        return 200 <= ex.code < 400
    except Exception:
        return False

def publish(mid, token, pick, media, gen, title):
    """Tell the job -- and so the browser polling /api/progress -- that
    this candidate is ready, AND register the session in _bx, for every
    mode. _bx is the one thing browser_playing()/claim_owner()/
    cache_watch() trust to know "is a browser watching something"; a
    direct play never calls bx_begin(), so without this line _bx stayed at
    its idle defaults for the whole film -- cache_watch() saw
    playing_now() as False, cleared the cache out from under a torrent
    source about a minute in, and claim_owner("tv") let a second device
    silently take the player over mid-film. See the comment on _bx's own
    definition.

    Ordering with bx_begin(): for "remux"/"audio", bx_begin() has already
    run by the time run_browser_job calls this (see there) and has
    already populated the packager-specific fields -- dir, seg, anchor,
    frontier, run_anchor, n_segs, proc_key, src, plan -- under this same
    token/gen (timescales is the exception: it is filled in lazily by
    bx_timescales(), from an init.mp4 ffmpeg has not written yet at this
    point). This only touches token/job/gen/state/at/dur/title, so it
    cannot clobber those; it only advances state from bx_begin's
    "starting" to "playing" and stamps a fresh heartbeat time. For
    "direct", bx_begin() never runs, _bx is otherwise still at its idle
    defaults, and this is the only thing that will ever populate it.

    `at` is seeded to now rather than left for the first /api/bx/beat:
    the page cannot send a beat until the media is ready and the player
    is attached, and leaving `at` at 0 (or stale) for that gap would open
    the exact same "browser_playing() reads False while a viewer is
    already watching" hole that claim_owner()'s own preparation-window
    comment describes for the buffering phase -- BX_IDLE-wide, here.
    """
    with _lock:
        _bx.update(token=token, job=mid, gen=gen, state="playing",
                   at=time.time(), dur=media.get("duration"), title=title)
    job_set(mid, stage="playing", ok=True, pct=100, owner="browser",
            otoken=token, media=media, pick=pick, msg="Ready to play")

def bx_next_episode(mid):
    """The episode after this browser job's, for the page to autoplay, or
    None. {"id", "s", "e", "label"}.

    Computed HERE, during preparation, rather than when the film ends: it
    costs provider calls, and the moment playback ends is the moment the
    viewer is waiting to see something happen.

    Why the browser is told rather than driven: the TV path fires autoplay
    from app_heartbeat, because the TV app keeps reporting state to the
    server and the server can simply start the next episode itself. A
    browser has no such channel -- the server does not know the film ended
    until the page says so, and the page may by then be closed, hidden, or
    on a phone that has gone to sleep. So the server offers the next
    episode and the page decides, which also means a viewer who does not
    want it is one button away from not getting it.

    Returns None for a film, when AUTOPLAY_NEXT is off, and at the end of a
    show -- next_episode() already swallows provider errors into None, and
    a failed lookup here must cost nothing more than autoplay not firing.
    """
    if not AUTOPLAY_NEXT or not str(mid).startswith("tv:"):
        return None
    try:
        tid, s, e = tv_job_parts(str(mid))
        nxt = next_episode(tid, s, e)
    except Exception:
        return None
    if not nxt:
        return None
    ns, ne = nxt
    return {"id": tid, "s": ns, "e": ne, "label": "S%02dE%02d" % (ns, ne)}

def _bx_track_codec(probe, aidx):
    # probe["audio"] is whatever ffprobe found; aidx is prep["aidx"], an
    # index into that SAME list (both probe_full() calls share the one
    # ffprobe call shape) -- but a file with fewer audio tracks than
    # expected is not impossible, so this is bounds-checked rather than
    # trusted.
    try:
        return probe["audio"][aidx].get("codec_name")
    except (IndexError, TypeError):
        return None

def run_browser_job(mid, picks, runtime_min, title, gen, token, caps, skip=None):
    """The browser's sibling of run_play_job: walk the ranked candidates,
    ask browser_play.decide() how (or whether) THIS browser can play each
    one, and publish the first that works.

    Deliberately never touches _hifi, _ss_q, app_cmd, app_fresh, wake_app
    or adb() -- see the requirement-3 guard test in test_bplay.py. Browser
    playback has no TV app to hand off to and no Sendspin bridge decoding
    for it; the browser IS the player, reading straight off /src/, /t/ or
    /hls/. Reaching into any of that machinery here would mean a film
    someone is watching on their phone silently starts hifi audio, or
    issues a command to a TV nobody asked to involve -- exactly the
    cross-device interference claim_owner() exists to prevent.
    """
    def stand_down():
        if superseded(gen):
            job_set(mid, stage="error", ok=False,
                    msg="Superseded by a newer play request")
            return True
        return False
    skip = set(skip or [])
    try:
        # Skipped BEFORE the ATTEMPTS cut, not inside the loop: a Retry after
        # a round where all five failed skips exactly those five, and skipping
        # them within the first five left nothing to try while picks_all still
        # held twenty more.
        picks = [p for p in (picks or []) if p]
        # tried_keys is what the page sends back as its next skip, so it
        # carries the earlier rounds too -- or a second Retry would bring the
        # first five back.
        tried_keys = [candidate_key(p) for p in picks if candidate_key(p) in skip]
        picks = [p for p in picks if candidate_key(p) not in skip]
        total = min(len(picks), ATTEMPTS)
        reasons = []
        for i, pick in enumerate(picks[:ATTEMPTS], start=1):
            if stand_down():
                return
            key = candidate_key(pick)
            tried = []
            prep = prepare_candidate(mid, pick, runtime_min, i, total, gen, tried)
            tried_keys.append(key)
            job_set(mid, tried_keys=tried_keys)
            if prep is None:
                if stand_down():
                    return
                reasons.append("%s: %s" % (pick.get("codec") or "?",
                               tried[-1] if tried else "could not be prepared"))
                continue
            probe = probe_full(prep["internal"])
            plan = browser_play.decide(caps, {
                "format_name": probe["format_name"], "duration": probe["duration"],
                "video": probe["video"], "audio": probe["audio"], "aidx": prep["aidx"],
            })
            if plan["mode"] == "skip":
                reasons.append("%s: %s" % (pick.get("codec") or "?", plan["reason"]))
                job_set(mid, msg="Candidate %d/%d: %s — trying the next…"
                                 % (i, total, plan["reason"]))
                continue
            if stand_down():
                return
            if plan["mode"] == "direct":
                url = stream_url_public(pick)
                if not bx_verify_url(url):
                    reasons.append("%s: the direct url did not check out"
                                   % (pick.get("codec") or "?"))
                    job_set(mid, msg="Candidate %d/%d could not be verified — "
                                     "trying the next…" % (i, total))
                    continue
                media = {"kind": "direct", "url": url, "duration": probe["duration"],
                         "mode": "direct",
                         "video": (probe["video"] or {}).get("codec_name"),
                         "audio": _bx_track_codec(probe, prep["aidx"]), "seg": None,
                         "next": bx_next_episode(mid)}
                # The check above is separated from here by a network round
                # trip (bx_verify_url) and a provider lookup
                # (bx_next_episode), and a Stop or a newer play can land
                # inside either. Publishing after that puts a cancelled film
                # back on screen AND back into _bx, where browser_playing()
                # then holds the player for a viewer who has gone -- so the
                # last word has to be here, immediately before publish().
                if stand_down():
                    return
                publish(mid, token, pick, media, gen, title)
                return
            # "remux" or "audio" -- both need the packager running, and
            # differ only in whether ffmpeg re-encodes the audio track
            # (decide() already put that choice in plan["acodec"]).
            duration = probe["duration"] or (runtime_min or 0) * 60
            gop = probe_gop(prep["internal"])
            seg, n_segs = browser_play.grid(duration, gop)
            if not bx_begin(token, prep["internal"], plan, seg, duration, mid, gen):
                reasons.append("%s: the packager could not start"
                               % (pick.get("codec") or "?"))
                job_set(mid, msg="Candidate %d/%d could not start — trying "
                                 "the next…" % (i, total))
                continue
            media = {"kind": "hls", "url": "/hls/%s/index.m3u8" % token,
                     "duration": duration, "mode": plan["mode"],
                     "video": (probe["video"] or {}).get("codec_name"),
                     "audio": _bx_track_codec(probe, prep["aidx"]), "seg": seg,
                     "next": bx_next_episode(mid)}
            # The same last word as the direct branch, except this one has
            # already started ffmpeg: the packager has to go with it, or it
            # keeps writing segments for a session nothing will ever publish.
            # Guarded on the token so the session that superseded this one --
            # if it has already begun its own -- is never what gets stopped.
            if stand_down():
                with _lock:
                    mine = _bx["token"] == token
                if mine:
                    bx_stop_all("superseded")
                return
            publish(mid, token, pick, media, gen, title)
            return
        job_set(mid, stage="error", ok=False, tried_keys=tried_keys,
                msg="No candidate could stream. " + (
                    "; ".join(reasons) if reasons else "Nothing left to try."))
    except Exception as ex:
        # Same guard run_play_job has: an uncaught error here must not
        # leave the job -- and the page's poll loop -- stuck forever.
        job_set(mid, stage="error", ok=False, msg=f"{type(ex).__name__}: {ex}")

def providers_health():
    """Per-role provider status for /api/health, which the phone polls every
    15s -- this must stay a read: nothing here contacts a provider, only the
    registry state the gateway already tracks.

    "No providers configured" is a SETUP state, not a failure: a fresh
    install with nothing installed yet must still get a 200 here, the same
    as a fully configured one, never something that reads as unhealthy.
    """
    state = gateway.setup_required()
    roles = {role: {"provider": (state.get("roles") or {}).get(role),
                     "ready": gateway.available(role)}
             for role in contract.ROLES}
    return {"configured": bool(state.get("configured")), "roles": roles,
            "message": state.get("message") or ""}

# ---- provider admin ----------------------------------------------------------
# Everything here reaches the registry/store ONLY through providers/gateway.py
# -- this file must never import registry.py, store.py or addon.py directly
# (see gateway.py's module docstring). Unauthenticated except GET
# /api/setup/state (safe by construction) and the two routes that establish a
# session in the first place (claim, login); everything else goes through
# H._require_admin().
ADMIN_COOKIE = "cinematica_admin"
CSRF_HEADER = "X-Cinematica-CSRF"
# A package upload is the one POST body allowed past do_POST's small JSON
# cap -- generous enough for a real provider package (a few .py files, at
# most a small data file) without letting an upload exhaust memory here.
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_UNPACKED_BYTES = 96 * 1024 * 1024
MAX_PACKAGE_MEMBERS = 4000

# contract.ProviderError.code -> HTTP status. Never 500: an add-on or a
# package misbehaving is an ordinary, expected outcome of "an admin just
# pasted a URL or a file", not a bug in this process.
PROVIDER_ERROR_STATUS = {
    contract.E_CONFIG: 400, contract.E_AUTH: 401, contract.E_RATE: 429,
    contract.E_UPSTREAM: 502, contract.E_NOTFOUND: 404, contract.E_UNSUPPORTED: 400,
    contract.E_TIMEOUT: 504, contract.E_CRASH: 502, contract.E_PROTOCOL: 502,
    contract.E_INTERNAL: 500,
}


def _safe_extract_tar(data, dest_dir):
    """Extract a provider package's tar.gz into dest_dir, or raise ValueError
    with a message safe to show the admin who uploaded it.

    Every member is checked before ANY file is written: no absolute path, no
    ".." component, no symlink/hardlink (either could point outside dest_dir
    in a way a name check alone would miss), and a running total that never
    lets a small download unpack into an unbounded amount of disk.
    """
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as ex:
        raise ValueError("not a valid tar.gz package: %s" % ex)
    root = os.path.realpath(dest_dir)
    with tf:
        members = tf.getmembers()
        if len(members) > MAX_PACKAGE_MEMBERS:
            raise ValueError("package has too many files")
        total = 0
        safe = []
        for m in members:
            name = m.name or ""
            norm = os.path.normpath(name)
            if (not name or name.startswith("/") or name.startswith("\\")
                    or os.path.isabs(norm) or norm == ".." or norm.split(os.sep)[0] == ".."):
                raise ValueError("archive member %r escapes the package directory" % name[:200])
            if m.issym() or m.islnk():
                raise ValueError("archive member %r is a link, which is not allowed" % name[:200])
            if not (m.isfile() or m.isdir()):
                raise ValueError("archive member %r is not a regular file" % name[:200])
            dest_path = os.path.realpath(os.path.join(dest_dir, norm))
            if dest_path != root and not dest_path.startswith(root + os.sep):
                raise ValueError("archive member %r escapes the package directory" % name[:200])
            total += max(0, m.size)
            if total > MAX_PACKAGE_UNPACKED_BYTES:
                raise ValueError("package is too large uncompressed")
            safe.append(m)
        try:
            tf.extractall(dest_dir, members=safe, filter="data")
        except TypeError:
            # Python < 3.12 has no extraction filter -- every member was
            # already vetted by name/type above, so a plain extractall is safe.
            tf.extractall(dest_dir, members=safe)


def _read_package_manifest(package_dir):
    """manifest.json from an extracted package, validated, with its declared
    entry file confirmed present -- a package that cannot be described, or
    whose entry is missing, is never installed."""
    path = os.path.join(package_dir, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ValueError("package has no manifest.json")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ValueError("package manifest.json is not valid JSON: %s" % ex)
    manifest = contract.validate_manifest(raw)
    root = os.path.realpath(package_dir)
    entry_path = os.path.realpath(os.path.join(package_dir, manifest["entry"]))
    if entry_path != root and not entry_path.startswith(root + os.sep):
        raise ValueError("manifest entry escapes the package directory")
    if not os.path.isfile(entry_path):
        raise ValueError("package is missing its declared entry file %r" % manifest["entry"])
    return manifest

# ---- HTTP -------------------------------------------------------------------
def start_play(jobid, resolve, autoplay=False, owner="tv", token=None,
                worker=run_play_job, start_s=None):
    """Shared tail of every play route -- a film, an episode and autoplay all
    land here. resolve() returns (entry, runtime) from get_stream() or
    get_stream_tv(); it is called inside the cancel guard because it can run
    for seconds with no job registered yet, so a cancel arriving in that
    window has nothing in active_job() to act on. Snapshot _cancel_gen first
    and recheck it before the job is allowed to actually start. `owner`
    defaults to "tv" so every existing caller is unaffected; a browser caller
    passes owner="browser" and its session token. `worker` is the function
    run on the background thread once the job is accepted -- it defaults to
    run_play_job (the TV path); a /api/bplay/... route passes a closure that
    calls run_browser_job instead, with the extra token/caps/skip it needs
    already bound in. Returns (status_code, body) for the caller to send
    straight through."""
    global _play_inflight, _cancel_gen
    with _lock:
        c0 = _cancel_gen
        _play_inflight += 1
    try:
        entry, runtime = resolve()
        if not entry["pick"]:
            return 409, {"ok": False, "msg": entry["err"] or "no stream"}
        with _lock:
            if _cancel_gen != c0:
                return 409, {"ok": False, "msg": "Cancelled"}
        # Only worth checking a route to the TV that is actually going
        # to be used. A connected app needs none of this, and with the
        # phone remote off there is nothing to check -- but then there is
        # also no way to play anything, and the phone should be told why
        # rather than watching a job fail a minute later. A browser owner
        # has no TV app to reach at all, so this whole check is TV-only.
        if owner == "tv" and not app_fresh():
            if not ADB_ENABLED:
                return 502, {"ok": False,
                    "msg": "No TV app is connected, and the phone remote is off"}
            st = adb_state()
            if st != "device":
                ok, msg = adb_ready()
                if not ok:
                    return 502, {"ok": False, "msg": msg}
        url = stream_url(entry["pick"])
        title = entry.get("title")
        with _lock:
            if _cancel_gen != c0:
                return 409, {"ok": False, "msg": "Cancelled"}
        # A stream that resolved fine is still not this owner's to take if
        # another device already has the player -- see claim_owner()'s table.
        # Refusing here, before anything is touched, keeps a losing request
        # from stomping on a film someone else is already watching.
        ok, msg = claim_owner(owner, token)
        if not ok:
            return 409, {"ok": False, "msg": msg}
        # Only now that the new request is known to be viable -- it has a
        # stream and the TV answered -- is the old one stood down. Doing
        # it earlier meant an unplayable pick or an unreachable TV marked
        # the running job as failed while its worker, never actually
        # superseded, carried on and launched its film anyway.
        amid, aj = active_job()
        if amid is not None and amid != jobid:
            job_set(amid, stage="error", ok=False,
                    msg="Superseded by a newer play request")
        gen = play_claim()
        kw = dict(stage="starting", got=0, target=0, pct=0,
                  msg="Resolving streams…", ok=None, url=url,
                  title=title, gen=gen, autoplay=autoplay, owner=owner)
        if token is not None:
            kw["otoken"] = token
        # Carried on the job, the way autoplay is: launch() puts it in the play
        # command for the TV, and shelf_note() uses it to tell a position
        # reported before the seek from a real one. Written even when it is
        # None, because job_set MERGES: a job id is reused by every later play
        # of the same title, and leaving the key alone would have a replay --
        # or an autoplay-next -- inherit the offset the previous play was
        # given and seek to the middle of a film nobody asked to resume.
        kw["start_s"] = start_s
        job_set(jobid, **kw)
        threading.Thread(target=worker,
                         args=(jobid, entry.get("picks") or [entry["pick"]], runtime,
                               title, gen),
                         daemon=True).start()
        # After the worker is away: _shelf_begin() can reach the metadata
        # provider for a series, and nothing about remembering where a film
        # got to is worth delaying the film itself for. Still well ahead of
        # the first heartbeat for this job -- the TV has not been handed a
        # URL yet.
        job_set(jobid, **_shelf_begin(jobid, entry, runtime))
        return 202, {"ok": True, "msg": "buffering", "url": url,
                     "pick": entry["pick"], "job": jobid}
    finally:
        with _lock:
            _play_inflight -= 1

# ---- channels ----------------------------------------------------------------
# Followed channels, their uploads, and a hand-off to an external app -- see
# providers/contract.py's channels role. Nothing here buffers or proxies a
# video: play() only records that it was opened and returns the provider's
# {url, package, label} for the TV to hand to another app.

# Both keyed by gateway.cache_tag(ROLE_CHANNELS), same convention as _genres
# above -- a provider swap or a config edit can never keep serving results
# gathered under the old one.
_channel_popular_cache = {"at": 0, "data": [], "tag": None}
_channel_details_cache = {}   # "<tag>@<id>" -> {"at":ts, "channel":{...}}, 1h TTL


def _channel_item(ch, cid=None):
    """The wall/detail tile shape both clients render, from either a
    normalised gateway channel (ch carries "id") or a stored shelf snap (cid
    given separately, since a snap has no id of its own). Decorated with
    this household's own state -- followed/new -- via shelf.channel_view,
    never from anything the provider said."""
    cid = cid or ch.get("id")
    view = shelf.channel_view(cid)
    return {
        "id": cid,
        "kind": "channel",
        "title": ch.get("title"),
        "poster": ch.get("avatar"),
        "backdrop": ch.get("banner"),
        "overview": ch.get("description"),
        "subscribers": ch.get("subscribers"),
        "latest_at": ch.get("latest_at"),
        "followed": view["followed"],
        "new": view["new"],
    }


def channel_popular(limit=40, seeds=()):
    """Cached 6h, same TTL as the catalogue's own popular list, and keyed on
    the followed channels (`seeds`) as well as the provider: a provider may
    suggest channels like the ones followed, so following one more must not
    wait six hours to count. Errors are never cached -- an empty result here
    is what a not-yet-usable index looks like, and caching that would keep
    the wall's Popular row empty for 6 hours after the provider recovers."""
    seeds = sorted(seeds)
    tag = "%s|%s" % (gateway.cache_tag(contract.ROLE_CHANNELS), ",".join(seeds))
    with _lock:
        c = _channel_popular_cache
        if c["data"] and c.get("tag") == tag and time.time() - c["at"] < TTL_LIST:
            return c["data"]
    try:
        items = gateway.channel_popular(limit, seeds=seeds)["items"]
    except contract.ProviderError:
        return []
    with _lock:
        _channel_popular_cache.update(at=time.time(), data=items, tag=tag)
    return items


def channel_details_cached(cid):
    """channel_details(), cached 1h -- for a channel NOT followed (a
    followed one is served from its stored shelf snap instead, refreshed by
    channel_watch()). Errors propagate; there is no stale copy worth
    swallowing an error for."""
    tag = "%s@%s" % (gateway.cache_tag(contract.ROLE_CHANNELS), cid)
    with _lock:
        e = _channel_details_cache.get(tag)
        if e and time.time() - e["at"] < 3600:
            return e["channel"]
    ch = gateway.channel_details(cid)
    with _lock:
        _channel_details_cache[tag] = {"at": time.time(), "channel": ch}
    return ch


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _proxy_upstream(self, url, headers=None):
        """Stream any upstream through this server, Range and framing intact.

        Split out of _proxy_source so the torrent route below can reuse it. The
        framing in particular is not optional anywhere: the local streaming
        server answers some requests chunked too, and a body that goes out under
        keep-alive with no declared length and no terminator makes the player
        read the whole film and then block forever on a connection that will
        never say anything else.
        """
        h = dict(headers or {})
        h.setdefault("User-Agent", UA)
        h["Accept-Encoding"] = "identity"
        rng = self.headers.get("Range")
        if rng:
            h["Range"] = rng
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url, headers=h), timeout=30)
        except urllib.error.HTTPError as e:
            # The upstream's own answer, not ours -- but its body may echo a
            # signed URL back, so it is not forwarded.
            return self._send(e.code if 400 <= e.code < 600 else 502,
                              {"err": "source refused the request"})
        except Exception as e:
            # redact: the message can contain the URL, and the URL can BE the
            # credential (a signed query string). Only the generic shapes
            # (URL userinfo, query secrets, Bearer, JWT) are caught here --
            # the literal-secret pass lives behind the registry, which
            # server.py never imports directly; gateway.py is the seam for
            # that, and this path is a plain network error, not a call
            # through it.
            return self._send(502, {"err": contract.redact(
                "%s: %s" % (type(e).__name__, e), gateway.secret_values())})
        try:
            status = getattr(r, "status", 200) or 200
            self.send_response(status)
            for name in ("Content-Type", "Content-Length", "Content-Range",
                         "Accept-Ranges", "Last-Modified", "ETag"):
                v = r.headers.get(name)
                if v:
                    self.send_header(name, v)
            if not r.headers.get("Accept-Ranges"):
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")

            # How the player is to know the body has ended.
            #
            # Forwarding the headers above is not enough on its own. When the
            # upstream answers with Transfer-Encoding: chunked, http.client
            # has already consumed that framing by the time `r` reaches here,
            # and it does not invent a Content-Length it was never sent -- so
            # the loop above copies no length header, and this response goes
            # out under HTTP/1.1 keep-alive with no declared length and no
            # terminator. The player reads every byte of the film and then
            # blocks forever on a connection that is never going to say
            # anything else: the hang at completion.
            #
            # So supply framing of our own. Chunked for an HTTP/1.1 client --
            # the zero-length chunk is a positive end-of-body, so a truncated
            # transfer is still distinguishable from a complete one, and the
            # connection survives for the next range request. For anything
            # older, where chunked is not available, hang up instead and let
            # EOF do it.
            # 204 and 304 carry no body at all, by definition; framing one
            # would describe a body that is never sent and hang the player on
            # the very response that was supposed to be cheap.
            chunked = False
            if r.headers.get("Content-Length") is None and status not in (204, 304):
                if self.request_version >= "HTTP/1.1":
                    chunked = True
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.close_connection = True
                    self.send_header("Connection", "close")
            self.end_headers()

            if not chunked:
                shutil.copyfileobj(r, self.wfile, 256 * 1024)
            else:
                try:
                    while True:
                        buf = r.read(256 * 1024)
                        if not buf:
                            break
                        self.wfile.write(b"%x\r\n" % len(buf))
                        self.wfile.write(buf)
                        self.wfile.write(b"\r\n")
                except BaseException:
                    # Stopping mid-message leaves the chunked stream without
                    # its terminator, and the next response parsed off this
                    # connection would be read as a continuation of this one.
                    # Never reuse it.
                    self.close_connection = True
                    raise
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            # The player seeked or stopped. Routine, not an error.
            pass
        finally:
            try: r.close()
            except Exception: pass

    def _proxy_source(self, key):
        """A direct source, with its credentials added here and nowhere else."""
        if not contract.RE_HASH40.match(key or ""):
            return self._send(400, {"err": "bad source key"})
        src = source_for(key)
        if not src:
            return self._send(404, {"err": "unknown source"})
        h = dict(src["headers"])
        h.setdefault("User-Agent", UA)
        return self._proxy_upstream(src["url"], h)

    def _proxy_torrent(self, ih, idx):
        """The local streaming server, reached through this origin instead.

        The browser player must never be told to open the streaming server's own
        port: the page may have been opened on a tailnet address that has no
        route to it, and a LAN-only host name means nothing to a phone away from
        the house. Same bytes, same Range behaviour, one origin.
        """
        if not contract.RE_HASH40.match(ih or ""):
            return self._send(400, {"err": "bad infoHash"})
        url = "%s/%s" % (STREMIO_IN, ih) + (("/%s" % idx) if idx is not None else "")
        return self._proxy_upstream(url)

    def _id(self, raw):
        """A title id out of a URL path.

        Percent-decoded, because a provider-qualified id carries a colon and
        the TV encodes it. Decoding a value with no escapes is a no-op, so this
        is safe on the older bare-numeric ids too.
        """
        return urllib.parse.unquote(raw or "").strip()

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            # Hanging up after this response: say so, rather than leaving a
            # keep-alive client to find out by having its next request reset.
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _retry(self, secs=2):
        """503 + Retry-After: the standard "come back shortly" answer for a
        long-poll that timed out without becoming ready. Kept separate from
        _send() because Retry-After has no place in an ordinary JSON reply."""
        body = json.dumps({"err": "not ready"}).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", str(secs))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self, cap=65536):
        """The JSON a client posted, {} if there is none worth reading, or None
        if the client declared a body bigger than the cap.

        Deliberately small: every POST is drained through it now, and a body
        must never be able to make this process hold megabytes per connection.
        None rather than {} for the oversize case because those bytes are NOT
        read off the socket -- the connection is left standing in the middle of a
        message, and the next request parsed from it would be a slice of that
        body. The caller has to answer 413 and hang up."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n > cap:
            return None
        if n <= 0:
            return {}
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return {}
        return d if isinstance(d, dict) else {}

    def _host_ok(self):
        """False for a request addressed to a name this server does not answer to.

        This is the DNS-rebinding guard, and it has to run on GET as well as
        POST: once the browser believes the attacker's page and this server
        share an origin, it will read the response back, so /src/ and /audio/
        leak the film just as surely as a forged POST starts one.

        A request with no Host at all is accepted -- that is an HTTP/1.0
        client, and no browser omits it.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        if host.startswith("["):                 # [::1]:8090 -- bracketed IPv6
            host = host[1:host.find("]")] if "]" in host else host[1:]
        elif host.count(":") == 1:               # name:port, never bare IPv6
            host = host.split(":", 1)[0]
        if not host:
            return False
        try:
            ipaddress.ip_address(host)           # LAN address, tailnet address
            return True
        except ValueError:
            pass
        if host == "localhost" or host == PUBLIC_HOST.lower():
            return True
        return host in HOST_ALLOW or host.endswith(HOST_ALLOW_SUFFIX)

    def _origin_ok(self):
        """False for a POST that smells like a cross-site browser request.

        A request with no Origin (curl, the TV app) is always accepted. One
        that has an Origin whose host disagrees with our own Host header, or
        a Sec-Fetch-Site saying "cross-site", is a forgery riding someone
        else's page and is rejected before any route runs.
        """
        origin = self.headers.get("Origin")
        if origin:
            host = urllib.parse.urlsplit(origin).netloc  # scheme stripped, port kept
            if host and host != self.headers.get("Host"):
                return False
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            return False
        return True

    # ---- provider admin --------------------------------------------------------
    def _session_cookie(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == ADMIN_COOKIE and v:
                return v
        return None

    def _require_admin(self):
        """Valid session cookie, and -- for anything but a GET -- a
        X-Cinematica-CSRF header that matches it. Sends a machine-readable
        401 ({"err": "admin required"}) and returns None on failure, so a
        caller just does `if self._require_admin() is None: return` and the
        web UI can show a login form instead of a broken page."""
        tok = self._session_cookie()
        if not tok or not gateway.check_session(tok):
            self._send(401, {"err": "admin required"})
            return None
        if self.command != "GET":
            given = self.headers.get(CSRF_HEADER) or ""
            expected = gateway.csrf_for(tok)
            if not given or not hmac.compare_digest(given, expected):
                self._send(401, {"err": "admin required"})
                return None
        return tok

    def _provider_error(self, ex):
        d = ex.as_dict()
        d["message"] = contract.redact(d.get("message"), gateway.secret_values())
        return self._send(PROVIDER_ERROR_STATUS.get(ex.code, 500), d)

    def _provider_summary(self, rec):
        code, msg = gateway.provider_status(rec.id)
        last_test = rec.last_test
        if isinstance(last_test, dict):
            last_test = dict(last_test)
            if last_test.get("message"):
                last_test["message"] = contract.redact(last_test["message"], gateway.secret_values())
        # A Stremio add-on is configured by its URL, so "source" is not a bare
        # address: the account token and the debrid API key live in a path
        # segment of it. Declared secret config fields have always been masked
        # here; the URL was being handed back verbatim beside them, which made
        # the masking of the fields beside it beside the point. Masked, not
        # dropped -- contract.mask_url keeps the host, so the settings page can
        # still say which add-on this is, and a public add-on with nothing
        # configured still reads in full.
        return {"id": rec.id, "name": rec.manifest.get("name"), "version": rec.manifest.get("version"),
                "capabilities": rec.manifest.get("capabilities"), "enabled": rec.enabled,
                "source": contract.mask_url(rec.source), "status": code, "message": msg,
                "config_fields": rec.manifest.get("config"),
                "config": gateway.public_config(rec.id), "last_test": last_test}

    def _providers_payload(self):
        return {"providers": [self._provider_summary(rec) for rec in gateway.list_installed()],
                "active": gateway.active_all()}

    def _read_body_capped(self, cap):
        """Like _body(), but returns raw bytes with no JSON parsing and a
        much larger cap -- for the one POST body allowed to be a multi-
        megabyte tar.gz. None means the client declared more than `cap` and
        none of it was read off the socket; the caller must 413 and hang up."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if n > cap:
            return None
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _provider_upload_route(self, p):
        """POST /api/providers/install and .../<id>/update both accept
        EITHER a small {"url": ...} JSON body (install/update from a Stremio
        add-on manifest) OR a tar.gz package upload -- told apart by content
        (gzip magic bytes), not by Content-Type, since a client may not set
        it precisely. Handled outside do_POST's normal body cap, which is
        far too small for a real package.
        """
        if self._require_admin() is None:
            return
        raw = self._read_body_capped(MAX_PACKAGE_BYTES)
        if raw is None:
            self.close_connection = True
            return self._send(413, {"err": "body too large"})
        is_gzip = raw[:2] == b"\x1f\x8b"
        payload = None
        if not is_gzip:
            try:
                parsed = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                payload = parsed if isinstance(parsed, dict) else None
            except Exception:
                payload = None
            if payload is None:
                return self._send(400, {"err": "config",
                                        "message": "body is neither a JSON {url} object nor a tar.gz package"})
        if p.path == "/api/providers/install":
            return self._install_package(raw) if is_gzip else self._install_addon(payload)
        pid = p.path[len("/api/providers/"):-len("/update")]
        if not pid:
            return self._send(404, {"err": "not found"})
        return self._update_package(pid, raw) if is_gzip else self._update_addon(pid, payload)

    def _install_addon(self, payload):
        url = (payload or {}).get("url")
        if not url:
            return self._send(400, {"err": "no url"})
        try:
            raw = gateway.fetch_addon_manifest(url)
            manifest = gateway.addon_manifest_to_provider(raw, url)
            rec = gateway.install_provider(manifest, source=url)
        except contract.ProviderError as ex:
            return self._provider_error(ex)
        except contract.ContractError as ex:
            return self._send(400, {"err": "config", "code": ex.code,
                                    "message": contract.redact(ex.message, gateway.secret_values())})
        gateway.invalidate(rec.id)
        return self._send(200, self._provider_summary(rec))

    def _install_package(self, raw):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                _safe_extract_tar(raw, tmp)
                manifest = _read_package_manifest(tmp)
                rec = gateway.install_provider(manifest, files_dir=tmp, source="package")
            except ValueError as ex:
                return self._send(400, {"err": "config", "message": str(ex)})
            except contract.ContractError as ex:
                return self._send(400, {"err": "config", "code": ex.code, "message": ex.message})
        gateway.invalidate(rec.id)
        return self._send(200, self._provider_summary(rec))

    def _update_addon(self, pid, payload):
        url = (payload or {}).get("url")
        if not url:
            return self._send(400, {"err": "no url"})
        try:
            raw = gateway.fetch_addon_manifest(url)
            manifest = gateway.addon_manifest_to_provider(raw, url)
            rec = gateway.update_provider(pid, manifest, source=url)
        except contract.ProviderError as ex:
            return self._provider_error(ex)
        except contract.ContractError as ex:
            return self._send(400, {"err": "config", "code": ex.code,
                                    "message": contract.redact(ex.message, gateway.secret_values())})
        if rec is None:
            return self._send(404, {"err": "not found"})
        gateway.invalidate(pid)
        return self._send(200, self._provider_summary(rec))

    def _update_package(self, pid, raw):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                _safe_extract_tar(raw, tmp)
                manifest = _read_package_manifest(tmp)
                rec = gateway.update_provider(pid, manifest, files_dir=tmp, source="package")
            except ValueError as ex:
                return self._send(400, {"err": "config", "message": str(ex)})
            except contract.ContractError as ex:
                return self._send(400, {"err": "config", "code": ex.code, "message": ex.message})
        if rec is None:
            return self._send(404, {"err": "not found"})
        gateway.invalidate(pid)
        return self._send(200, self._provider_summary(rec))

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        note_request(p.path)
        if not self._host_ok():
            self.close_connection = True
            return self._send(403, {"ok": False, "msg": "unrecognised host"})
        try:
            if p.path == "/":
                body = open(os.path.join(HERE, "index.html"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(body)
                return
            if p.path in STATIC:
                # The few files the page pulls in besides itself (the wordmark's
                # face). Long-lived cache: a phone on the sofa should fetch a
                # 97 KB font once, not on every reload of a no-store page. A
                # new file gets a new path rather than a new version of this one.
                fn, ctype = STATIC[p.path]
                try:
                    body = open(os.path.join(HERE, "static", fn), "rb").read()
                except OSError:
                    return self._send(404, {"error": "no such file"})
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(body)
                return
            if p.path == "/api/setup/state":
                # Unauthenticated by design -- the settings page needs to know
                # whether to show a login form or a setup form before there is
                # any session to prove who is asking. No secret, no provider
                # config value and no bootstrap token belongs in this response.
                st = gateway.setup_required()
                return self._send(200, {"claimed": gateway.admin_claimed(),
                                        "configured": bool(st.get("configured")),
                                        "roles": st.get("roles") or {},
                                        "message": st.get("message") or ""})
            if p.path == "/api/providers":
                if self._require_admin() is None:
                    return
                return self._send(200, self._providers_payload())
            if p.path == "/api/genres":
                q = urllib.parse.parse_qs(p.query)
                kind = q.get("kind", ["movie"])[0]
                if kind not in ("movie", "tv"):
                    kind = "movie"
                return self._send(200, {"genres": get_genres(kind)})
            if p.path == "/api/movies/progress":
                # Cheap and lock-free: what the matching /api/movies is doing.
                q = urllib.parse.parse_qs(p.query)
                kind = q.get("kind", ["movie"])[0]
                if kind not in ("movie", "tv"):
                    kind = "movie"
                g = [x for x in (q.get("genres", [""])[0]).split(",") if x]
                ex = [x for x in (q.get("exclude", [""])[0]).split(",") if x]
                srt = (q.get("sort", ["top"])[0] or "top")
                bias = q.get("bias", [None])[0]
                if bias is not None:
                    bias = bias.lower() not in ("0", "", "false", "no", "off")
                _, _, _, key = view_key(g, srt, ex, kind, bias)
                return self._send(200, view_progress(key))
            if p.path == "/api/shelf":
                # Favourites, newest first, films and series mixed. Rendered
                # from the stored snapshots rather than the pool: a favourite
                # is kept precisely so it survives the catalogue forgetting it.
                return self._send(200, {"items": shelf.favourites()})
            if p.path == "/api/channel":
                # A channel id contains ':' (provider:local), so it travels
                # as a query param, never as a path segment.
                q = urllib.parse.parse_qs(p.query)
                cid = (q.get("id", [""])[0] or "").strip()
                if not cid:
                    return self._send(400, {"err": "no id"})
                try:
                    ops = gateway.channel_ops()
                    view = shelf.channel_view(cid)
                    if view["followed"]:
                        item = shelf.stored_item(cid)
                        if not item or not item.get("title"):
                            # Followed but never actually got a snap (e.g. an
                            # interrupted follow) -- one live fetch to fill it
                            # in; channel_watch() takes over refreshing it daily.
                            ch = channel_details_cached(cid)
                            shelf.set_snap(cid, ch)
                            item = _channel_item(ch, cid)
                    else:
                        item = _channel_item(channel_details_cached(cid), cid)
                except contract.ProviderError as ex:
                    return self._provider_error(ex)
                body = dict(item)
                body["channel_ops"] = sorted(ops)
                return self._send(200, body)
            if p.path == "/api/channel/videos":
                # Blank values kept: "page=" (the first page's next="" followed)
                # means "the full list from the start", and a parser that drops
                # it serves page one again, so the list never grows past it.
                q = urllib.parse.parse_qs(p.query, keep_blank_values=True)
                cid = (q.get("id", [""])[0] or "").strip()
                if not cid:
                    return self._send(400, {"err": "no id"})
                paging = "page" in q
                page = (q.get("page", [""])[0] or "").strip()
                try:
                    ops = gateway.channel_ops()
                    if paging:
                        if contract.OP_CH_VIDEOS not in ops:
                            return self._send(200, {"videos": [], "next": None})
                        r = gateway.channel_videos(cid, page)
                        videos, nxt = r["videos"], r["next"]
                    else:
                        if shelf.channel_view(cid)["followed"]:
                            stored = shelf.stored_latest(cid)
                            fresh = stored["checked"] is not None and \
                                time.time() - stored["checked"] < CHANNEL_POLL_MIN * 60
                            if fresh:
                                videos = stored["videos"]
                            else:
                                r = gateway.channel_latest(cid)
                                videos = r["videos"]
                                shelf.set_latest(cid, videos)
                        else:
                            videos = gateway.channel_latest(cid)["videos"]
                        # "" (never None) when channels.videos is available:
                        # the client reads that as "call channels.videos with
                        # no token for the full list" -- channels.latest has
                        # no paging of its own to hand back a real one.
                        nxt = "" if contract.OP_CH_VIDEOS in ops else None
                except contract.ProviderError as ex:
                    return self._provider_error(ex)
                return self._send(200, {"videos": shelf.decorate_videos(cid, videos), "next": nxt})
            if p.path == "/api/movies":
                q = urllib.parse.parse_qs(p.query)
                kind = q.get("kind", ["movie"])[0]
                if kind == "channel":
                    # Its own shape, not a catalogue page: followed channels
                    # (new uploads first), then Popular -- only when this
                    # install's channels provider can answer that op at all.
                    if not gateway.available(contract.ROLE_CHANNELS):
                        return self._send(200, {"movies": [], "popular": [], "more": False,
                                                "offset": 0, "limit": 0,
                                                "err": "channels are not set up",
                                                "pool": 0, "checked": 0, "channel_ops": []})
                    ops = gateway.channel_ops()
                    movies = shelf.followed_channels()
                    popular = []
                    if contract.OP_CH_POPULAR in ops:
                        followed_ids = {m["id"] for m in movies}
                        popular = [_channel_item(ch) for ch in channel_popular(seeds=followed_ids)
                                  if ch.get("id") not in followed_ids]
                    return self._send(200, {"movies": movies, "popular": popular, "more": False,
                                            "offset": 0, "limit": len(movies), "err": None,
                                            "pool": len(movies), "checked": 0,
                                            "channel_ops": sorted(ops)})
                if kind not in ("movie", "tv"):
                    kind = "movie"
                g = [x for x in (q.get("genres", [""])[0]).split(",") if x]
                # unclamped, a large/negative offset forces resolution of the
                # whole pool (or negative-slice weirdness) for an unauthenticated caller
                off = max(0, min(int(q.get("offset", ["0"])[0]), POOL_MAX))
                lim = max(1, min(int(q.get("limit", [str(PAGE)])[0]), 50))
                srt = (q.get("sort", ["top"])[0] or "top")
                ex  = [x for x in (q.get("exclude", [""])[0]).split(",") if x]
                # absent -> the server default; the TV app always says which
                bias = q.get("bias", [None])[0]
                if bias is not None:
                    bias = bias.lower() not in ("0", "", "false", "no", "off")
                ms, more, cursor, pool, perr = get_page(g, off, lim, srt, ex, kind, bias)
                # Decorated COPIES. `ms` is a slice of the pool's own served
                # list, which every later request for this view is served
                # from -- a shelf state written into those dicts would be
                # baked into the cache and go on being sent long after it
                # stopped being true.
                body = {"movies": [shelf.decorate(dict(m)) for m in ms], "err": perr,
                        "genres_applied": g,
                        "excluded": ex,
                        "bias": BIAS if bias is None else bias,
                        "sort": srt if srt in SORTS else "top",
                        "offset": off, "limit": lim, "more": more,
                        "checked": cursor, "pool": pool}
                if off == 0:
                    # First page only: the client prepends these and drops any
                    # later catalogue item with the same id, so sending them
                    # again further down the wall would only duplicate work.
                    # Paging arithmetic above is untouched by them.
                    body["pinned"] = shelf_pins(kind)
                return self._send(200, body)
            if p.path == "/api/search/stream":
                # Same search, pushed result-by-result. A cold franchise search
                # resolves ~60 candidates through the streams provider and takes
                # ~25s, but the first playable film is ready in about one -- so the page
                # should not sit empty waiting for the last one.
                sq = urllib.parse.parse_qs(p.query)
                term = (sq.get("q", [""])[0] or "").strip()
                if not term:
                    return self._send(400, {"err": "no query"})
                lim = max(1, min(int(sq.get("limit", ["24"])[0]), 60))
                kind = sq.get("kind", ["movie"])[0]
                if kind == "channel":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    def emit(ev, obj):
                        self.wfile.write(("event: %s\ndata: %s\n\n"
                                          % (ev, json.dumps(obj))).encode("utf-8"))
                        self.wfile.flush()
                    try:
                        ops = gateway.channel_ops()
                        # A link or an @handle names exactly one channel --
                        # resolve it rather than search it, and fall back to
                        # resolve for anything else when this install's
                        # provider has no search index of its own.
                        if "/" in term or term.startswith("@") or contract.OP_CH_SEARCH not in ops:
                            n = 1
                            emit("movie", _channel_item(gateway.channel_resolve(term)))
                        else:
                            items = gateway.channel_search(term, lim)["items"]
                            n = len(items)
                            for ch in items:
                                emit("movie", _channel_item(ch))
                        emit("done", {"playable": n, "found": n, "checked": n})
                    except contract.ProviderError as ex:
                        try:
                            emit("fail", {"err": ex.message})
                        except Exception:
                            pass
                    except (BrokenPipeError, ConnectionResetError):
                        pass              # searched again, or closed the popup
                    except Exception as ex:
                        try:
                            emit("fail", {"err": "%s: %s" % (type(ex).__name__, ex)})
                        except Exception:
                            pass
                    return
                if kind not in ("movie", "tv"):
                    kind = "movie"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                def emit(ev, obj):
                    self.wfile.write(("event: %s\ndata: %s\n\n"
                                      % (ev, json.dumps(obj))).encode("utf-8"))
                    self.wfile.flush()
                try:
                    ms, found, checked = search_movies(
                        term, lim,
                        on_found=lambda c: emit("found", {"found": c}),
                        on_movie=lambda m: emit("movie", shelf.decorate(dict(m))),
                        kind=kind)
                    emit("done", {"playable": len(ms), "found": found, "checked": checked})
                except (BrokenPipeError, ConnectionResetError):
                    pass                  # searched again, or closed the popup
                except Exception as ex:
                    try:
                        emit("fail", {"err": "%s: %s" % (type(ex).__name__, ex)})
                    except Exception:
                        pass
                return
            if p.path == "/api/search":
                q = urllib.parse.parse_qs(p.query)
                term = (q.get("q", [""])[0] or "").strip()
                if not term:
                    return self._send(400, {"err": "no query"})
                lim = max(1, min(int(q.get("limit", ["24"])[0]), 60))
                kind = q.get("kind", ["movie"])[0]
                if kind not in ("movie", "tv"):
                    kind = "movie"
                ms, found, checked = search_movies(term, lim, kind=kind)
                return self._send(200, {"q": term,
                                        "movies": [shelf.decorate(dict(m)) for m in ms],
                                        "playable": len(ms),
                                        "found": found, "checked": checked})
            if p.path.startswith("/api/movie/"):
                tid = self._id(p.path.rsplit("/", 1)[-1])
                d = gateway.details(tid, contract.KIND_MOVIE)
                # The catalogue's own rating, as it always was here; the IMDb
                # one is the tile's badge (imdb.rating), not this.
                tr = own_rating(d)
                return self._send(200, shelf.decorate({
                    "id": d.get("id"), "title": d.get("title"),
                    "tagline": d.get("tagline"), "overview": d.get("overview"),
                    "runtime": d.get("runtime"), "vote": tr[0] if tr else None,
                    "votes": tr[1] if tr else None, "release": d.get("release_date"),
                    # `year` as well as `release`: a provider may know a title's
                    # year without knowing its exact release date, and the grid
                    # shows the year. Deriving it from `release` on the client
                    # loses exactly those titles.
                    "year": d.get("year"),
                    "genres": d.get("genres") or [],
                    "backdrop": d.get("backdrop"), "poster": d.get("poster"),
                    "imdb_id": (d.get("external_ids") or {}).get("imdb")}))
            if p.path.startswith("/api/tv/") and "/season/" in p.path:
                bits = p.path[len("/api/tv/"):].split("/")
                bits[0] = self._id(bits[0])
                if len(bits) != 3 or bits[1] != "season":
                    return self._send(404, {"err": "not found"})
                tid, n = bits[0], int(bits[2])
                e = tv_season(tid, n)
                # Copies again: this dict IS the 24h season cache, and a
                # watched flag written into it would outlive the fact.
                return self._send(200, dict(e, episodes=[
                    dict(ep, **_ep_view(tid, n, ep)) for ep in e["episodes"]]))
            if p.path.startswith("/api/tv/"):
                tid = self._id(p.path.rsplit("/", 1)[-1])
                return self._send(200, shelf.decorate(dict(tv_detail(tid))))
            if p.path.startswith("/api/stream/tv/"):
                bits = p.path[len("/api/stream/tv/"):].split("/")
                bits[0] = self._id(bits[0])
                if len(bits) != 3:
                    return self._send(404, {"err": "not found"})
                tid, s, ep = bits
                force = "force" in urllib.parse.parse_qs(p.query)
                e = get_stream_tv(tid, int(s), int(ep), force=force)
                pick = e["pick"]
                # rejected: so a client showing "N streams, 0 playable" can
                # also say why -- codec, hardsub, language, budget...
                return self._send(200, {"pick": pick, "count": e["count"], "err": e["err"],
                                        "rejected": e.get("rejected") or {},
                                        "url": stream_url(pick) if pick else None})
            if p.path.startswith("/api/stream/"):
                tid = self._id(p.path.rsplit("/", 1)[-1])
                force = "force" in urllib.parse.parse_qs(p.query)
                e = get_stream(tid, force=force)
                pick = e["pick"]
                return self._send(200, {"pick": pick, "count": e["count"], "err": e["err"],
                                        "rejected": e.get("rejected") or {},
                                        "url": stream_url(pick) if pick else None})
            if p.path.startswith("/api/progress/"):
                return self._send(200, job_get(self._id(p.path.rsplit("/", 1)[-1])))
            if p.path == "/api/bx/probes":
                # Unauthenticated and cheap: a static list, published so the
                # page and this server cannot drift apart on which codec
                # strings caps["types"] is keyed by -- both sides build
                # against browser_play.CODEC_PROBES, but only this process
                # can prove which version of it actually shipped.
                # "audio" is the subset of "probes" that names an audio
                # codec. Every probe is published as video/mp4, so the page
                # cannot work that out for itself -- and without it the
                # pairing probes it builds have nothing to pair.
                return self._send(200, {"probes": list(browser_play.CODEC_PROBES),
                                        "audio": list(browser_play.AUDIO_PROBES)})
            if p.path.startswith("/src/"):
                return self._proxy_source(p.path[len("/src/"):].strip("/"))
            if p.path.startswith("/t/"):
                bits = [x for x in p.path[len("/t/"):].split("/") if x]
                idx = bits[1] if len(bits) > 1 and bits[1].isdigit() else None
                return self._proxy_torrent(bits[0] if bits else "", idx)
            if p.path.startswith("/audio/"):
                # Serve the converted file from disk, following it as ffmpeg
                # appends. A real file means a player reconnect resumes from a
                # byte offset instead of restarting the film from zero.
                bits = [x for x in p.path[len("/audio/"):].split("/") if x]
                if not bits or not contract.RE_HASH40.match(bits[0]):
                    return self._send(400, {"err": "bad infoHash"})
                ih = bits[0]
                idx = bits[1] if len(bits) > 1 and bits[1].isdigit() else None
                name = tc_name(ih, idx)
                path = os.path.join(TC_HOST, name)
                if not os.path.exists(path):
                    return self._send(404, {"err": "no converted stream for this title"})
                start_at = 0
                rng = self.headers.get("Range") or ""
                m = re.match(r"bytes=(\d+)-", rng)
                if m:
                    start_at = int(m.group(1))
                # Logged because "missing the opening sequence" would be explained
                # by the player seeking to the live edge of a growing file rather
                # than starting at byte 0.
                try:
                    cur = os.path.getsize(path)
                except OSError:
                    cur = 0
                print("audio: %s range=%r -> start=%d of %d bytes on disk"
                      % (name, rng or "none", start_at, cur), flush=True)
                # A finished conversion is just a file, and must be served like
                # one. Without a length the player cannot tell "the film ended"
                # from "the connection dropped", so on reaching the last byte it
                # reconnected at offset 0 and played the whole film again -- the
                # looping. A real length also makes the file properly seekable.
                done = not transcode_writing(ih, name)
                self.send_response(206 if m else 200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Accept-Ranges", "bytes")
                if done:
                    end = max(start_at, cur - 1)
                    if m:
                        self.send_header("Content-Range",
                                         "bytes %d-%d/%d" % (start_at, end, cur))
                    self.send_header("Content-Length", str(max(0, cur - start_at)))
                elif m:
                    # Still converting, so the total genuinely is unknown: RFC
                    # 7233 allows "*" there, but the end must still be a number.
                    self.send_header("Content-Range",
                                     "bytes %d-%d/*" % (start_at, max(start_at, cur - 1)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                idle = 0
                held = 0
                try:
                    with open(path, "rb") as f:
                        f.seek(start_at)
                        while True:
                            chunk = f.read(262144)
                            if chunk:
                                idle = 0
                                self.wfile.write(chunk)
                                continue
                            if not transcode_writing(ih, name):
                                break            # conversion finished, file complete
                            if transcode_suspended(ih):
                                # The regulator is holding it back on purpose --
                                # seek forward and the player reads to the live
                                # edge, where 60s of "no growth" used to look
                                # like EOF and hang up mid-film.
                                idle = 0
                                held += 1
                                if held > 12000:
                                    break        # 20 min suspended: regulator gone
                                time.sleep(0.1)
                                continue
                            held = 0
                            idle += 1
                            if idle > 600:
                                break            # 60s with no new data: give up
                            time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass                          # player went away; expected
                return
            if p.path.startswith("/hls/"):
                # On-demand browser HLS: index.m3u8, init.mp4, and the
                # per-segment fMP4 files, all under /hls/<token>/... The
                # token IS the route: checking it against _bx["token"] here
                # is the whole mechanism that keeps a stale browser tab's
                # requests from ever reaching a session that replaced it --
                # there is no other check anywhere downstream of this one.
                bits = [x for x in p.path[len("/hls/"):].split("/") if x]
                if len(bits) != 2 or not browser_play.RE_TOKEN.match(bits[0]):
                    return self._send(404, {"err": "not found"})
                token, leaf = bits
                with _lock:
                    if _bx["token"] != token:
                        return self._send(404, {"err": "not found"})
                    sess_dir, seg, dur = _bx["dir"], _bx["seg"], _bx["dur"]
                    n_segs = _bx["n_segs"]

                if leaf == "index.m3u8":
                    body = browser_play.vod_playlist(dur, seg)
                    if body is None:
                        return self._send(404, {"err": "not found"})
                    data = body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                if leaf == "init.mp4":
                    init_path = os.path.join(sess_dir, "init.mp4")
                    t0 = time.time()
                    while not os.path.exists(init_path):
                        with _lock:
                            if _bx["token"] != token:
                                return self._send(404, {"err": "not found"})
                        if time.time() - t0 > BX_SEG_WAIT:
                            return self._retry()
                        time.sleep(0.1)
                    try:
                        with open(init_path, "rb") as f:
                            data = f.read()
                    except OSError:
                        return self._retry()
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                m = re.match(r"^s(\d{6})\.m4s$", leaf)
                if not m:
                    return self._send(404, {"err": "not found"})
                k = int(m.group(1))
                if n_segs is None or k >= n_segs:
                    return self._send(404, {"err": "not found"})

                key = "bx:" + token
                seg_path = os.path.join(sess_dir, "s%06d.m4s" % k)
                succ_path = os.path.join(sess_dir, "s%06d.m4s" % (k + 1))
                # The last slot in the playlist has to carry the end of the
                # film, and a run that seeked does not fit the grid it was
                # numbered against.
                #
                # BUG a seek makes ffmpeg start at the keyframe at or before
                # k0*seg -- EARLIER than the grid point, by design (see
                # bx_spawn) -- so that run has more film left to write than
                # the grid budgeted slots for, and it numbers the overflow
                # past the end of the playlist. Measured: seeking to 40s in
                # a 60s clip on a 4s grid wrote s000010..s000015, six files
                # for the five slots 10..14 that the playlist has. s000015
                # held the last 2.04s, the playlist never named it, and the
                # film ended 2s early -- silently, because nothing errored:
                # the player simply ran out of playlist.
                #
                # So the final slot serves its own file AND every file after
                # it. They are consecutive fragments of one continuous run,
                # already stamped on one timeline by the shift below, so
                # what the player gets is the real end of the film.
                last_slot = (k == n_segs - 1)

                def _bx_complete():
                    # The next file existing is what proves the muxer closed
                    # this one -- it only finalizes segment k the instant it
                    # opens k+1. The one exception is the process being gone
                    # entirely: nothing will ever open a successor then, so
                    # whatever is on disk for k is all there is ever going to
                    # be, and has to be served as final rather than waited on
                    # forever.
                    if last_slot:
                        # A successor file proves nothing here: on the final
                        # slot the successor is part of THIS response, so it
                        # has to be finished too. Only the run being over
                        # settles that -- and a run that has reached the last
                        # slot has reached the end of the film, so it is
                        # about to end anyway.
                        return os.path.exists(seg_path) and not transcode_alive(key)
                    return os.path.exists(seg_path) and (
                        os.path.exists(succ_path) or not transcode_alive(key))

                if not _bx_complete():
                    frontier = bx_frontier(token)
                    if frontier is None:
                        return self._send(404, {"err": "not found"})
                    near = frontier <= k <= frontier + BX_LOOKAHEAD
                    if not near:
                        # Far from the frontier: a seek. There is no separate
                        # seek API for an HLS <video> element to call, so the
                        # segment request IS the seek channel -- this is how
                        # the player tells the server it moved. Debounce it
                        # so a drag across many segments collapses into one
                        # restart instead of one per segment the scrub
                        # passes over; seek_gen is what lets a later request
                        # (a newer point in the same drag) cancel this one.
                        with _lock:
                            if _bx["token"] != token:
                                return self._send(404, {"err": "not found"})
                            _bx["pending_anchor"] = k
                            _bx["seek_gen"] += 1
                        time.sleep(BX_SEEK_DEBOUNCE)
                        with _lock:
                            still = (_bx["token"] == token
                                    and _bx["pending_anchor"] == k)
                        if still:
                            bx_restart(token, k)
                        # either way, fall through to the long-poll below

                    t0 = time.time()
                    while not _bx_complete():
                        with _lock:
                            gen = _bx["gen"]
                            alive = _bx["token"] == token
                        if not alive or superseded(gen):
                            return self._send(404, {"err": "not found"})
                        if time.time() - t0 > BX_SEG_WAIT:
                            return self._retry()
                        time.sleep(0.1)

                try:
                    with open(seg_path, "rb") as f:
                        data = f.read()
                    if last_slot:
                        # Re-listed here rather than reused from above: the
                        # long poll may have waited a while, and the run may
                        # have written more of the tail in the meantime.
                        j = k + 1
                        while True:
                            more = os.path.join(sess_dir, "s%06d.m4s" % j)
                            if not os.path.exists(more):
                                break
                            with open(more, "rb") as f:
                                # Only the fragments: appending whole files
                                # would leave a styp and two sidx boxes in
                                # the middle of a media segment.
                                data += browser_play.fragments_only(f.read())
                            j += 1
                except OSError:
                    return self._retry()

                timescales = bx_timescales(token)
                if not timescales:
                    return self._retry()
                with _lock:
                    if _bx["token"] != token:
                        return self._send(404, {"err": "not found"})
                    run_anchor = _bx["run_anchor"]

                # What is added is the RUN's real start time, once, to every
                # segment that run wrote -- not this segment's own grid
                # position.
                #
                # BUG it used to add seg_start(k, seg), reasoning that k*seg
                # is segment k's absolute position on the grid. That part is
                # true; adding it was not. -ss is an input seek and -copyts
                # is never passed, so ffmpeg zeroes the run's clock at the
                # keyframe it seeked to and then counts up CONTINUOUSLY
                # across every segment that run writes -- the second segment
                # of a run already carries a segment's worth of ticks in its
                # own bytes. Adding k*seg on top counted the same elapsed
                # time twice: on the unseeked run, the segment holding 6s of
                # the film was served stamped 12s, and the error grew with
                # k. The bytes are missing exactly one thing, the offset
                # ffmpeg threw away when it zeroed its clock, and that is
                # the run's anchor.
                #
                # Adding the anchor rather than the grid position is also
                # what keeps consecutive segments gapless: ffmpeg's own
                # count is contiguous within a run, so shifting the whole
                # run by one constant preserves that, while stamping each
                # segment at k*seg would have forced a gap or an overlap
                # wherever a keyframe made the real segment longer or
                # shorter than the grid promised.
                #
                # This needs every file present to belong to the current
                # run, which bx_spawn guarantees by clearing the older ones
                # before the run starts.
                deltas = browser_play.deltas_for(timescales, run_anchor)
                try:
                    data, _trafs, _sidx = browser_play.shift_timeline(data, deltas)
                except browser_play.TfdtPatchError as ex:
                    # A silently mis-stamped segment plays back with a
                    # timestamp that looks plausible and is not -- far worse
                    # than a failed request, so this is a hard stop, logged
                    # for whoever has to work out which segment went wrong.
                    print("bx: tfdt patch failed for %s seg %06d: %s"
                          % (token[:8], k, contract.redact(str(ex), gateway.secret_values())),
                          flush=True)
                    return self._send(500, {"err": "segment could not be timestamped"})

                total = len(data)
                start, end, status = 0, total - 1, 200
                rng = self.headers.get("Range") or ""
                mrange = re.match(r"bytes=(\d+)-(\d*)", rng)
                if mrange:
                    start = int(mrange.group(1))
                    end = int(mrange.group(2)) if mrange.group(2) else total - 1
                    end = min(end, total - 1)
                    if start > end or start >= total:
                        return self._send(416, {"err": "range not satisfiable"})
                    status = 206
                chunk = data[start:end + 1]
                self.send_response(status)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if status == 206:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
                self.send_header("Content-Length", str(len(chunk)))
                # Unlike /audio/, deliberately keep-alive: segments are many
                # and small, and a fresh TCP+TLS-less handshake per one would
                # cost more than the segment itself on a slow link.
                self.end_headers()
                self.wfile.write(chunk)
                return
            if p.path == "/api/nowplaying":
                app = app_fresh()
                st = tv_playback_state()
                live = st in (2, 3)
                known = live and bool(_now.get("title"))
                # _now records what was last HANDED OVER, which is empty after a
                # restart and wrong if the app was told to play by something else.
                # The app knows what it actually has open, so it wins on the title.
                title = _now.get("title") if known else None
                if app and live and not title and app.get("title"):
                    title = str(app["title"])[:200]
                return self._send(200, {
                    "playing": st == 3,
                    "paused": st == 2,
                    "live": live,
                    "state": st,
                    # who the state above came from, so the phone knows whether
                    # a position is worth showing
                    "source": "app" if app else
                              ("adb" if (ADB_ENABLED and st is not None) else None),
                    "position_s": app.get("position_s") if app else None,
                    "duration_s": app.get("duration_s") if app else None,
                    "app": {"name": app["name"], "version": app["version"],
                            "seen_s": round(time.time() - app["seen_at"], 1)}
                           if app else None,
                    "title": title,
                    "tag": _now.get("tag") if known else None,
                    "audio": _now.get("audio") if known else None,
                    "transcoded": _now.get("transcoded") if known else None,
                    # The size that ACTUALLY played. run_play_job walks up to
                    # ATTEMPTS candidates and quietly falls through when one is
                    # too slow, so the film on screen can be a different release
                    # from the one whose size the grid advertised. Without this
                    # the interface could never tell you that happened.
                    "gb": _now.get("gb") if known else None,
                    "kind": _now.get("kind"),
                    "season": _now.get("season"),
                    "episode": _now.get("episode"),
                    "next": _now.get("next"),
                    "hifi": {"on": _hifi["on"], "gen": _hifi["gen"],
                             "err_s": _hifi["err_s"], "streaming": _hifi["streaming"],
                             "pending": _hifi["pending_since"] > 0,
                             "delay_ms": _hifi["delay_ms"],
                             "applied_delay_ms": _hifi["applied_delay_ms"],
                             "connected": _hifi["connected"],
                             "player_url": _hifi["player_url"],
                             "last_error": _hifi["last_error"],
                             "fail_count": _hifi["fail_count"],
                             "supply": _hifi["supply"],
                             "cache": _hifi["cache"]},
                })
            if p.path == "/api/hifi/players":
                # Request-handler thread, not the heartbeat path -- calling
                # the bridge directly here is fine (see _ss_call's own doc).
                status, body = _ss_call("/players", timeout=3.0)
                if status != 200:
                    return self._send(200, {"ok": False, "players": [],
                                            "error": body.get("error") or "bridge unreachable"})
                return self._send(200, {"ok": True, **body})
            if p.path == "/api/netcheck":
                # The page polls this every 2s while calibrating, so it must
                # never block behind a writer. A status read is not worth
                # waiting on: report progress and move on.
                if not _lock.acquire(timeout=2):
                    return self._send(200, {"busy": _net_busy["on"],
                                            "calibrating": _cal["on"],
                                            "cal_done": _cal["done"],
                                            "cal_want": _cal["want"],
                                            "cal_msg": _cal["msg"] or "working\u2026",
                                            "stale_read": True})
                try:
                    prof = dict(_net)
                finally:
                    _lock.release()
                prof["sustain_live"] = SUSTAIN_MBPS
                # first run has no profile at all, so the page can say what the
                # wait is for rather than just looking slow
                prof["busy"] = _net_busy["on"]
                prof["first_run"] = not prof.get("at")
                # recount both: record_rate() appends from real playback, so the
                # figure frozen at the last derive drifts behind the list itself
                prof["n_samples"] = len(prof.get("samples") or [])
                prof["n_ordinary"] = len(ordinary_samples())
                prof["needs"] = max(0, NET_MIN_OBS - prof["n_ordinary"])
                prof["well_seeded_at"] = WELL_SEEDED
                prof["calibrating"] = _cal["on"]
                prof["cal_done"] = _cal["done"]
                prof["cal_want"] = _cal["want"]
                prof["cal_msg"] = _cal["msg"]
                prof["age_h"] = round((time.time() - float(prof.get("at") or 0)) / 3600, 1) \
                                if prof.get("at") else None
                return self._send(200, prof)
            if p.path == "/api/health":
                # The phone polls this every 15s, so it must never have side
                # effects -- adb_ready() can run `adb connect` and sleep for
                # several seconds, which turned a powered-off TV into constant
                # adb churn and a multi-second response. /api/reconnect is the
                # endpoint that actually reconnects.
                app = app_fresh()
                st = adb_state()          # "off", and no adb run at all, when disabled
                if app:
                    msg = "TV app connected (%s)" % (app["name"] or app["id"] or "unnamed")
                elif ADB_ENABLED:
                    msg = {"device": "connected",
                           "unauthorized": "TV is waiting for you to accept the USB-debugging prompt on screen",
                           }.get(st, f"adb state: {st}")
                else:
                    # Nothing is connected and there is no fallback to offer, so
                    # say the thing the user can act on rather than an adb state
                    # they have deliberately turned off.
                    msg = "no TV app connected"
                return self._send(200, {"tv": bool(app) or (ADB_ENABLED and st == "device"),
                                        "tv_msg": msg, "tv_state": st,
                                        "app": {"id": app["id"], "name": app["name"],
                                                "version": app["version"],
                                                "state": app["state"],
                                                "seen_s": round(time.time() - app["seen_at"], 1)}
                                               if app else None,
                                        "adb": {"enabled": ADB_ENABLED, "state": st},
                                        "movies": pool_served(),
                                        "page_size": PAGE,
                                        "streams_cached": len(_streams),
                                        "providers": providers_health(),
                                        "fourk_only": FOURK_ONLY, "hevc_only": HEVC_ONLY,
                                        "autoplay": AUTOPLAY_NEXT,
                                        "min_seeders": MIN_SEEDERS,
                                        "max_gb": MAX_GB_4K,
                                        "sustain_mbps": SUSTAIN_MBPS,
                                        "conns": _net.get("conns"),
                                        "net_source": _net.get("source")})
        except Exception as ex:
            return self._send(500, {"err": contract.redact(
                "%s: %s" % (type(ex).__name__, ex), gateway.secret_values())})
        self._send(404, {"err": "not found"})

    def do_POST(self):
        # /api/cancel bumps it. Without this the += made it a local of this
        # whole method, and every cancel that had something to cancel died
        # with UnboundLocalError before the job was ever stood down.
        global _cancel_gen
        p = urllib.parse.urlparse(self.path)
        note_request(p.path)
        # Addressed to a name that is not ours: rebinding, and the body is
        # still unread, so the connection goes with it.
        if not self._host_ok():
            self.close_connection = True
            return self._send(403, {"ok": False, "msg": "unrecognised host"})
        # Every mutating route lives behind this: reject a forged cross-site
        # POST before it touches anything.
        if not self._origin_ok():
            # The body is still sitting unread on the socket -- this
            # connection cannot be reused for anything, so close it rather
            # than parse the next request out of the middle of it.
            self.close_connection = True
            return self._send(403, {"ok": False, "msg": "cross-site request rejected"})
        # Provider install/update accept a tar.gz package upload, which the
        # small JSON cap below cannot carry -- handled first, and entirely
        # separately, so a real package never hits the 413 meant for a
        # misbehaving JSON client.
        if p.path == "/api/providers/install" or (
                p.path.startswith("/api/providers/") and p.path.endswith("/update")):
            try:
                return self._provider_upload_route(p)
            except Exception as ex:
                return self._send(500, {"err": contract.redact(
                    "%s: %s" % (type(ex).__name__, ex), gateway.secret_values())})
        # Every POST route needs its body off the socket before it can be
        # routed at all: on a reused keep-alive connection, leaving it
        # unread means the next request is parsed out of the middle of it.
        d = self._body()
        if d is None:
            # Oversize body, and none of it has been read. This connection
            # cannot be reused for anything, so close it rather than parse
            # the next request out of the middle of a body the app is still
            # sending.
            self.close_connection = True
            return self._send(413, {"ok": False, "msg": "body too large"})
        try:
            if p.path == "/api/setup/claim":
                # Single-use: store.claim() itself refuses once a password
                # already exists, regardless of what token is presented.
                token, password = d.get("token"), d.get("password")
                if not token or not password or len(str(password)) < 8:
                    return self._send(400, {"ok": False, "err":
                                            "token and an 8+ character password are required"})
                if not gateway.claim_setup(str(token), str(password)):
                    return self._send(400, {"ok": False, "err": "invalid or already-used setup token"})
                return self._send(200, {"ok": True})
            if p.path == "/api/admin/login":
                client = self.client_address[0]
                wait = gateway.login_wait(client)
                if wait:
                    return self._send(429, {"ok": False, "err": "too many attempts",
                                            "retry_after": wait})
                password = d.get("password")
                ok = bool(password) and gateway.check_admin_password(str(password))
                gateway.note_login(client, ok)
                if not ok:
                    return self._send(401, {"ok": False, "err": "admin required"})
                tok = gateway.new_session()
                csrf = gateway.csrf_for(tok)
                body = json.dumps({"ok": True, "csrf": csrf}).encode()
                self.send_response(200)
                self.send_header("Set-Cookie", "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (ADMIN_COOKIE, tok))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if p.path == "/api/admin/logout":
                tok = self._require_admin()
                if tok is None:
                    return
                gateway.drop_session(tok)
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Set-Cookie", "%s=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" % ADMIN_COOKIE)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if p.path == "/api/providers/preview":
                # See-before-you-install: convert and validate a manifest URL
                # without installing anything.
                if self._require_admin() is None:
                    return
                url = d.get("url")
                if not url:
                    return self._send(400, {"err": "no url"})
                try:
                    raw = gateway.fetch_addon_manifest(url)
                    manifest = gateway.addon_manifest_to_provider(raw, url)
                except contract.ProviderError as ex:
                    return self._provider_error(ex)
                except contract.ContractError as ex:
                    return self._send(400, {"err": "config", "code": ex.code,
                                            "message": contract.redact(ex.message, gateway.secret_values())})
                return self._send(200, {"id": manifest["id"], "name": manifest["name"],
                                        "version": manifest["version"],
                                        "capabilities": manifest["capabilities"],
                                        "config": manifest["config"]})
            if p.path == "/api/providers/active":
                if self._require_admin() is None:
                    return
                role, pid = d.get("role"), d.get("provider_id")
                if role not in contract.ROLES:
                    return self._send(400, {"err": "unknown role"})
                try:
                    gateway.set_active(role, pid)
                except ValueError as ex:
                    return self._send(400, {"err": str(ex)})
                gateway.invalidate(pid if pid else None)
                return self._send(200, {"ok": True, "active": gateway.active_all()})
            if p.path.startswith("/api/providers/") and p.path.endswith("/config"):
                pid = p.path[len("/api/providers/"):-len("/config")]
                if self._require_admin() is None:
                    return
                if gateway.get_provider(pid) is None:
                    return self._send(404, {"err": "not found"})
                values, clear = d.get("values") or {}, d.get("clear") or []
                if not isinstance(values, dict) or not isinstance(clear, list):
                    return self._send(400, {"err": "values must be an object, clear a list"})
                gateway.set_config(pid, values, clear_keys=[str(k) for k in clear])
                gateway.invalidate(pid)
                return self._send(200, {"ok": True, "config": gateway.public_config(pid)})
            if p.path.startswith("/api/providers/") and p.path.endswith("/test"):
                pid = p.path[len("/api/providers/"):-len("/test")]
                if self._require_admin() is None:
                    return
                if gateway.get_provider(pid) is None:
                    return self._send(404, {"err": "not found"})
                # A failed test is a normal, expected outcome here -- captured
                # into the stored/returned result rather than surfaced as an
                # HTTP error, so the admin UI can show it inline.
                try:
                    result = gateway.test(pid)
                    record = {"ok": True, "at": time.time(), "result": result}
                except contract.ProviderError as ex:
                    record = {"ok": False, "at": time.time(), "code": ex.code,
                             "message": contract.redact(ex.message, gateway.secret_values())}
                gateway.record_test(pid, record)
                return self._send(200, record)
            if p.path.startswith("/api/providers/") and p.path.endswith("/enable"):
                pid = p.path[len("/api/providers/"):-len("/enable")]
                if self._require_admin() is None:
                    return
                if gateway.get_provider(pid) is None:
                    return self._send(404, {"err": "not found"})
                enabled = bool(d.get("enabled"))
                gateway.set_enabled(pid, enabled)
                gateway.invalidate(pid)
                return self._send(200, {"ok": True, "enabled": enabled})
            if p.path.startswith("/api/providers/") and p.path.endswith("/remove"):
                pid = p.path[len("/api/providers/"):-len("/remove")]
                if self._require_admin() is None:
                    return
                ok = gateway.remove_provider(pid)
                gateway.invalidate(pid)
                return self._send(200, {"ok": ok})
            if p.path == "/api/player/heartbeat":
                # The TV app's whole connection to the server: its state in, the
                # next command out. Can block for up to the `wait` it asks for,
                # which is the point -- see app_heartbeat().
                return self._send(200, app_heartbeat(d))
            if p.path == "/api/hifi/delay":
                # Live lip-sync trim, for dialling it in from a laptop while
                # watching; the TV's own Settings value takes over on its
                # next heartbeat if it carries one.
                try:
                    with _lock:
                        ms = _hifi_set_delay(d.get("ms", 0), persist=True)
                except (TypeError, ValueError):
                    return self._send(400, {"ok": False, "msg": "ms must be an integer"})
                return self._send(200, {"ok": True, "delay_ms": ms})
            if p.path == "/api/player/volume":
                # Enqueue only -- the bridge call happens on _ss_worker's
                # thread, same as every other sendspin action.
                payload = {"delta": d["delta"]} if "delta" in d else {"level": d.get("level")}
                _ss_q.put(("volume", payload))
                return self._send(200, {"ok": True})
            # The three shelf writes. Household actions, exactly like a play
            # or a stop: behind the host and origin guards every POST here is
            # behind, and not behind _require_admin -- marking a film watched
            # is not administering the install. Each forces a save: these are
            # deliberate, one-at-a-time acts, and a restart losing the last
            # one would be plainly wrong in a way a dropped heartbeat is not.
            if p.path == "/api/shelf/fav":
                tid = str(d.get("id") or "").strip()
                if not tid:
                    return self._send(400, {"ok": False, "msg": "id is required"})
                snap = d.get("snap") if isinstance(d.get("snap"), dict) else None
                shelf.set_fav(tid, bool(d.get("on")), snap=snap)
                shelf.save(force=True)
                return self._send(200, {"ok": True, "shelf": shelf.view(tid)})
            if p.path == "/api/shelf/watched":
                tid = str(d.get("id") or "").strip()
                if not tid:
                    return self._send(400, {"ok": False, "msg": "id is required"})
                # s and e together mean one episode; either missing means the
                # whole title, so a half-given pair is a mistake worth saying
                # rather than silently marking a whole series watched.
                s, e = d.get("s"), d.get("e")
                if s is not None or e is not None:
                    try:
                        s, e = int(s), int(e)
                    except (TypeError, ValueError):
                        return self._send(400, {"ok": False,
                                                "msg": "s and e must both be numbers"})
                shelf.set_watched(tid, bool(d.get("on")), s=s, e=e)
                shelf.save(force=True)
                return self._send(200, {"ok": True, "shelf": shelf.view(tid)})
            if p.path == "/api/shelf/drop":
                # "Done with this". The job form is what a player sends as it
                # stops: it mutes that exact job, so the stop arriving right
                # behind it cannot record the position back again.
                jb = str(d.get("job") or "").strip() or None
                tid = str(d.get("id") or "").strip() or None
                if not jb and not tid:
                    return self._send(400, {"ok": False, "msg": "id or job is required"})
                shelf.drop(title_id=tid, job=jb)
                shelf.save(force=True)
                return self._send(200, {"ok": True})
            # The channel writes. Same household-action footing as the shelf
            # writes just above: no admin gate, one save per act.
            if p.path == "/api/channel/follow":
                cid = str(d.get("id") or "").strip()
                if not cid:
                    return self._send(400, {"ok": False, "msg": "id is required"})
                on = bool(d.get("on"))
                if on:
                    try:
                        ch = channel_details_cached(cid)
                    except contract.ProviderError as ex:
                        return self._provider_error(ex)
                    shelf.follow(cid, True, snap=ch)
                    shelf.save(force=True)
                    def _prime():
                        # Fills the tile's upload count right away, rather
                        # than leaving it at 0 until channel_watch()'s next
                        # sweep (up to CHANNEL_POLL_MIN minutes away).
                        try:
                            r = gateway.channel_latest(cid)
                            shelf.set_latest(cid, r["videos"])
                            shelf.save(force=True)
                        except contract.ProviderError:
                            pass
                    threading.Thread(target=_prime, daemon=True).start()
                    item = _channel_item(ch, cid)
                else:
                    shelf.follow(cid, False)
                    shelf.save(force=True)
                    item = shelf.stored_item(cid) or {"id": cid, "kind": "channel",
                                                       "followed": False, "new": 0}
                return self._send(200, {"ok": True, "channel": item})
            if p.path == "/api/channel/seen":
                cid = str(d.get("id") or "").strip()
                if not cid:
                    return self._send(400, {"ok": False, "msg": "id is required"})
                shelf.channel_seen(cid)
                shelf.save(force=True)
                return self._send(200, {"ok": True})
            if p.path == "/api/channel/opened":
                cid = str(d.get("id") or "").strip()
                vid = str(d.get("video") or "").strip()
                if not cid or not vid:
                    return self._send(400, {"ok": False, "msg": "id and video are required"})
                shelf.video_opened(cid, vid, bool(d.get("on", True)))
                shelf.save(force=True)
                return self._send(200, {"ok": True})
            if p.path == "/api/channel/play":
                # No buffering, no Stremio, no heartbeat -- just the record
                # that it was opened, and whatever the provider says the TV
                # should hand to an external app.
                cid = str(d.get("id") or "").strip()
                vid = str(d.get("video") or "").strip()
                if not cid or not vid:
                    return self._send(400, {"ok": False, "msg": "id and video are required"})
                try:
                    r = gateway.channel_play(cid, vid)
                except contract.ProviderError as ex:
                    return self._send(502, {"ok": False, "msg": ex.message})
                shelf.video_opened(cid, vid, True)
                shelf.save(force=True)
                return self._send(200, {"ok": True, "play": r})
            if p.path.startswith("/api/play/tv/"):
                bits = p.path[len("/api/play/tv/"):].split("/")
                if len(bits) != 3:
                    return self._send(404, {"ok": False, "msg": "not found"})
                try:
                    # The id stays a string: it is a provider-qualified
                    # identifier now, and nothing guarantees it is numeric.
                    tid, s, ep = self._id(bits[0]), int(bits[1]), int(bits[2])
                except ValueError:
                    return self._send(400, {"ok": False, "msg": "bad id/season/episode"})
                if not tid:
                    return self._send(400, {"ok": False, "msg": "bad id/season/episode"})
                jobid = f"tv:{tid}:{s}:{ep}"
                # Same race-refusal as the film route below, keyed by episode
                # rather than movie id.
                amid, aj = active_job()
                if amid is not None and amid == jobid:
                    return self._send(202, {"ok": True, "msg": "already starting",
                                            "job": amid})
                q = urllib.parse.parse_qs(p.query)
                ap = q.get("autoplay", [None])[0]
                autoplay = AUTOPLAY_NEXT if ap is None else ap == "1"
                def resolve():
                    entry = get_stream_tv(tid, s, ep)
                    return entry, entry.get("runtime") or 45
                return self._send(*start_play(jobid, resolve, autoplay=autoplay,
                                              start_s=_start_s(p.query)))
            if p.path.startswith("/api/play/"):
                tid = self._id(p.path.rsplit("/", 1)[-1])
                # Refuse rather than race. The loser of a two-job race does not
                # stop; it keeps buffering and then steals the TV when it lands.
                # Last request wins. A double-tap on the film already being
                # started attaches to that job rather than restarting it, but any
                # OTHER work -- an older job still probing, a film already on
                # screen -- is superseded here, so only one play is ever live.
                amid, aj = active_job()
                if amid is not None and amid == str(tid):
                    return self._send(202, {"ok": True, "msg": "already starting",
                                            "job": amid})
                def resolve():
                    # get_stream() already resolved the identity (and its
                    # runtime, if the catalogue has one) to make this same
                    # stream lookup -- nothing left to fetch again here.
                    e = get_stream(tid)
                    return e, e.get("runtime")
                return self._send(*start_play(str(tid), resolve,
                                              start_s=_start_s(p.query)))
            if p.path.startswith("/api/bplay/tv/"):
                # The browser's counterpart to /api/play/tv/... above --
                # same id/season/episode shape and the same dedupe, but it
                # claims the player as owner="browser" against a session
                # token rather than the TV app, and its worker is
                # run_browser_job rather than run_play_job.
                bits = p.path[len("/api/bplay/tv/"):].split("/")
                if len(bits) != 3:
                    return self._send(404, {"ok": False, "msg": "not found"})
                try:
                    tid, s, ep = self._id(bits[0]), int(bits[1]), int(bits[2])
                except ValueError:
                    return self._send(400, {"ok": False, "msg": "bad id/season/episode"})
                if not tid:
                    return self._send(400, {"ok": False, "msg": "bad id/season/episode"})
                jobid = f"tv:{tid}:{s}:{ep}"
                amid, aj = active_job()
                if amid is not None and amid == jobid:
                    return self._send(202, {"ok": True, "msg": "already starting",
                                            "job": amid})
                caps = d.get("caps") or {}
                skip = d.get("skip") or []
                token = browser_play.new_token()
                # resolve() is the only place start_play() ever computes the
                # catalogue entry, and it runs INSIDE start_play's cancel
                # guard -- so the worker closure below cannot call
                # get_stream_tv() a second time without losing that guard.
                # Stashing the entry in this holder as a side effect of
                # resolve() is what lets the worker reach it, since by the
                # time the worker actually runs, resolve() has already been
                # called (start_play calls it synchronously before spawning
                # the thread).
                holder = {}
                def resolve():
                    entry = get_stream_tv(tid, s, ep)
                    holder["entry"] = entry
                    return entry, entry.get("runtime") or 45
                def worker(mid, picks, runtime_min, title, gen):
                    run_browser_job(mid, browser_picks(holder["entry"], caps),
                                    runtime_min, title, gen, token, caps, skip)
                start_s = _start_s(p.query)
                status, body = start_play(jobid, resolve, owner="browser",
                                          token=token, worker=worker,
                                          start_s=start_s)
                if status != 202:
                    return self._send(status, body)
                # No absolute media URL here -- unlike /api/play/'s body,
                # which hands the TV app a URL it dials directly. The
                # browser gets its media URL only once run_browser_job has
                # actually decided how to serve it (see publish()), and it
                # is always a path relative to this same origin.
                # start_s is echoed rather than acted on: there is no command
                # channel to a browser, so the page seeks its own <video>
                # once the metadata is in.
                return self._send(202, {"ok": True, "job": jobid, "token": token,
                                        "gen": job_get(jobid).get("gen"),
                                        "start_s": start_s})
            if p.path.startswith("/api/bplay/"):
                # The browser's counterpart to /api/play/<id> above.
                tid = self._id(p.path.rsplit("/", 1)[-1])
                amid, aj = active_job()
                if amid is not None and amid == str(tid):
                    return self._send(202, {"ok": True, "msg": "already starting",
                                            "job": amid})
                caps = d.get("caps") or {}
                skip = d.get("skip") or []
                token = browser_play.new_token()
                holder = {}
                def resolve():
                    e = get_stream(tid)
                    holder["entry"] = e
                    return e, e.get("runtime")
                def worker(mid, picks, runtime_min, title, gen):
                    run_browser_job(mid, browser_picks(holder["entry"], caps),
                                    runtime_min, title, gen, token, caps, skip)
                start_s = _start_s(p.query)
                status, body = start_play(str(tid), resolve, owner="browser",
                                          token=token, worker=worker,
                                          start_s=start_s)
                if status != 202:
                    return self._send(status, body)
                return self._send(202, {"ok": True, "job": str(tid), "token": token,
                                        "gen": job_get(str(tid)).get("gen"),
                                        "start_s": start_s})
            if p.path == "/api/bx/beat":
                # The browser's heartbeat: real currentTime and play/pause
                # state, on whatever cadence the page chooses.
                # browser_playing() treats a live heartbeat as "still
                # watching" even while paused, and regulate_hls() paces the
                # packager off _bx["pos"]/_bx["at"] this sets -- so a stale
                # or mismatched beat must change NOTHING, not even the
                # fields a live session's OWN beat would touch, or a
                # straggling request from a tab that has already moved on
                # could corrupt a newer session's pacing.
                token, gen = d.get("token"), d.get("gen")
                with _lock:
                    match = (token is not None and token == _bx["token"]
                             and gen == _bx["gen"])
                if not match:
                    return self._send(409, {"ok": False, "stale": True})
                state = d.get("state")
                with _lock:
                    was = _bx["state"]
                    _bx["at"] = time.time()
                    _bx["state"] = state
                    pos = d.get("pos")
                    if pos is not None:
                        try:
                            _bx["pos"] = float(pos)
                        except (TypeError, ValueError):
                            pass
                    mid, bpos, bdur = _bx["job"], _bx["pos"], _bx["dur"]
                # The shelf, outside the lock (see shelf_note): this beat is
                # the browser's only progress report, so it records where the
                # film got to exactly as the TV's heartbeat does -- "ended"
                # included, which is what marks it watched.
                if mid and state in ("playing", "paused", "ended"):
                    shelf_note(mid, bpos, bdur, state, force_save=(state != was))
                # A pause must never tear the job down -- only a lost
                # heartbeat does, via browser_playing()'s own staleness
                # check above. All a pause has to do here is stop the
                # packager burning CPU (and lead-time) on a viewer who has
                # stepped away, the same SIGSTOP/SIGCONT regulate_lead()
                # already uses for the TV's own transcode, with the same
                # _tc_flag(..., suspended=...) bookkeeping so /audio/-style
                # readers and regulate_hls()'s own resync never disagree
                # about whether this process is actually running.
                key = "bx:" + token
                name = BX_DIR + token
                if state in ("paused", "playing"):
                    pid = _ctr_pid(name)
                    if pid:
                        sig = "-STOP" if state == "paused" else "-CONT"
                        subprocess.run(["docker", "exec", FFMPEG_CTR, "kill", sig, pid],
                                      capture_output=True, timeout=15)
                        _tc_flag(key, suspended=(state == "paused"))
                return self._send(200, {"ok": True})
            if p.path == "/api/bx/stop":
                # A browser session can be abandoned at any point: before
                # bx_begin() ever runs (still buffering/probing -- there is
                # no _bx session yet to tear down), during a live HLS
                # session, or after a direct-mode play that never touched
                # _bx at all. The token is the one thing that identifies
                # "this viewer's attempt" across every one of those states
                # -- a job id gets reused by a later replay of the same
                # title, but a token is minted fresh per attempt -- so both
                # halves of this route match on the token, independently of
                # each other, rather than on the job id.
                #
                # Must tolerate navigator.sendBeacon, which POSTs
                # Content-Type: text/plain and never reads the response --
                # _body() above parses JSON regardless of Content-Type, and
                # every reply here is a plain 200 the beacon will ignore.
                token = d.get("token")
                if not token:
                    return self._send(200, {"ok": True})
                with _lock:
                    bx_match = _bx["token"] == token
                if bx_match:
                    bx_stop_all("Stopped")
                # Independently of the above: a job still buffering or
                # probing has no _bx session yet, so closing the tab during
                # that window must still be able to stand the worker down
                # -- otherwise it keeps holding the swarm open, and
                # active_job() keeps telling claim_owner() a browser is
                # still playing long after the viewer gave up and walked to
                # the TV, which then refuses to play with "Another device
                # is playing" for a film nobody is watching.
                with _lock:
                    amid = next((k for k, j in _jobs.items()
                                if j.get("otoken") == token
                                and j.get("stage") in JOB_ACTIVE), None)
                if amid is not None:
                    play_claim()   # the worker's next superseded(gen) check returns
                    job_set(amid, stage="error", ok=False, msg="Playback abandoned")
                return self._send(200, {"ok": True})
            if p.path == "/api/netcheck":
                # The probe saturates the link for a few seconds, so it must not
                # run against a film in progress -- it would starve the very
                # stream it is trying to characterise.
                if tv_playback_state() in (2, 3):
                    return self._send(409, {"ok": False,
                        "msg": "Something is playing — stop it first, the test needs the link to itself."})
                if _net_busy["on"] or _cal["on"]:
                    return self._send(202, {"ok": True, "msg": "already running"})
                # Runs in the background and reports through GET: a full
                # calibration takes minutes, and the button previously only ever
                # ran the HTTP probe -- which is why the "Optimising your
                # connection" progress was never reachable from the UI.
                # Pressing the button means "measure it again", so it always
                # calibrates rather than only topping up missing samples.
                threading.Thread(target=net_full, kwargs={"force": True},
                                 daemon=True).start()
                return self._send(202, {"ok": True, "msg": "started"})
            if p.path == "/api/cancel":
                # Closing the film's popup means "I have changed my mind". The
                # job keeps buffering otherwise, holding the progress bar and the
                # play claim, and will eventually seize the TV for a film the
                # user walked away from. Deliberately does NOT touch the player:
                # a film already on screen is not an active job and must not be
                # interrupted by closing a different film's popup.
                amid, aj = active_job()
                # A browser tab fires this on popup-close, and once browser
                # playback exists that tab is not necessarily the one that
                # is actually playing -- a stray cancel from a second tab or
                # a delayed request must not kill somebody else's film.
                if (amid is not None and aj.get("owner") == "browser"
                        and d.get("token") != aj.get("otoken")):
                    return self._send(200, {"ok": False, "msg": "not yours"})
                if amid is None:
                    with _lock:
                        inflight = _play_inflight > 0
                        if inflight:
                            _cancel_gen += 1
                    if inflight:
                        # No job exists yet -- it's still in get_stream() -- so
                        # there's nothing for active_job() to find. Bumping
                        # _cancel_gen is the only way to reach it.
                        print("play: cancelled while resolving streams", flush=True)
                        return self._send(200, {"ok": True, "msg": "cancelled"})
                    return self._send(200, {"ok": True, "msg": "nothing to cancel"})
                with _lock:
                    _cancel_gen += 1  # also stands down a concurrent in-flight play
                play_claim()                      # the worker stands down
                job_set(amid, stage="error", ok=False, msg="Cancelled")
                print("play: %s cancelled by the user" % amid, flush=True)
                return self._send(200, {"ok": True, "msg": "cancelled", "job": amid})
            if p.path == "/api/reconnect":
                # Nothing to reconnect to: the TV app is not something this
                # server dials, and adb is off.
                if not ADB_ENABLED:
                    return self._send(200, {"ok": False, "msg": "phone remote is off",
                                            "state": "off"})
                hard = "hard" in urllib.parse.parse_qs(p.query)
                ok, msg = adb_ready(hard=hard)
                return self._send(200, {"ok": ok, "msg": msg, "state": adb_state()})
            if p.path == "/api/stop":
                # Tell the app first: it is the thing with a film on screen, and
                # the command has to be queued before the job is retired or a
                # worker still in its handoff wait would race the stop.
                # Unconditionally, not only while the app is fresh: an app that
                # has just missed a poll is precisely the one that must not come
                # back and carry on playing a film that has been stopped here.
                # If it never comes back, the command expires by itself
                # (APP_CMD_TTL) instead of ambushing the next session.
                app_cmd("stop")
                # An explicit stop is the one case the grace window must not
                # smooth over: the film is gone the moment this returns, so
                # nowplaying should say so, and the calibration guard should
                # not spend 45 s refusing on a memory of it.
                with _lock:
                    _app_last_play.update(state=None, at=0.0)
                play_claim()        # any in-flight job stands down
                amid, _ = active_job()
                if amid is not None:
                    # Retire it now rather than waiting for its worker's next
                    # checkpoint, or an immediate replay of the same film attaches
                    # to a job that is already doomed and fails for no reason.
                    job_set(amid, stage="error", ok=False, msg="Stopped")
                # Stop means stop regardless of who was playing -- a browser
                # session left live here would otherwise go on blocking a TV
                # play through claim_owner() after the human has already hit
                # the one button that is supposed to end it. bx_stop_all()
                # is the real teardown (kills the packager's ffmpeg, drops
                # the session directory); this used to only reset _bx's
                # fields by hand, which left that ffmpeg running -- a real
                # leak, since nothing else here ever reaped it.
                bx_stop_all("Stopped")
                # Whatever the last heartbeat recorded is the resume point for
                # this film, and the viewer has just said they are done with
                # it for now: put it on disk rather than leave it to the
                # 30 s throttle of a heartbeat that is not coming.
                shelf.save(force=True)
                threading.Thread(target=cache_clear, daemon=True).start()
                ok, msg = tv_stop()
                return self._send(200, {"ok": ok, "msg": msg})
        except Exception as ex:
            return self._send(500, {"ok": False, "msg": contract.redact(
                "%s: %s" % (type(ex).__name__, ex), gateway.secret_values())})
        self._send(404, {"err": "not found"})

class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        """A phone closing a tab or VLC hanging up mid-file is ordinary, not an
        error. The stdlib default prints a full traceback per disconnect, and
        /audio/ streams for hours -- the real failures were buried under dozens
        of these an hour."""
        ex = sys.exc_info()[1]
        if isinstance(ex, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)

if __name__ == "__main__":
    # Anything still converting was started by the previous process and is
    # unknown to this one: an orphan by definition (see _kill_orphans).
    threading.Thread(target=transcode_stop_all, daemon=True).start()
    threading.Thread(target=prefetch, daemon=True).start()
    threading.Thread(target=cache_watch, daemon=True).start()
    threading.Thread(target=cache_size_apply, daemon=True).start()
    threading.Thread(target=channel_watch, daemon=True).start()
    if SENDSPIN_ENABLED:
        threading.Thread(target=_ss_worker, daemon=True).start()
    print("cinematica on :%d  (stremio=%s  phone remote=%s)"
          % (PORT, STREMIO, ADB_TV or "off"), flush=True)
    _tok = gateway.bootstrap_token()
    if _tok:
        # None once a password has been claimed -- printed every restart
        # until then, since a fresh install has no other way to learn it.
        print("cinematica: no admin password set -- claim this install at "
              "/api/setup/claim with token: %s" % _tok, flush=True)
    Server(("0.0.0.0", PORT), H).serve_forever()
