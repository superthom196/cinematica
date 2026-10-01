"""Every setting the server reads, in one place: device limits, link
assessment, picking rules, timings. Values come through settings.py (the
environment, then .env, then the default given here).
"""
import os
import settings

HERE      = os.path.dirname(os.path.abspath(__file__))
PORT      = int(settings.get("PORT", 8090))
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

PUBLIC_HOST = settings.get("PUBLIC_HOST") or _default_host()
STREMIO   = settings.get("STREMIO", f"http://{PUBLIC_HOST}:11470")
# The names this server answers to. A page on the public internet can point a
# hostname it owns at this box's private address -- DNS rebinding -- and from
# then on the browser treats it as same-origin, so _origin_ok() in routes.py sees
# Origin and Host agree and waves it through. Both of them say the attacker's
# name, which is the tell: the one thing the attack cannot fake is being
# addressed to a name this server actually has. An IP literal is always
# accepted, because the attack needs a name whose DNS it controls and cannot
# make a browser put someone else's address in Host.
HOST_ALLOW = tuple(h.strip().lower() for h in
                   settings.get("HOST_ALLOW", "").split(",") if h.strip())
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
ADB_TV    = settings.get("ADB_TV", settings.get("TV", ""))
ADB_ENABLED = bool(ADB_TV)
ADB       = settings.get("ADB", "/usr/bin/adb")
# The standalone VLC install this used to fall back to is gone from the TV --
# the Cinematica app is the only player left. The app registers itself over the
# heartbeat channel once it is in the foreground; adb's only remaining job is to
# bring it there.
PLAYER    = settings.get("PLAYER", "io.github.superthom196.cinematica/.MainActivity")
# How long a TV app counts as connected after its last heartbeat. Longer than
# the app's poll interval, so one lost request is not a disconnect; short enough
# that a TV switched off at the wall stops claiming the player within a poll of
# the phone's health check.
APP_TTL   = float(settings.get("APP_TTL", 15))
# How long to wait for the app to report that it is actually playing after being
# told to. Opening a 4K stream and filling the player's own buffer is not instant.
APP_HANDOFF_SECS = int(settings.get("APP_HANDOFF_SECS", 60))
# How long to wait for a woken app to show up on the heartbeat channel. adb's
# "am start" returns as soon as the activity is requested, well before the app
# has actually launched and made its first heartbeat.
APP_WAKE_SECS = int(settings.get("APP_WAKE_SECS", 20))
# How long a queued command is still worth delivering. Nothing else expires it:
# the app may be off, asleep or wedged, and an order that has sat unacked this
# long describes a world that no longer exists -- an app coming back an hour
# later must not start a film nobody is in the room for.
APP_CMD_TTL = float(settings.get("APP_CMD_TTL", 90))
# How long the last reported playback state is still believed after the app's
# heartbeat has gone stale. APP_TTL is deliberately one missed poll, which is
# nothing like "the film ended": without this a 15 s network blip made the
# phone's now-playing strip flap and made cache_watch() empty the cache in the
# middle of the film. Three missed polls before that is believed.
APP_GRACE = float(settings.get("APP_GRACE", 3 * APP_TTL))
PAGE      = int(settings.get("PAGE_SIZE", 20))    # films per infinite-scroll page
POOL_MAX  = int(settings.get("POOL_MAX", 600))   # candidate pool depth per view
CHANNEL_POLL_MIN = int(settings.get("CHANNEL_POLL_MIN", 30))  # how often followed channels are checked for uploads
# Beside server.py, like .env, netprofile.json and nowplaying.json. It was
# pinned to one absolute path under $HOME, which silently made any second
# checkout read and overwrite the first one's dataset.
TTL_LIST  = 6 * 3600                # popular list cache
TTL_STREAM= 3 * 3600                # per-film stream cache
TTL_FAIL  = int(settings.get("TTL_FAIL", 120))   # transport failures: retry soon
TTL_JOB   = 2 * 3600                # finished/abandoned play-job records
# caches never evicted otherwise grow forever -- _pool's key is a sort+genre
# combination an unauthenticated caller can enumerate at will.
MAX_POOL_ENTRIES   = int(settings.get("MAX_POOL_ENTRIES", 40))
MAX_STREAM_ENTRIES = int(settings.get("MAX_STREAM_ENTRIES", 2000))
MAX_JOB_ENTRIES    = int(settings.get("MAX_JOB_ENTRIES", 200))
# A catalogue's TV vote counts run much lower than film (a well-known show
# can sit under 500), so the pool's rating floor needs its own, lower knob.
TV_MIN_VOTES = int(settings.get("TV_MIN_VOTES", 200))
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
BIAS            = settings.flag("BIAS", True)
BIAS_LANG       = settings.get("BIAS_LANG", "en").strip().lower()   # ISO 639-1; "" = any
HOME_COUNTRIES  = _countries(settings.get("HOME_COUNTRIES", "GB"))
TV_BIG_MIN_VOTES    = int(settings.get("TV_BIG_MIN_VOTES", 1500))
MOVIE_BIG_MIN_VOTES = int(settings.get("MOVIE_BIG_MIN_VOTES", 3000))
WORLD_MIN_VOTES     = int(settings.get("WORLD_MIN_VOTES", 10000))   # any language; 0 = no such tier
HOME_W          = float(settings.get("HOME_W", 0.5))   # rating points added to a home title

# HEVC decoder tops out at 4096x2304 / 60 Mbps; there is NO AV1 decoder at all,
# and no transcode fallback anywhere, so an AV1 pick is simply a dead end.
MAX_GB_4K   = float(settings.get("MAX_GB_4K", 25))   # hard ceiling regardless
# What the VPN'd link actually sustains on a torrent swarm, measured: 0.9-3.6
# MB/s, typically ~1.5-1.8. A file whose bitrate exceeds this CANNOT be fixed by
# pre-buffering -- the buffer just drains at the difference and stalls. So the
# real fix is refusing to pick such files in the first place.
SUSTAIN_MBPS = float(settings.get("SUSTAIN_MBPS", 12))   # base budget, retuned by netcheck
# SUSTAIN_MBPS is a budget for TORRENT throughput over the VPN, which is nothing
# like a single TLS stream to a CDN: measured on this link, plain HTTP ran ~82
# Mbps while the value that actually works is 8. So the primary signal is what
# real swarms have delivered -- probe_and_buffer() already measures that for
# every film it buffers -- and the HTTP probe is used for sizing peer
# connections and as a sanity ceiling, never on its own for the budget.
NET_FILE     = os.path.join(HERE, "netprofile.json")
NET_SECS     = float(settings.get("NET_SECS", 8))        # seconds per endpoint
# Plain HTTP deliberately, not HTTPS. A flaky link can corrupt sustained TLS
# transfers -- the "bad record MAC" that makes docker pull restart every layer
# forever -- and an 8-second read is long enough to hit that every time,
# while a short curl succeeds. Measured: both endpoints fail over HTTPS and give
# ~85 Mbps over HTTP. A throughput probe carries nothing worth encrypting.
NET_URLS     = [u for u in settings.get("NET_URLS",
                "http://cachefly.cachefly.net/100mb.test,"
                "http://fsn1-speed.hetzner.com/100MB.bin").split(",") if u.strip()]
NET_TRIES    = int(settings.get("NET_TRIES", 2))   # this link drops transfers
# HTTP -> budget, used ONLY until three real streams have been measured. 0.10
# from the one link there is evidence for: 87 Mbps measured over HTTP against a
# hand-tuned budget of 8 that demonstrably works, i.e. ~0.09. Deliberately close
# to that rather than optimistic -- being too generous offers films the swarm
# cannot sustain, which is the failure this number exists to avoid.
NET_FRACTION = float(settings.get("NET_FRACTION", 0.10))
NET_SAMPLES  = int(settings.get("NET_SAMPLES", 20))       # swarm rates remembered
NET_PCT      = float(settings.get("NET_PCT", 0.25))       # percentile of those to trust
NET_MIN_OBS  = int(settings.get("NET_MIN_OBS", 3))        # ordinary swarms needed first
# First-run calibration: rather than waiting for three real films to go by --
# during which the budget is a guess and the first film is the one that matters
# -- measure real swarms up front, once.
CAL_FILMS    = int(settings.get("CAL_FILMS", 4))          # ordinary swarms to sample
CAL_SECS     = float(settings.get("CAL_SECS", 30))        # seconds of download each
CAL_MAX      = float(settings.get("CAL_MAX", 600))        # overall ceiling, 10 min
# Stremio keeps whole films so a resume does not re-download. That is not wanted
# here: the disk is worth more than the re-download, so the cache is capped and
# emptied when playback ends.
CACHE_GB     = float(settings.get("CACHE_GB", 30))
# Emptying on playback end needs the server to have seen the film play and stop,
# and a restart or an unseen player loses that edge: one install kept ten torrents
# overnight. So anything nothing has written to for this long is swept anyway.
CACHE_SWEEP_HOURS = float(settings.get("CACHE_SWEEP_HOURS", 4))
# How much to ask for per sample. Stremio buffers well past the requested range
# -- asking for BUFFER_MAX (1 GB) left every sampled film pulling a gigabyte in
# the background long after its 30s measurement had finished.
CAL_MB       = int(settings.get("CAL_MB", 128))
SUSTAIN_MIN, SUSTAIN_MAX = 4.0, 40.0
CONNS_MIN, CONNS_MAX     = 60, 180
WELL_SEEDED  = int(settings.get("WELL_SEEDED", 200))     # peers for the bonus
SEED_BONUS   = float(settings.get("SEED_BONUS", 1.5))    # extra budget when well seeded
# H.265 only. AV1 has no decoder on this panel, and H.264 needs far more bitrate
# for the same quality -- which the peer-starved swarm cannot deliver. Releases
# with no codec token in the name are rejected too: they cannot be verified.
HEVC_ONLY    = settings.flag("HEVC_ONLY", True)
# A torrent index's seeder counts are scraped and unreliable -- a "53 seeder"
# torrent can deliver nothing while a "34 seeder" one flies. There is no way
# to tell from metadata, so instead of guessing we PROBE each candidate and
# move on.
ATTEMPTS    = int(settings.get("ATTEMPTS", 5))     # candidates to try per film
DEAD_SECS   = int(settings.get("DEAD_SECS", 25))   # no bytes at all -> abandon
PROBE_SECS  = int(settings.get("PROBE_SECS", 35))  # too slow by now -> abandon
SLOW_RATIO  = float(settings.get("SLOW_RATIO", 0.7))
# When runtime_min is unknown, need_bps is 0 and the SLOW_RATIO check in jobs.py is
# disabled outright -- nothing stops a trickling swarm from buffering forever.
# This is the backstop: no candidate gets longer than this, full stop.
HARD_CAP_SECS = int(settings.get("HARD_CAP_SECS", 240))

# --- ordering -----------------------------------------------------------------
# Sorting purely by IMDb rating is permanently stuck in 1994. "balanced" adds a
# recency bonus that decays with age, so a well-reviewed recent film can out-rank
# a canonical classic without letting poorly-rated new releases in (they still
# have to clear the rating and vote floors).
RECENCY_W    = float(settings.get("RECENCY_W", 1.2))    # max IMDb-points bonus
RECENCY_SPAN = float(settings.get("RECENCY_SPAN", 20))  # years until bonus is 0
RECENT_YEARS = int(settings.get("RECENT_YEARS", 5))     # window for "recent"
MIN_RATING   = float(settings.get("MIN_RATING", 6.5))   # keeps the shit out
MIN_IMDB_VOTES = int(settings.get("MIN_IMDB_VOTES", 2000))
SORTS = ("top", "balanced", "recent")
# How many search candidates may be resolved before giving up looking for
# playable ones. Each resolution is a metadata + streams round trip, so this
# is the ceiling on how slow a fruitless search can get.
SEARCH_POOL = int(settings.get("SEARCH_POOL", 120))
MIN_SEEDERS = int(settings.get("MIN_SEEDERS", 20))   # "a decent amount of peers"
# Below these the release is a heavy re-encode: it still carries a 4K tag but
# will not look like one. Bitrate, not file size, is what decides that -- a 20 GB
# three-hour film is roughly a 13 GB two-hour one. Flagged rather than rejected,
# because it may be the only thing available today and a better release often
# appears in the swarm a day later.
LOW_MBPS_4K = float(settings.get("LOW_MBPS_4K", 5.0))
LOW_MBPS_HD = float(settings.get("LOW_MBPS_HD", 2.5))
RESOLVE_CHUNK = int(settings.get("RESOLVE_CHUNK", 20))  # candidates resolved per pass
RESOLVE_FIRST = int(settings.get("RESOLVE_FIRST", 10))  # ...fewer, for the first row
BUFFER_SECS = int(settings.get("BUFFER_SECS", 90))   # seconds of video to pre-load
BUFFER_MIN  = int(settings.get("BUFFER_MIN_MB", 40)) * 1048576
BUFFER_MAX  = int(settings.get("BUFFER_MAX_MB", 400)) * 1048576
# Matroska keeps its seek index (Cues/SeekHead) at the END of the file, and every
# player reads it the moment it opens the stream. Stremio downloads strictly
# sequentially, so that tail is never cached and the player sits on a spinner
# while those pieces are fetched out of order. Pre-fetch the tail too.
TAIL_MB     = int(settings.get("TAIL_MB", 8))
# The tail fetch runs BEFORE probe_and_buffer's guarded loop, so without its own
# ceiling it was bounded only by the socket timeout: a swarm trickling a few KB/s
# could sit in "fetching the seek index..." for ten minutes, per candidate.
TAIL_SECS   = int(settings.get("TAIL_SECS", 90))
# This TV plays AC3 cleanly but its VLC software-decodes AAC 5.1 and the audio
# clock drifts -- audible break-up while the video stays perfect. Measured at
# 21x realtime on this Pi, remuxing the SAME 4K video untouched and re-encoding
# only the audio to AC3 is nearly free, so AAC picks get piped through ffmpeg.
FFMPEG      = settings.get("FFMPEG", "/usr/lib/jellyfin-ffmpeg/ffmpeg")
FFMPEG_CTR  = settings.get("FFMPEG_CTR", "stremio-server")
AUDIO_FIX   = settings.flag("AUDIO_FIX", True)
# ffmpeg runs INSIDE the stremio container, which uses Docker's DNS and cannot
# resolve the host name -- it must reach the server on the container's own loopback.
STREMIO_IN  = settings.get("STREMIO_INTERNAL", "http://127.0.0.1:11470")
FFPROBE     = settings.get("FFPROBE", "/usr/lib/jellyfin-ffmpeg/ffprobe")
# Codecs this panel decodes in hardware. Anything else -- DTS, AAC multichannel,
# TrueHD -- gets software-decoded by VLC and the audio clock drifts. Guessing
# this from the release name does not work: "Shawshank [2160p x265 10bit FS97
# Joy]" carries no audio token at all and is actually DTS 5.1. So probe it.
NATIVE_AUDIO = tuple(settings.get("NATIVE_AUDIO", "ac3,eac3").split(","))
FOURK_ONLY  = settings.flag("FOURK_ONLY", True)
AUTOPLAY_NEXT = settings.flag("AUTOPLAY_NEXT", True)
# A film nobody in this house can follow is not a film. Two layers enforce it:
# the listing layer reads whatever languages the streams provider reports on
# the candidate itself, the file layer reads the audio tracks ffprobe finds
# in the actual file. PREF_LANG is an ISO 639-1 code -- ffprobe reports 639-2
# ("eng"), so LANG_TAGS in streams.py bridges the two.
PREF_LANG   = settings.get("PREF_LANG", "en")
REJECT_LANG = settings.flag("REJECT_LANG", True)
# Hardcoded ("burnt-in") subtitles are part of the picture: no player can turn
# them off, so an HC/KORSUB release is unwatchable here no matter how good the
# swarm is. Release names mark them reliably, which is why this one needs no
# file-level counterpart.
REJECT_HARDSUB = settings.flag("REJECT_HARDSUB", True)
# The bridge sidecar that owns the hi-fi DAC. Its own process, its own port --
# server.py never touches the device directly, it only tells the bridge what
# to play and reads back where the audio actually is.
SENDSPIN_BRIDGE = settings.get("SENDSPIN_BRIDGE", "http://127.0.0.1:8091")

# Hi-fi only makes sense once the bridge sidecar is around to receive it --
# this is the one flag that turns the whole bridge codepath on. The TV now
# picks which Sendspin player to use at runtime (GET /api/hifi/players), so
# a fixed client URL is no longer required -- the bridge's own presence is
# what matters.
SENDSPIN_CLIENT_URL = settings.get("SENDSPIN_CLIENT_URL") or None
SENDSPIN_ENABLED = settings.flag("SENDSPIN", True)
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
HIFI_AUDIO_DELAY_MS = int(settings.get("HIFI_AUDIO_DELAY_MS", "0"))
# How close to the end of the decoded track counts as the end of it. The last
# seconds of a film are the one place a stopped stream is not a fault, and a
# start this close to the end could not land anyway: the player will not take
# a first chunk less than its send-ahead floor (~1 s) from now.
HIFI_TRACK_END_S = 2.0

# Kept here with the rest, so every setting the server reads is in one place.
# Probing saturates the link by design, so any uncached catalogue call made
# while it runs times out -- which showed up as a completely blank film
# list. It waits for the app to be genuinely idle instead of merely waiting
# its turn at startup.
IDLE_SECS = float(settings.get("NET_IDLE_SECS", 45))
IDLE_WAIT = float(settings.get("NET_IDLE_WAIT", 1800))

# CINEMATICA_SHELF exists for the test suite, which drives real heartbeats
# through this module: without somewhere else to point it, a test run on the Pi
# would write "Stub Movie 1" into the household's actual watch history.
SHELF_FILE = settings.get("CINEMATICA_SHELF") or os.path.join(HERE, "shelf.json")

MAX_TRANSCODES = int(settings.get("MAX_TRANSCODES", 2))

# The transcoder was a live pipe started when the player connected, so it raced
# from cold exactly when Stremio was still downloading hard and VLC had an empty
# buffer -- about a minute of glitching every time. It now writes to a file
# during the buffer phase and gets a head start before the player ever connects.
TC_HOST  = os.path.join(HERE, "transcode")          # host side of the bind mount
TC_CTR   = settings.get("TC_CTR", "/transcode")   # same dir inside the container
# Built at full speed, so this costs seconds of wall time, not TC_HEAD seconds.
TC_HEAD  = int(settings.get("TRANSCODE_HEAD_SECS", 60))
# How far ahead of playback the regulator keeps the conversion once it starts.
TC_LEAD  = int(settings.get("TRANSCODE_LEAD_SECS", 180))
# Resume once the lead has drained to this fraction of TC_LEAD. Was a hardcoded
# 0.6, which meant ~72s of playback had to drain before ffmpeg was let go again
# -- so it ran in ~27s sprints separated by ~72s of SIGSTOP, with its HTTP
# connection to Stremio sitting idle throughout. A narrow band stop-starts more
# often but never leaves the input idle long enough to go stale.
TC_BAND  = float(settings.get("TRANSCODE_LEAD_BAND", 0.9))
TC_KEEP  = int(settings.get("TRANSCODE_KEEP", 0))   # cache is emptied per film, so keep none

# The on-demand browser HLS packager: one ffmpeg per session, remuxing the
# source into fMP4 segments a grid-index at a time, paced against the real
# playhead instead of an estimate. See regulate_hls / bx_spawn in browser_session.py.
BX_DIR      = "bx_"                    # per-session directory prefix under TC_HOST
BX_SEG_WAIT = float(settings.get("BX_SEG_WAIT", 45))   # long-poll ceiling per segment
BX_LEAD     = float(settings.get("BX_LEAD", 300))      # seconds ahead of the playhead
BX_BEHIND   = float(settings.get("BX_BEHIND", 600))    # seconds kept behind it
BX_SEEK_DEBOUNCE = float(settings.get("BX_SEEK_DEBOUNCE", 0.25))
BX_LOOKAHEAD = int(settings.get("BX_LOOKAHEAD", 8))    # segments past the frontier that just wait

JOB_STALE  = int(settings.get("JOB_STALE", 120))  # a play job not updated this long: its worker is gone

BX_IDLE = float(settings.get("BX_IDLE", 60))   # a browser session with no heartbeat this long has stopped playing

# No service credentials here any more. A catalogue key or a stream-index
# config string belongs to the provider that needs it, is entered in the
# browser, and is stored outside this directory. An old provider API key
# left in a .env is never read: it must not silently switch a service on.

# Cloudflare 403s the default "Python-urllib/x.y" agent outright, so every
# request has to carry a normal-looking UA. Found the hard way.
UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")
