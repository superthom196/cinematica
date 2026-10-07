"""What the link can sustain: the measured budget, the download limits set
from the Settings page, and the calibration that measures the link.
"""
import json, os, random, time, urllib.request
from providers import contract, gateway

import config, core, disk, streams, catalogue, torrents, tvlink

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
        budget = config.SUSTAIN_MBPS
    else:
        budget = config.SUSTAIN_MBPS * (config.SEED_BONUS if seeders >= config.WELL_SEEDED else 1.0)
    lid = cap_mbps()
    return min(budget, lid) if lid else budget

# The budget only asks whether the link can keep up, so a fast link waves
# through anything: a 30 Mbps budget passes a 20 GB two-hour film, which a
# small box's cache cannot even hold. Two lids, kept in netprofile.json beside
# the measurements (a recalibration only resets the samples, so they survive
# it); 0 or absent means no lid:
#   cap_mbps -- ceiling on the budget, well-seeded bonus included. Bounds the
#               bitrate, so the size still scales with the film's length.
#   cap_gb   -- the largest file ever picked, however long the film.
# A file must fit in the cache as well, lid or no lid -- and the cache is sized
# by the disk it sits on (disk.cache_gb), not just by CACHE_GB.
def cap_mbps():
    return float(_net.get("cap_mbps") or 0)

def max_gb():
    lid = float(_net.get("cap_gb") or 0)
    return min([x for x in (config.MAX_GB_4K, lid) if x > 0] + [disk.cache_gb()])

def limits():
    return {"cap_mbps": cap_mbps() or None, "cap_gb": float(_net.get("cap_gb") or 0) or None,
            "max_gb": max_gb(), "cache_gb": disk.cache_gb()}

def set_limits(cap_mbps_v, cap_gb_v):
    """Store both lids and drop every pick made under the old ones: the stream
    cache holds a chosen file for hours and the grid's pools carry those picks,
    so without this a new lid would not reach a film until its entry expired."""
    with core._lock:
        _net["cap_mbps"] = cap_mbps_v or None
        _net["cap_gb"] = cap_gb_v or None
        snap = dict(_net)
        streams._streams.clear()
        catalogue._pool.clear()
    net_save(snap)
    print("limits: speed %s Mbps, size %s GB (picking up to %g GB)"
          % (cap_mbps_v or "no lid", cap_gb_v or "no lid", max_gb()), flush=True)
    return limits()

def net_load():
    try:
        with open(config.NET_FILE) as f:
            d = json.load(f)
    except Exception:
        return {}
    # Early samples were bare floats with no peer count. A rate cannot be
    # interpreted without one, so they are dropped rather than guessed at.
    d["samples"] = [x for x in (d.get("samples") or []) if isinstance(x, dict)]
    return d

def net_save(d):
    try:
        tmp = config.NET_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, config.NET_FILE)
    except Exception as ex:
        # Not fatal -- the measurements are still in memory -- but a profile
        # that never reaches the disk is re-measured after every restart.
        print("net: could not save %s: %s" % (config.NET_FILE, ex), flush=True)

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
    with core._lock:
        s = _net.setdefault("samples", [])
        s.append({"mbps": round(mbps, 2), "seeders": int(seeders or 0),
                  "at": int(time.time())})
        del s[:-config.NET_SAMPLES]
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
    with core._lock:
        raw = list(_net.get("samples") or [])
    return sorted(x["mbps"] for x in raw
                  if isinstance(x, dict) and x.get("seeders", 0) < config.WELL_SEEDED)

def sustain_from_samples():
    """A low percentile of ordinary-swarm throughput: a rate most films can meet,
    not the best one ever managed."""
    s = ordinary_samples()
    if len(s) < config.NET_MIN_OBS:
        return None
    return s[min(len(s) - 1, int(len(s) * config.NET_PCT))]

def link_probe():
    """Sustained single-stream HTTP throughput, in Mbps. Best of the endpoints,
    so one slow CDN cannot understate the link."""
    best = 0.0
    for url in config.NET_URLS:
        if tvlink.playing_now():
            print("netcheck: abandoned, playback started", flush=True)
            return best
        host = url.split("/")[2]
        for attempt in range(1, config.NET_TRIES + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": config.UA})
                t0 = time.time(); got = 0
                with urllib.request.urlopen(req, timeout=20) as r:
                    while time.time() - t0 < config.NET_SECS:
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
        req = urllib.request.Request(config.STREMIO_IN + "/settings",
                                     data=json.dumps(vals).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print("netcheck: could not set %s: %s" % (list(vals), e), flush=True)
        return False

def net_apply(quiet=False):
    """Push the current profile into the running config."""
    mbps = _net.get("mbps")
    conns = _net.get("conns")
    if mbps:
        config.SUSTAIN_MBPS = float(mbps)
    if conns:
        stremio_set(btMaxConnections=int(conns))
    if not quiet:
        print("netcheck: applied sustain=%s Mbps conns=%s (%s)"
              % (mbps, conns, _net.get("source")), flush=True)

_net_busy = {"on": False, "since": 0.0}
_cal = {"on": False, "done": 0, "want": 0, "msg": ""}
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
    return idle_for() < config.IDLE_SECS

def wait_until_idle(limit=config.IDLE_WAIT):
    """Hold off until nobody has touched the app for IDLE_SECS."""
    t0 = time.time()
    while time.time() - t0 < limit:
        if tvlink.playing_now():
            return False
        if idle_for() > config.IDLE_SECS:
            return True
        time.sleep(5)
    return False

def sample_swarm(pick, secs=None):
    """Download from one real swarm for a few seconds and return bytes/sec.

    The same measurement probe_and_buffer() makes during playback, done
    deliberately rather than waiting for it to happen.
    """
    url = streams.stream_url(pick)
    secs = secs or config.CAL_SECS
    try:
        # From the start, which is what a player reads and what peers prioritise.
        # It is uncached because cached torrents are skipped before we get here --
        # reading cached data measured the NVMe, not the link, and reported
        # 269 Mbps on an 87 Mbps connection.
        req = urllib.request.Request(url,
                headers={"Range": "bytes=0-%d" % (config.CAL_MB * 1048576 - 1),
                         "User-Agent": config.UA})
        t0 = time.time(); got = 0
        with urllib.request.urlopen(req, timeout=config.DEAD_SECS + 10) as r:
            while time.time() - t0 < secs:
                c = r.read1(262144)
                if not c:
                    break
                got += len(c)
                if got == 0 and time.time() - t0 > config.DEAD_SECS:
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
    _cal.update(on=True, done=0, want=config.CAL_FILMS, msg="Finding films to test\u2026")
    try:
        # Deliberately NOT get_page(): that holds the browse pool's lock for the
        # whole resolve, and calibration takes minutes -- the phone's grid needs
        # the same lock and simply stopped loading until this finished. Resolve
        # a private handful instead; get_stream() only takes the shared lock in
        # short bursts and its results are cached for the grid anyway.
        cands = []
        held = torrents.cached_hashes()
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
            if len(cands) >= config.CAL_FILMS or time.time() - t0 > config.CAL_MAX / 2:
                break
            if tvlink.playing_now():
                _cal["msg"] = "Paused — you started watching"
                return
            _cal["msg"] = "Finding films to test (%d of %d)\u2026" % (i + 1, len(entries))
            try:
                pk = streams.get_stream(ent["id"], entry=ent).get("pick")
            except Exception:
                continue
            if not pk or not (config.MIN_SEEDERS <= (pk.get("seeders") or 0) < config.WELL_SEEDED):
                continue
            if pk.get("infoHash") in held:
                continue          # already on disk: would measure the NVMe
            cands.append(pk)
        if not cands:
            _cal["msg"] = "No ordinary swarms available to measure"
            print("calibrate: nothing with %d..%d seeders to sample"
                  % (config.MIN_SEEDERS, config.WELL_SEEDED), flush=True)
            return
        for pk in cands:
            if _cal["done"] >= config.CAL_FILMS or time.time() - t0 > config.CAL_MAX:
                break
            if tvlink.playing_now():
                # A film wins. Browsing does not: the page is held behind the
                # calibration overlay while this runs, so "in use" cannot happen
                # -- and aborting on it meant calibration never finished at all.
                _cal["msg"] = "Paused — you started watching"
                print("calibrate: abandoned, playback started", flush=True)
                break
            _cal["msg"] = ("Measuring swarm %d of %d\u2026"
                           % (_cal["done"] + 1, config.CAL_FILMS))
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
            torrents.cache_clear()
        except Exception as ex:
            print("calibrate: could not empty the cache: %s" % ex, flush=True)

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
        mbps, source = http_mbps * config.NET_FRACTION, "estimated"
    else:
        mbps, source = config.SUSTAIN_MBPS, "unchanged"
    if http_mbps and source == "estimated":
        # Cap a guess at the link, but never a measurement: observed rates ARE
        # real throughput, and the probe can read low if it ran while torrents
        # were active. A measurement beats an inference about the same thing.
        mbps = min(mbps, http_mbps * 0.8)
    mbps = round(max(config.SUSTAIN_MIN, min(config.SUSTAIN_MAX, mbps)), 1)
    conns = int(max(config.CONNS_MIN, min(config.CONNS_MAX, 60 + http_mbps))) if http_mbps \
            else int(_net.get("conns") or 90)
    # Computed BEFORE the lock: ordinary_samples() takes _lock itself, so calling
    # it here acquired the same lock twice in one thread. Against the plain Lock
    # this was in place with, that self-deadlocked on the first derive and every
    # other thread then queued behind it -- a 22-thread pile-up and a dead server.
    n_ord = len(ordinary_samples())
    with core._lock:
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
        with core._lock:
            dropped = len(_net.get("samples") or [])
            _net["samples"] = []
        if dropped:
            print("netcheck: recalibrating from scratch, %d old sample(s) discarded"
                  % dropped, flush=True)
    net_check()
    if (force or len(ordinary_samples()) < config.NET_MIN_OBS) and not tvlink.playing_now():
        calibrate()
        net_retune()
    return dict(_net)

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
        if have >= config.NET_MIN_OBS:
            print("netcheck: already calibrated from %d ordinary swarms, "
                  "nothing to do" % have, flush=True)
            return
        if tvlink.playing_now():
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
