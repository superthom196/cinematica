"""Audio conversion: the ffmpeg runs inside the Stremio container that turn
an unplayable soundtrack into one the TV can decode, regulated against
the playhead.
"""
import os, shutil, subprocess, threading, time
from providers import contract

import config, core, jobs, browser_session

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
        config.PUBLIC_HOST, config.PORT, tc_key(c), ("/%d" % idx) if idx is not None else "")
_transcodes = {}          # infoHash -> Popen
_tc_lock = threading.Lock()

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
        r = subprocess.run(["docker", "exec", config.FFMPEG_CTR, "ps", "-eo", "pid,args"],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or "ffmpeg" not in parts[1] or config.TC_CTR + "/" not in parts[1]:
            continue
        rel = parts[1].rsplit(config.TC_CTR + "/", 1)[-1].split()[0]
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
            subprocess.run(["docker", "exec", config.FFMPEG_CTR, "kill", sig, pid],
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
        subprocess.run(["docker", "exec", config.FFMPEG_CTR, "kill", "-KILL", pid],
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
        files = [os.path.join(config.TC_HOST, f) for f in os.listdir(config.TC_HOST) if f.endswith(".ts")]
        files.sort(key=os.path.getmtime, reverse=True)
        with _tc_lock:
            busy = {v.get("name") for v in _transcodes.values() if isinstance(v, dict)}
        for old in files[config.TC_KEEP:]:
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
        with core._lock:
            live_token = browser_session._bx.get("token")
        live_dir = (config.BX_DIR + live_token) if live_token else None
        for name in os.listdir(config.TC_HOST):
            if not name.startswith(config.BX_DIR) or name == live_dir:
                continue
            full = os.path.join(config.TC_HOST, name)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
    except Exception:
        pass

def transcode_begin(ih, idx, src_internal, bytes_per_sec, mid, gen=None, aidx=0, start_s=0):
    """Start the conversion to disk and wait until it is TC_HEAD seconds ahead.
    Returns the file name on success, else None.

    start_s is a resume: the conversion starts there rather than at 0:00, so
    the TV can open the film where it is wanted instead of waiting for a
    from-the-top conversion to get that far. The file then begins a little
    before start_s, at the keyframe the video copy has to start on;
    film_start() measures where."""
    os.makedirs(config.TC_HOST, exist_ok=True)
    name = tc_name(ih, idx)
    host_path = os.path.join(config.TC_HOST, name)
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
    cmd = ["docker", "exec", config.FFMPEG_CTR, config.FFMPEG, "-hide_banner", "-loglevel", "error",
           *(["-ss", "%.3f" % start_s] if start_s and start_s > 0 else []),
           "-i", src_internal,
           "-map", "0:v:0", "-map", "0:a:%d" % aidx, "-c:v", "copy",
           "-c:a", "ac3", "-b:a", "448k",
           "-f", "mpegts", "-y", "%s/%s" % (config.TC_CTR, name)]
    # Belt and braces before unlinking: if anything is still writing this exact
    # output and the registry has lost track of it, removing the file would
    # orphan it onto a deleted inode where it burns CPU and disk unseen.
    _kill_ctr(name)
    try:
        os.remove(host_path)
    except OSError:
        pass
    proc = transcode_start(ih, cmd, name=name)
    want = max(12 * 1048576, int((bytes_per_sec or 0) * config.TC_HEAD))
    def start_regulator():
        threading.Thread(target=regulate_lead,
                         args=(ih, host_path, bytes_per_sec, proc),
                         daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < config.HARD_CAP_SECS:
        if gen is not None and jobs.superseded(gen):
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
        jobs.job_set(mid, stage="encoding", pct=min(99, int(sz * 100 / max(want, 1))),
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
        r = subprocess.run(["docker", "exec", config.FFMPEG_CTR, "ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=15)
        st = (r.stdout or "").strip()
        return st.startswith("T") if st else None
    except Exception:
        return None

def _ctr_pid(name):
    """PID of the ffmpeg writing `name`, as seen INSIDE the container -- host
    PIDs are the docker-exec client, and signalling those does nothing."""
    try:
        r = subprocess.run(["docker", "exec", config.FFMPEG_CTR, "pgrep", "-f", name],
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
        r = subprocess.run(["docker", "exec", config.FFMPEG_CTR, config.FFPROBE, "-v", "error",
                            "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                            "%s/%s" % (config.TC_CTR, os.path.basename(host_path))],
                           capture_output=True, text=True, timeout=25)
        return float((r.stdout or "").strip())
    except Exception:
        return None

def film_start(host_path, start_s):
    """Where in the film a conversion started at start_s actually begins.

    -ss before -i seeks the input: the AC3 audio is decoded, so it is trimmed to
    start_s exactly, but the copied video can only start on a keyframe, and that
    is the one before start_s. Both streams keep their spacing in the output, so
    the gap between their first timestamps is how far before start_s the
    picture -- and the player's clock, which counts from it -- begins. Falls
    back to start_s itself, a few seconds out at worst, if the file will not say.
    """
    try:
        r = subprocess.run(["docker", "exec", config.FFMPEG_CTR, config.FFPROBE, "-v", "error",
                            "-show_entries", "stream=codec_type,start_time", "-of", "csv=p=0",
                            "%s/%s" % (config.TC_CTR, os.path.basename(host_path))],
                           capture_output=True, text=True, timeout=25)
        return film_start_from(r.stdout or "", start_s)
    except Exception:
        return float(start_s)

def film_start_from(probe_csv, start_s):
    """film_start()'s arithmetic, on ffprobe's `codec_type,start_time` lines."""
    first = {}
    for line in probe_csv.splitlines():
        kind, _, t = line.strip().partition(",")
        try:
            first.setdefault(kind, float(t))
        except ValueError:
            continue
    if "video" not in first or "audio" not in first:
        return float(start_s)
    return max(0.0, float(start_s) + first["video"] - first["audio"])

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
            if not stopped and lead > config.TC_LEAD:
                subprocess.run(["docker", "exec", config.FFMPEG_CTR, "kill", "-STOP", pid],
                               capture_output=True, timeout=15)
                stopped = True
                _tc_flag(ih, suspended=True)     # /audio/ must not read this as EOF
                print("transcode: suspend %s lead=%.0fs" % (ih[:8], lead), flush=True)
            elif stopped and lead < config.TC_LEAD * config.TC_BAND:
                subprocess.run(["docker", "exec", config.FFMPEG_CTR, "kill", "-CONT", pid],
                               capture_output=True, timeout=15)
                stopped = False
                _tc_flag(ih, suspended=False)
                print("transcode: resume  %s lead=%.0fs" % (ih[:8], lead), flush=True)
            time.sleep(2.0)
    except Exception as ex:
        # The regulator is gone either way; say why, or a conversion that ran
        # away from the playhead has nothing in the log to explain it.
        print("transcode: regulator for %s stopped: %s" % (ih[:8], ex), flush=True)
    finally:
        if stopped:                       # never leave it suspended
            try:
                subprocess.run(["docker", "exec", config.FFMPEG_CTR, "kill", "-CONT", pid],
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
        while len(_transcodes) >= config.MAX_TRANSCODES:
            k, v = next(iter(_transcodes.items()))
            _transcodes.pop(k, None)
            _stop(v)
        # ffmpeg runs with -loglevel error, and its stderr went to DEVNULL --
        # so every decode error, short read and mux warning it has ever reported
        # was discarded. Audio faults were undiagnosable as a direct result.
        log = None
        if name:
            try:
                log = open(os.path.join(config.TC_HOST, os.path.splitext(name)[0] + ".log"), "wb")
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
