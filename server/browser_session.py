"""Playing in the browser: the on-demand HLS packager session, its
regulator, and the job that picks a source the browser can play.
"""
import math, os, re, shutil, subprocess, threading, time, urllib.error, urllib.request
import browser_play

import config, core, mediaprobe, streams, catalogue, torrents, transcode, jobs

def browser_playing():
    """Whether a browser session is currently watching something.

    A pause still counts: the viewer is sitting in front of it, and treating a
    paused film as idle is what would let cache_watch() empty the cache under
    them. Only a session that has stopped sending heartbeats is gone.
    """
    with core._lock:
        return (_bx["token"] is not None and _bx["state"] != "ended"
                and time.time() - _bx["at"] < config.BX_IDLE)

# One browser session at a time, mirroring the one-play-job rule in jobs.py. The
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

def bx_begin(token, src_internal, plan, seg, duration, mid, gen):
    """Start a browser HLS session: make its directory, record the state the
    routes in routes.py read, and kick off the first ffmpeg at the front of the
    film. Returns True on success, False if the directory could not be made
    or the first ffmpeg failed to start -- either way there is nothing yet
    for the browser to fetch.
    """
    sess_dir = os.path.join(config.TC_HOST, config.BX_DIR + token)
    try:
        os.makedirs(sess_dir, exist_ok=True)
    except OSError:
        return False
    n_segs = math.ceil(duration / seg) if duration and seg else 0
    with core._lock:
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

    -ss before -i is an INPUT seek, and -copyts is never passed (see
    segment_cmd in browser_play.py), so ffmpeg resets the seeked stream's own
    presentation clock to (near) zero at the seek point and counts up from
    there for as long as this process keeps running. -start_number k0 only
    renames the OUTPUT FILES this run writes -- s000100.m4s and so on -- onto
    their real place on the absolute grid; it does not tell ffmpeg to stamp
    an offset into the bytes it writes. So the files on disk are correctly
    named while everything inside them still starts its clock at zero. The
    server corrects that at serve time -- see the segment route in routes.py, and
    the reasoning next to its delta calculation.

    Registered through transcode_start with key "bx:"+token and name
    "bx_"+token, so MAX_TRANSCODES, _stop(), transcode_stop_all() and
    _kill_orphans() all manage this exactly like a TV transcode, with no
    special case anywhere in that machinery for what a browser session is.
    ffmpeg's own ff.m3u8 is written but never served -- the server hands out
    its own VOD playlist instead (see vod_playlist / the index.m3u8 route).
    """
    with core._lock:
        if _bx["token"] != token:
            return False
        sess_dir, seg, src = _bx["dir"], _bx["seg"], _bx["src"]
        plan = _bx["plan"] or {}
    if not sess_dir or not seg or not src:
        return False
    name = config.BX_DIR + token
    aidx = int(plan.get("aidx") or 0)
    # Every segment file left in this directory was written by the run that
    # is being replaced, on a different anchor, so its bytes carry a
    # different zero point -- and the whole re-timing in routes.py rests on one
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
            mediaprobe.probe_keyframes(src, k0 * seg), k0, seg)
    # The argv itself is built in browser_play.segment_cmd -- a pure
    # function with no docker/host detail -- precisely so a test can assert
    # "-c:v copy" is unconditional and no video encoder ever sneaks in.
    # Only the docker-exec/ffmpeg-binary prefix belongs here.
    out_dir = "%s/%s" % (config.TC_CTR, name)
    playlist = "%s/ff.m3u8" % out_dir
    cmd = ["docker", "exec", config.FFMPEG_CTR, config.FFMPEG] + browser_play.segment_cmd(
        src, out_dir, playlist, k0, seg, plan, aidx)
    # Published BEFORE the process that will write the segments, not after.
    # The serve path re-times whatever is on disk by _bx["run_anchor"], so
    # any moment where ffmpeg is writing this run's segments while _bx still
    # names the previous run's anchor is a moment a segment can be served
    # with the wrong timeline -- and once it is in the player's buffer, that
    # is not something a later correction can take back.
    with core._lock:
        if _bx["token"] != token:
            return False
        _bx.update(anchor=k0, frontier=k0 - 1, run_anchor=run_anchor)
    transcode.transcode_start("bx:" + token, cmd, name=name)
    with core._lock:
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
    -- see the segment route in routes.py, the only caller, which already collapsed
    a burst of seek requests into this one call via seek_gen and
    BX_SEEK_DEBOUNCE before ever reaching here.
    """
    with core._lock:
        if _bx["token"] != token:
            return False
        my_gen = _bx["seek_gen"]
    name = config.BX_DIR + token
    with transcode._tc_lock:
        ent = transcode._transcodes.get("bx:" + token)
    transcode._kill_ctr(name)
    if isinstance(ent, dict):
        transcode._stop(ent)
    with core._lock:
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
    with core._lock:
        token = _bx["token"]
        sess_dir = _bx["dir"]
        _bx.update(token=None, job=None, gen=0, at=0.0, state="idle",
                   pos=0.0, dur=None, title=None,
                   dir=None, seg=None, anchor=None, frontier=None,
                   seek_gen=0, pending_anchor=None, timescales=None,
                   run_anchor=0.0,
                   n_segs=None, proc_key=None, src=None, plan=None)
    if token:
        with transcode._tc_lock:
            ent = transcode._transcodes.pop("bx:" + token, None)
        transcode._kill_ctr(config.BX_DIR + token)
        if isinstance(ent, dict):
            transcode._stop(ent)
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
    with core._lock:
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
    with core._lock:
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
    with core._lock:
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
    with core._lock:
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
        if browser_play.seg_start(k, seg) < pos - config.BX_BEHIND:
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
    name = config.BX_DIR + token
    pid = None
    # The caller starts this thread immediately after transcode_start, with
    # no wait loop first (unlike regulate_lead, whose caller already waited
    # for a head start to build) -- so the container process may not be
    # visible to pgrep yet. Give it a moment rather than bailing out cold.
    for _ in range(20):
        pid = transcode._ctr_pid(name)
        if pid:
            break
        time.sleep(0.25)
    if not pid:
        return
    started = time.time()
    stopped = False
    try:
        while True:
            with transcode._tc_lock:
                ent = transcode._transcodes.get(key)
            proc = ent.get("proc") if isinstance(ent, dict) else None
            if proc is None or proc.poll() is not None:
                break
            with core._lock:
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
            real = transcode._ctr_stopped(pid)
            if real is not None and real != stopped:
                print("bx: %s externally %s -- resyncing"
                      % (token[:8], "suspended" if real else "resumed"), flush=True)
                stopped = real
                transcode._tc_flag(key, suspended=real)
            if not stopped and lead > config.BX_LEAD:
                subprocess.run(["docker", "exec", config.FFMPEG_CTR, "kill", "-STOP", pid],
                               capture_output=True, timeout=15)
                stopped = True
                transcode._tc_flag(key, suspended=True)
                print("bx: suspend %s lead=%.0fs" % (token[:8], lead), flush=True)
            elif stopped and lead < config.BX_LEAD * config.TC_BAND:
                subprocess.run(["docker", "exec", config.FFMPEG_CTR, "kill", "-CONT", pid],
                               capture_output=True, timeout=15)
                stopped = False
                transcode._tc_flag(key, suspended=False)
                print("bx: resume  %s lead=%.0fs" % (token[:8], lead), flush=True)
            _bx_trim(token, playhead, frontier)
            time.sleep(2.0)
    except Exception as ex:
        print("bx: regulator for %s stopped: %s" % (token[:8], ex), flush=True)
    finally:
        if stopped:                       # never leave it suspended
            try:
                subprocess.run(["docker", "exec", config.FFMPEG_CTR, "kill", "-CONT", pid],
                               capture_output=True, timeout=15)
            except Exception:
                pass
            transcode._tc_flag(key, suspended=False)

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
            "http://127.0.0.1:%d%s" % (config.PORT, url_path),
            headers={"Range": "bytes=0-0", "User-Agent": config.UA})
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
    with core._lock:
        _bx.update(token=token, job=mid, gen=gen, state="playing",
                   at=time.time(), dur=media.get("duration"), title=title)
    jobs.job_set(mid, stage="playing", ok=True, pct=100, owner="browser",
            otoken=token, media=media, pick=pick, msg="Ready to play")
    threading.Thread(target=torrents.keep_only, args=((pick or {}).get("infoHash"),),
                     daemon=True).start()

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
    if not config.AUTOPLAY_NEXT or not str(mid).startswith("tv:"):
        return None
    try:
        tid, s, e = catalogue.tv_job_parts(str(mid))
        nxt = catalogue.next_episode(tid, s, e)
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
        total = min(len(picks), config.ATTEMPTS)
        reasons = []
        for i, pick in enumerate(picks[:config.ATTEMPTS], start=1):
            if jobs._stand_down(mid, gen):
                return
            key = candidate_key(pick)
            tried = []
            prep = jobs.prepare_candidate(mid, pick, runtime_min, i, total, gen, tried)
            tried_keys.append(key)
            jobs.job_set(mid, tried_keys=tried_keys)
            if prep is None:
                if jobs._stand_down(mid, gen):
                    return
                reasons.append("%s: %s" % (pick.get("codec") or "?",
                               tried[-1] if tried else "could not be prepared"))
                continue
            probe = mediaprobe.probe_full(prep["internal"])
            plan = browser_play.decide(caps, {
                "format_name": probe["format_name"], "duration": probe["duration"],
                "video": probe["video"], "audio": probe["audio"], "aidx": prep["aidx"],
            })
            if plan["mode"] == "skip":
                reasons.append("%s: %s" % (pick.get("codec") or "?", plan["reason"]))
                jobs.job_set(mid, msg="Candidate %d/%d: %s — trying the next…"
                                 % (i, total, plan["reason"]))
                continue
            if jobs._stand_down(mid, gen):
                return
            if plan["mode"] == "direct":
                url = streams.stream_url_public(pick)
                if not bx_verify_url(url):
                    reasons.append("%s: the direct url did not check out"
                                   % (pick.get("codec") or "?"))
                    jobs.job_set(mid, msg="Candidate %d/%d could not be verified — "
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
                if jobs._stand_down(mid, gen):
                    return
                publish(mid, token, pick, media, gen, title)
                return
            # "remux" or "audio" -- both need the packager running, and
            # differ only in whether ffmpeg re-encodes the audio track
            # (decide() already put that choice in plan["acodec"]).
            duration = probe["duration"] or (runtime_min or 0) * 60
            gop = mediaprobe.probe_gop(prep["internal"])
            seg, n_segs = browser_play.grid(duration, gop)
            if not bx_begin(token, prep["internal"], plan, seg, duration, mid, gen):
                reasons.append("%s: the packager could not start"
                               % (pick.get("codec") or "?"))
                jobs.job_set(mid, msg="Candidate %d/%d could not start — trying "
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
            if jobs._stand_down(mid, gen):
                with core._lock:
                    mine = _bx["token"] == token
                if mine:
                    bx_stop_all("superseded")
                return
            publish(mid, token, pick, media, gen, title)
            return
        jobs.job_set(mid, stage="error", ok=False, tried_keys=tried_keys,
                msg="No candidate could stream. " + (
                    "; ".join(reasons) if reasons else "Nothing left to try."))
    except Exception as ex:
        # Same guard run_play_job has: an uncaught error here must not
        # leave the job -- and the page's poll loop -- stuck forever.
        jobs.job_set(mid, stage="error", ok=False, msg=f"{type(ex).__name__}: {ex}")
