"""The torrent cache: its size limit, the sweep that empties it, and the
prefetch that warms it.
"""
import json, re, subprocess, time, urllib.request
from providers import contract

import config, disk, netprofile, catalogue, transcode, sendspin, tvlink, jobs, browser_session, nowplaying

def cached_hashes():
    """Torrents Stremio already holds. Sampling one would measure the disk."""
    try:
        r = subprocess.run(["docker", "exec", config.FFMPEG_CTR, "sh", "-c",
                            "ls /stremio-server/stremio-cache 2>/dev/null"],
                           capture_output=True, text=True, timeout=30)
        return {d for d in (r.stdout or "").split() if contract.RE_HASH40.match(d)}
    except Exception:
        return set()

def cache_size_apply():
    """Cap Stremio's cache, retrying while its container is still coming up.

    This runs in its own thread at boot, which on a reboot is well before the
    stremio container has bound its port: the single attempt it used to make
    died with a broken pipe and the cap was then simply never applied for the
    whole life of the service. Retry, stop at the first success, and say so once
    if it never takes.
    """
    gb    = disk.cache_gb()
    tries = 10
    gap   = 15
    for attempt in range(1, tries + 1):
        if netprofile.stremio_set(cacheSize=int(gb * 1024 ** 3)):
            _applied["gb"] = gb
            print("cache: capped at %g GB" % gb, flush=True)
            return
        if attempt < tries:
            time.sleep(gap)
    print("cache: could not cap at %g GB after %d attempts (%ds), leaving "
          "Stremio's own setting alone" % (gb, tries, tries * gap), flush=True)

# The cap last handed to Stremio. The disk's room changes as other things fill
# or free it, so cache_watch() re-applies it whenever it drifts by a gigabyte.
_applied = {"gb": None}

def hashes_in_use():
    """Torrents a player or a play job is on right now, lowercased: every job
    still working, the newest one handed to a player, a browser session's
    source, and what nowplaying.json says was last played -- the one record
    that survives a restart mid-film, when the jobs table starts out empty."""
    out = set(jobs.hashes_in_use())
    for src in (browser_session._bx.get("src"), nowplaying._now.get("hash"),
                nowplaying._now.get("hifi_src")):
        m = re.search(r"\b([0-9a-fA-F]{40})\b", src or "")
        if m:
            out.add(m.group(1).lower())
    return out

def cache_clear(older_than=None, keep=None, label=None):
    """Empty Stremio's torrent cache, skipping anything still in use.

    Deliberate: Stremio hoards whole films so a resume is instant, which meant
    18 GB across 21 titles already watched. The disk is worth more than the
    re-download.

    With older_than (seconds), only torrents nothing has written to for that
    long go: the sweep for films whose end was never seen. It goes by writes,
    not by reads, so a film still being watched off a player this server
    cannot see keeps its torrent as long as Stremio is still fetching it.

    With keep (a set of lowercased hashes), the disk is out of room: only
    those survive, and an open engine on anything else is released and
    deleted even mid-film -- the episode before this one, say, which Stremio
    holds open long after autoplay moved on."""
    try:
        raw = urllib.request.urlopen(config.STREMIO_IN + "/stats.json", timeout=10).read()
        active = set(json.loads(raw or "{}"))
    except Exception:
        active = set()
    try:
        r = subprocess.run(["docker", "exec", config.FFMPEG_CTR, "sh", "-c",
                            "ls /stremio-server/stremio-cache 2>/dev/null"],
                           capture_output=True, text=True, timeout=30)
        # ls's own complaint is silenced (no cache dir is just an empty cache),
        # so anything on stderr is docker's: the container is down or out of reach.
        if r.returncode and (r.stderr or "").strip():
            print("cache: could not list:", r.stderr.strip(), flush=True)
            return
        dirs = [d for d in (r.stdout or "").split() if contract.RE_HASH40.match(d)]
        if older_than is not None and dirs:
            r = subprocess.run(["docker", "exec", config.FFMPEG_CTR, "find",
                                "/stremio-server/stremio-cache", "-mindepth", "1",
                                "-mmin", "-%d" % max(1, older_than // 60)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode:
                print("cache: could not date:", (r.stderr or "").strip(), flush=True)
                return
            fresh = {line.split("/")[3] for line in r.stdout.splitlines()
                     if line.count("/") >= 3}
            dirs = [d for d in dirs if d not in fresh]
    except Exception as e:
        print("cache: could not list:", e, flush=True)
        return
    freed = kept = 0
    for h in dirs:
        if keep is not None and h.lower() in keep:
            kept += 1
            continue
        if h in active:
            if keep is None and tvlink.playing_now():
                kept += 1
                continue                  # still streaming: leave it alone
            # Stremio keeps an engine open long after its last reader has
            # gone -- a stopped conversion, a calibration sample -- and an
            # open engine kept its files from ever being cleared. Nothing is
            # playing, so ask Stremio to drop it (verified: GET /{hash}/remove
            # answers {} and the hash leaves stats.json at once).
            try:
                urllib.request.urlopen(config.STREMIO_IN + "/" + h + "/remove", timeout=10).read()
            except Exception as e:
                print("cache: could not release %s: %s" % (h[:8], e), flush=True)
                continue
        try:
            r = subprocess.run(["docker", "exec", config.FFMPEG_CTR, "rm", "-rf",
                                "/stremio-server/stremio-cache/" + h],
                               capture_output=True, text=True, timeout=60)
            if r.returncode:
                print("cache: could not delete %s: %s" % (h[:8], (r.stderr or "").strip()),
                      flush=True)
                continue
            freed += 1
        except Exception as e:
            print("cache: could not delete %s: %s" % (h[:8], e), flush=True)
    if freed:
        print("cache: %s %d torrent(s)%s"
              % (label or ("swept" if older_than is not None else
                           "freed disk:" if keep is not None else "cleared"), freed,
                 ", kept %d still streaming" % kept if kept else ""), flush=True)

def cache_watch():
    """Clear the cache when a film finishes, on the playing -> idle edge.

    Debounced to 3 consecutive idle samples (60s): even with the buffering
    fix in tv_playback_state(), a missed heartbeat can still read as idle for
    a beat, and firing on the very first one tore down a film still playing.

    That edge lives in this process only, so a restart mid-film or a player
    the server never saw loses it. Two minutes after start, and every half
    hour after that, anything untouched for CACHE_SWEEP_HOURS goes as well.
    """
    was = False
    idle_count = 0
    next_sweep = time.time() + 120
    last_err = None
    while True:
        time.sleep(20)
        try:
            if time.time() >= next_sweep:
                next_sweep = time.time() + 1800
                cache_clear(older_than=int(config.CACHE_SWEEP_HOURS * 3600))
            disk_pressure()
            # A browser watching counts as playing too, or the cache gets torn
            # down out from under a film someone is watching in the browser.
            now = tvlink.tv_playback_state() in (2, 3) or browser_session.browser_playing()
            if now:
                idle_count = 0
            elif was or idle_count:
                idle_count += 1
            if idle_count == 3:
                amid, _aj = jobs.active_job()
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
                    transcode.transcode_stop_all()
                    if config.SENDSPIN_ENABLED:
                        sendspin._hifi_release()
                    cache_clear()
                    idle_count = 0
            was = now
            last_err = None
        except Exception as e:
            # Once per distinct failure: this loop swallowing every error is
            # how a cache could stay full with nothing in the log.
            if repr(e) != last_err:
                last_err = repr(e)
                print("cache: watch failed:", last_err, flush=True)

def keep_only(info_hash):
    """A film has just reached its player: every other torrent goes now.

    Mid-film the only torrent worth its disk is the one being watched. Before
    this, the episode before it, and every candidate tried and dropped on the
    way here, sat in the cache -- open in Stremio, so even the four-hour sweep
    left them -- until playback ended, which autoplay never lets happen. Kept
    as well: anything a newer play is still buffering. A direct (non-torrent)
    source has no hash, so every torrent goes."""
    keep = set(jobs.hashes_in_use(playing=False))
    h = (info_hash or "").lower()
    if contract.RE_HASH40.match(h):
        keep.add(h)
    cache_clear(keep=keep, label="new film playing, cleared")
    disk.measure(fresh=True)

def disk_pressure():
    """Out of room mid-film: keep what is in use, delete everything else now.

    Clearing on the playing -> idle edge never fires during a binge, since
    autoplay never lets playback go idle, so every episode's torrent stayed
    until the disk filled and the film stopped. When nothing in use is known
    but something is playing (a restart with no nowplaying hash), the plain
    clear's rule applies instead: open engines are kept while anything plays."""
    gb = disk.cache_gb()
    if _applied["gb"] is not None and abs(gb - _applied["gb"]) >= 1:
        if netprofile.stremio_set(cacheSize=int(gb * 1024 ** 3)):
            print("cache: re-capped at %g GB (was %g)" % (gb, _applied["gb"]), flush=True)
            _applied["gb"] = gb
    if not disk.over():
        return
    keep = hashes_in_use()
    v = disk.measure() or {}
    print("cache: %.1f GB held, %.1f GB free, room for %.1f GB -- freeing everything but %d in use"
          % (v.get("held_gb", 0), v.get("free_gb", 0), v.get("cache_gb", 0), len(keep)), flush=True)
    if keep or not tvlink.playing_now():
        cache_clear(keep=keep)
    else:
        cache_clear()
    disk.measure(fresh=True)

def prefetch():
    try:
        catalogue.get_genres()
        catalogue.get_page(None, 0, config.PAGE)     # warm the first page
    except Exception as e:
        print("prefetch failed:", e, flush=True)
    # Only now assess the link. Both the HTTP probe and the swarm sampling
    # saturate the connection for minutes, and starting them first starved every
    # catalogue call behind them: genres timed out, the page's init sequence died
    # with it, and the interface came up completely empty. Measuring after the app is
    # warm means the grid is already usable and served from cache while it runs.
    netprofile.net_auto()
