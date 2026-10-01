"""Play jobs: one at a time, from the request through buffering and
probing each candidate to the handoff, including autoplay of the next
episode.
"""
import threading, time, urllib.request

import config, core, nowplaying, mediaprobe, streams, netprofile, catalogue, transcode, sendspin, tvlink, browser_session, watching

def _remember_next_episode(jobid):
    """Store an autoplay episode's successor on its job while it plays. Off the
    request thread: crossing into a new season can be an uncached provider
    call, and nothing about starting this episode should wait for it."""
    try:
        tid, s, e = catalogue.tv_job_parts(jobid)
        job_set(jobid, next_ep=catalogue._next_episode_info(tid, s, e))
    except Exception as ex:
        print("autoplay: could not look ahead from %s: %s" % (jobid, ex), flush=True)

def _autoplay_start(nid, resolve):
    """start_play() for an autoplay-next, retiring the placeholder job the
    heartbeat registered if the play never got going -- no stream, cancelled,
    or a lookup that raised -- so the app's countdown is told rather than
    left polling a job that will never move."""
    try:
        code, body = start_play(nid, resolve, autoplay=True)
        msg = None if code == 202 else (body.get("msg") or "No stream found")
    except Exception as ex:
        msg = "Could not start the next episode: %s" % ex
    if msg and (job_get(nid) or {}).get("stage") == "starting":
        job_set(nid, stage="error", ok=False, msg=msg)
        print("autoplay: %s did not start: %s" % (nid, msg), flush=True)

# Stremio downloads sequentially on demand, so VLC starts playing while the
# engine is still finding peers -- which is exactly the first ~30s of stutter.
# Pulling a head start into the cache ourselves before launching the player
# removes it.
_jobs = {}      # movie id -> progress dict

def job_get(mid):
    with core._lock:
        return dict(_jobs.get(str(mid)) or {})

# One play job at a time. Two in parallel raced everything downstream: two
# buffers competing for the same constrained link, two ffmpegs writing a single
# output path, and -- the one that actually bit -- whichever job finished
# probing LAST would force-stop the other film and take the TV, minutes after
# the user had given up on it.
JOB_ACTIVE = ("starting", "buffering", "encoding", "launching")
_play_gen  = 0        # bumped per accepted play; an older job stands down
# A play request has no job to cancel while it is still resolving streams --
# get_stream() can block for seconds before job_set() ever runs -- so a
# cancel landing in that window has to reach it some other way than
# active_job(). _cancel_gen counts accepted cancels; _play_inflight counts
# play requests currently between entry and job_set().
_cancel_gen = 0
_play_inflight = 0

def active_job():
    """(mid, job) of the play job still working, if any."""
    now = time.time()
    with core._lock:
        for k, j in _jobs.items():
            if j.get("stage") in JOB_ACTIVE and now - j.get("at", 0) < config.JOB_STALE:
                return k, dict(j)
    return None, None

def play_claim():
    """Claim the TV. Anything older sees superseded() and stands down -- the
    belt to active_job()'s braces, for a job that went stale while still alive."""
    global _play_gen
    with core._lock:
        _play_gen += 1
        return _play_gen

def superseded(gen):
    with core._lock:
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
        if browser_session.browser_playing():
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
        # preparing has no _bx session yet, so browser_playing() (browser_session.py) reads
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
    if tvlink.tv_playback_state() in (2, 3):
        return False, "Another device is playing"
    if browser_session.browser_playing():
        with core._lock:
            same = token is not None and browser_session._bx["token"] == token
        if not same:
            return False, "Another device is playing"
    return True, None

def job_set(mid, **kw):
    with core._lock:
        key = str(mid)
        is_new = key not in _jobs
        j = _jobs.setdefault(key, {})
        j.update(kw)
        j["at"] = time.time()
        if is_new:               # only worth scanning the whole dict on growth
            core._evict(_jobs, config.TTL_JOB, config.MAX_JOB_ENTRIES)

def buffer_target(pick, runtime_min):
    """Bytes needed for BUFFER_SECS of playback, from the release's own bitrate."""
    gb = pick.get("gb") or 0
    total = gb * 1024 * 1024 * 1024
    secs = (runtime_min or 0) * 60
    if total > 0 and secs > 0:
        want = int(total / secs * config.BUFFER_SECS)
    else:
        want = config.BUFFER_MIN
    return max(config.BUFFER_MIN, min(config.BUFFER_MAX, want, int(total) if total else config.BUFFER_MAX))

def fetch_tail(url, mid, label):
    """Pull the last TAIL_MB so the player's index read is already cached.

    Returns (ok, size) -- size is the real byte count read off the head
    request's Content-Range header, or None if it could not be read. That size
    is the release's true file size, which a candidate's own listing can get
    wrong (packs especially), so callers use it to correct the estimate."""
    size = None
    try:
        head = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": config.UA})
        with urllib.request.urlopen(head, timeout=45) as r:
            cr = r.headers.get("Content-Range") or ""
        size = int(cr.split("/")[-1]) if "/" in cr else None
        if not size or size <= 0:
            return False, size
        start = max(0, size - config.TAIL_MB * 1048576)
        job_set(mid, msg="%s — fetching the seek index…" % label)
        req = urllib.request.Request(url, headers={"Range": "bytes=%d-%d" % (start, size - 1),
                                                   "User-Agent": config.UA})
        got = 0
        t0 = time.time()
        # DEAD_SECS + 10 to match probe_and_buffer: a swarm that never sends a
        # byte is capped by the socket timeout, so 180s here meant a truly dead
        # candidate cost three minutes before the fast-abandon logic even ran.
        with urllib.request.urlopen(req, timeout=config.DEAD_SECS + 10) as r:
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
                if got == 0 and el > config.DEAD_SECS:
                    return False, size
                if el > config.TAIL_SECS:
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
    url = streams.stream_url(pick)
    target = buffer_target(pick, runtime_min)
    req = netprofile.required_mbps(pick.get("gb"), runtime_min)
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
        req = netprofile.required_mbps(pick.get("gb"), runtime_min)
        need_bps = (req / 8.0) * 1048576 if req else 0
    t0 = time.time(); got = 0; grew = False
    try:
        r_h = urllib.request.Request(url, headers={"Range": f"bytes=0-{config.BUFFER_MAX-1}",
                                                   "User-Agent": config.UA})
        with urllib.request.urlopen(r_h, timeout=config.DEAD_SECS + 10) as r:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                if got == 0 and time.time() - t0 > config.DEAD_SECS:
                    # still nothing at all after DEAD_SECS -- checked BEFORE
                    # folding this chunk in, otherwise got is never 0 here
                    return False, 0, 0.0
                got += len(chunk)
                el = max(time.time() - t0, 0.001)
                rate = got / el
                if superseded(gen):
                    return False, got, rate          # a newer play owns the TV now
                if el > config.HARD_CAP_SECS:
                    return False, got, rate          # absolute ceiling: see HARD_CAP_SECS
                if need_bps and el > config.PROBE_SECS and rate < need_bps * config.SLOW_RATIO:
                    return False, got, rate          # too slow, try the next one
                if need_bps and rate < need_bps and not grew and target < config.BUFFER_MAX and got > target * 0.6:
                    target = min(config.BUFFER_MAX, int(target * 1.8)); grew = True
                job_set(mid, got=got, target=target, speed=rate,
                        pct=min(99, int(got * 100 / target)),
                        msg="Trying %d/%d: %s — %d/%d MB at %.1f Mbps%s" % (
                            attempt, total, label, got // 1048576, target // 1048576,
                            rate * 8 / 1048576,
                            "  (slow — deeper cushion)" if grew else ""))
                if got >= target:
                    el = max(time.time() - t0, 0.001)
                    netprofile.record_rate(got / el, pick.get("seeders"))
                    return True, got, got / el
    except Exception:
        pass
    el = max(time.time() - t0, 0.001)
    return (got >= config.BUFFER_MIN), got, got / el

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
    internal = streams.stream_url_internal(pick)
    acodec, adur, alangs, acodecs = mediaprobe.probe_media(internal)
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
    if config.REJECT_LANG and alangs and not streams.audio_has_lang(alangs, config.PREF_LANG):
        job_set(mid, msg="Candidate %d/%d has no %s audio — trying the next…"
                         % (i, total, streams.LANG_NAME.get(config.PREF_LANG, config.PREF_LANG)))
        return None
    # transcode_begin keeps whichever track passed the language check,
    # not track 0, so needs_fix has to be judged on that same track.
    aidx = streams.audio_track_for(alangs, config.PREF_LANG) if config.REJECT_LANG else 0
    if acodecs and aidx < len(acodecs) and acodecs[aidx]:
        acodec = acodecs[aidx]
    return {"internal": internal, "acodec": acodec, "adur": adur, "alangs": alangs,
            "acodecs": acodecs, "aidx": aidx, "got": got, "rate": rate}

def _stand_down(mid, gen):
    """True, with the job marked superseded, once a newer play has claimed the
    player: the check both play workers make between every step."""
    if superseded(gen):
        job_set(mid, stage="error", ok=False,
                msg="Superseded by a newer play request")
        return True
    return False

def run_play_job(mid, picks, runtime_min, title=None, gen=None):
    """Work down the ranked candidates until one actually streams."""
    try:
        picks = [p for p in (picks or []) if p]
        total = min(len(picks), config.ATTEMPTS)
        tried = []
        for i, pick in enumerate(picks[:config.ATTEMPTS], start=1):
            if _stand_down(mid, gen):
                return
            prep = prepare_candidate(mid, pick, runtime_min, i, total, gen, tried)
            if prep is None:
                if _stand_down(mid, gen):
                    return
                continue
            internal = prep["internal"]
            acodec, adur = prep["acodec"], prep["adur"]
            alangs, aidx = prep["alangs"], prep["aidx"]
            got, rate = prep["got"], prep["rate"]
            fidx = pick.get("fileIdx")
            needs_fix = bool(config.AUDIO_FIX and acodec and acodec not in config.NATIVE_AUDIO)
            if sendspin._hifi["on"] and config.SENDSPIN_ENABLED:
                # hifi: the bridge/DAC plays the audio straight off the
                # source file, so the TV only ever shows picture -- the AC3
                # conversion this release would otherwise need never happens.
                print("hifi: skipping AC3 conversion, TV plays video only", flush=True)
                needs_fix = False
            pick["audio_actual"] = acodec or "?"
            pick["audio_langs"] = alangs
            pick["audio_track"] = aidx
            pick["transcoded"] = needs_fix
            with core._lock:
                if sendspin._hifi["on"] and config.SENDSPIN_ENABLED:
                    # The first "playing" heartbeat starts the bridge stream,
                    # not this job -- it has the one clock (server monotonic)
                    # and the TV's own reported position to line the start up
                    # against. A new source retires the old timeline now, so
                    # no verdict is ever computed against the previous film.
                    sendspin._hifi["src"] = internal
                    sendspin._hifi["aidx"] = aidx
                    sendspin._hifi["film_centre"] = sendspin._hifi["centre"]
                    sendspin._hifi["job"] = str(mid)
                else:
                    sendspin._hifi["src"] = None
                    sendspin._hifi["job"] = None
                sendspin._hifi_invalidate()
                sendspin._hifi["fail_count"] = 0
                sendspin._hifi["last_error"] = None
                if sendspin._hifi["src"]:
                    # Decode the audio while the film pre-buffers, so it is
                    # sitting in the bridge's cache before the TV shows a frame.
                    sendspin._ss_q.put(("prepare", internal, aidx, sendspin._hifi["gen"]))
            # Committed to this candidate, so every earlier conversion is dead
            # weight and must go before we start ours. No keep= exception for the
            # same infoHash: transcode_begin unlinks the output and starts fresh
            # regardless, so "keeping" that one only meant a replay of the film
            # already playing left the old ffmpeg alive, writing to an inode that
            # had just been deleted, while a second one wrote the real file.
            transcode.transcode_stop_all()
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
                name = transcode.transcode_begin(transcode.tc_key(pick), fidx, internal, bps, mid, gen, aidx=aidx)
                if not name:
                    job_set(mid, msg="Audio conversion failed — playing as-is")
                    pick["transcoded"] = False
                    url = streams.stream_url(pick)
                else:
                    url = transcode.audio_url(pick)
            else:
                url = streams.stream_url(pick)
            job_set(mid, stage="launching", pct=100, url=url,
                    msg=("Starting the player — %s audio, converting to AC3…" % acodec)
                        if needs_fix else
                        ("Starting the player — %s audio, native…" % (acodec or "unknown")))
            if _stand_down(mid, gen):
                return
            pok, pmsg = tvlink.launch(url, mid, pick, title, gen)
            # launch() stands aside for a newer play rather than failing the
            # film; the worker's own stand-down writes the right message.
            if not pok and _stand_down(mid, gen):
                return
            if not pok and pick.get("transcoded"):
                # launch() failing does not stop the transcode it was waiting
                # on; nothing else will either, so the swarm stays open.
                transcode.transcode_stop_all()
            if pok:
                # kind/season/episode/next let UIs show "Next: S01E02" -- kept
                # separate from the fields above, which apply to films too.
                kind, season, episode, next_ep = "movie", None, None, None
                if str(mid).startswith("tv:"):
                    kind = "tv"
                    try:
                        tid, season, episode = catalogue.tv_job_parts(str(mid))
                        nxt = catalogue.next_episode(tid, season, episode)
                        next_ep = "S%02dE%02d" % nxt if nxt else None
                    except Exception:
                        pass
                nowplaying._now.update(title=title or "Unknown", tag=pick.get("tag"),
                            audio=pick.get("audio_actual") or "?",
                            transcoded=bool(pick.get("transcoded")),
                            gb=pick.get("gb"), at=time.time(),
                            kind=kind, season=season, episode=episode, next=next_ep,
                            # What the bridge decodes for this film, so a service
                            # restart mid-film (every server push is one) can
                            # pick the audio back up instead of leaving the TV
                            # silent until the film is started again.
                            hifi_src=internal if (sendspin._hifi["on"] and config.SENDSPIN_ENABLED) else None,
                            hifi_aidx=pick.get("audio_track") or 0,
                            hifi_centre=sendspin._hifi["film_centre"],
                            hifi_job=str(mid) if (sendspin._hifi["on"] and config.SENDSPIN_ENABLED) else None)
                nowplaying._now_save(nowplaying._now)
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
    global _play_inflight
    with core._lock:
        c0 = _cancel_gen
        _play_inflight += 1
    try:
        entry, runtime = resolve()
        if not entry["pick"]:
            return 409, {"ok": False, "msg": entry["err"] or "no stream"}
        with core._lock:
            if _cancel_gen != c0:
                return 409, {"ok": False, "msg": "Cancelled"}
        # Only worth checking a route to the TV that is actually going
        # to be used. A connected app needs none of this, and with the
        # phone remote off there is nothing to check -- but then there is
        # also no way to play anything, and the phone should be told why
        # rather than watching a job fail a minute later. A browser owner
        # has no TV app to reach at all, so this whole check is TV-only.
        if owner == "tv" and not tvlink.app_fresh():
            if not config.ADB_ENABLED:
                return 502, {"ok": False,
                    "msg": "No TV app is connected, and the phone remote is off"}
            st = tvlink.adb_state()
            if st != "device":
                ok, msg = tvlink.adb_ready()
                if not ok:
                    return 502, {"ok": False, "msg": msg}
        url = streams.stream_url(entry["pick"])
        title = entry.get("title")
        with core._lock:
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
        # Same reason: the episode after this one is worked out afresh for
        # every play (_remember_next_episode), never inherited from the last.
        kw["next_ep"] = None
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
        job_set(jobid, **watching._shelf_begin(jobid, entry, runtime))
        if autoplay and str(jobid).startswith("tv:"):
            threading.Thread(target=_remember_next_episode, args=(jobid,),
                             daemon=True).start()
        return 202, {"ok": True, "msg": "buffering", "url": url,
                     "pick": entry["pick"], "job": jobid}
    finally:
        with core._lock:
            _play_inflight -= 1
