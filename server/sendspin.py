"""Sendspin hi-fi audio: the server's side of the bridge sidecar that plays a
film's soundtrack through a network player in step with the TV.
"""
import json, queue, time, urllib.error, urllib.request

import config, core, nowplaying, tvlink

# The bridge, not this process, owns the DAC and the ffmpeg feeding it. All
# network I/O to it happens on _ss_worker's own thread so a slow or dead
# bridge can never block the heartbeat handler or be reached while _lock (==
# _app_cv's lock) is held -- see the callers, which only ever put() here.
_hifi = {"on": False, "gen": int(time.time()), "t0_us": None, "clock_offset_us": 0,
         "streaming": False, "connected": False, "src": None, "aidx": 0,
         # Centre mode: the TV's own speakers play the film's centre channel
         # and the Sendspin player gets L/R with the centre taken out. "centre"
         # is the TV's setting off the heartbeat; "film_centre" is what the
         # film on screen was started with, fixed for that film so the bridge's
         # cache and the TV's own decode never disagree mid-film.
         "centre": False, "film_centre": False,
         "last_restart": 0.0, "err_s": None, "player_url": None,
         "pending_since": 0.0, "seek_seq": None, "delay_ms": config.HIFI_AUDIO_DELAY_MS,
         # Why the last start did not produce audio, for the TV to show instead
         # of a silent film; cleared the moment audio goes live.
         "last_error": None, "fail_count": 0,
         # delay_ms is what the viewer asked for; applied_delay_ms is what the
         # bridge says its timeline carries. err_s uses the applied one, so the
         # picture never chases a trim the sound has not taken on yet.
         "applied_delay_ms": config.HIFI_AUDIO_DELAY_MS, "delay_at": 0.0,
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
    with core._lock:
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
    return end_s is not None and position_s >= end_s - config.HIFI_TRACK_END_S


def _hifi_fail(msg):
    """Record why audio is not running; the next start waits longer for it."""
    _hifi["last_error"] = msg
    _hifi["fail_count"] += 1
    _hifi["pending_since"] = 0.0
    _hifi["streaming"] = False
    _hifi["t0_us"] = None


def _hifi_restart_gap_s():
    return min(config.HIFI_RESTART_MIN_GAP_S * (2 ** _hifi["fail_count"]), config.HIFI_RESTART_MAX_GAP_S)


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
        nowplaying._now["hifi_delay_ms"] = ms
        nowplaying._now_save(nowplaying._now)
    return ms
_ss_q = queue.Queue()

def _hifi_apply_status(body):
    """Fold a bridge /status into _hifi. Only the gen this process last asked
    for has a timeline; anything else the bridge reports is either a stream we
    already retired or proof that the bridge lost ours (it restarted, or the
    player dropped), in which case the next playing heartbeat starts afresh."""
    now = time.time()
    with core._lock:
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
        elif pending and now - _hifi["pending_since"] > config.HIFI_START_TIMEOUT_S:
            print("sendspin: start gen=%d produced no audio in %.0fs, will retry"
                  % (_hifi["gen"], config.HIFI_START_TIMEOUT_S), flush=True)
            _hifi_fail("no audio within %.0f s" % config.HIFI_START_TIMEOUT_S)

def _ss_call(path, body=None, timeout=3.0):
    """Talk to the bridge. Never raises -- a bridge that is down, slow, or
    answers with garbage must not take out the worker thread. /status and
    /players are the only GETs on this API; everything else is POST, empty
    body or not.
    Returns (status, dict); status 0 means the request itself never landed."""
    method = "GET" if path in ("/status", "/players") else "POST"
    data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
    req = urllib.request.Request(config.SENDSPIN_BRIDGE + path, data=data, method=method,
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
        with core._lock:
            pending = _hifi["pending_since"] > 0
        try:
            # A pending start is polled briskly: t0 is what the TV is waiting on.
            action = _ss_q.get(timeout=0.5 if pending else 5)
        except queue.Empty:
            action = None
        try:
            if action is None:
                with core._lock:
                    on = _hifi["on"]
                    pending = _hifi["pending_since"] > 0
                if not on or (not pending and tvlink.tv_playback_state() not in (2, 3)):
                    continue
                status, body = _ss_call("/status")
                if status == 200:
                    _hifi_apply_status(body)
                continue
            kind = action[0]
            if kind == "connect":
                with core._lock:
                    player_url = _hifi["player_url"]
                payload = {"url": player_url} if player_url else {}
                status, body = _ss_call("/connect", payload, timeout=config.HIFI_CONNECT_TIMEOUT_S)
                with core._lock:
                    _hifi["connected"] = status == 200 and bool(body.get("connected"))
            elif kind == "prepare":
                # The film is still pre-buffering: get the player and start
                # decoding the track into the bridge's cache now, so the first
                # playing heartbeat is answered from the cache, not by ffmpeg
                # seeking the swarm.
                _, src, aidx, gen = action
                with core._lock:
                    if _hifi["gen"] != gen or _hifi["src"] != src:
                        continue
                    player_url = _hifi["player_url"]
                    centre = _hifi["film_centre"]
                status, body = _ss_call("/connect", {"url": player_url}, timeout=config.HIFI_CONNECT_TIMEOUT_S)
                with core._lock:
                    _hifi["connected"] = status == 200 and bool(body.get("connected"))
                    if status != 200:
                        _hifi["last_error"] = str(body.get("error") or "bridge answered %s" % status)
                        print("sendspin: connect failed: %s" % _hifi["last_error"], flush=True)
                    if _hifi["gen"] != gen or _hifi["src"] != src:
                        continue
                status, body = _ss_call("/prepare", {"src": src, "aidx": aidx, "centre": centre},
                                        timeout=config.HIFI_CALL_TIMEOUT_S)
                if status != 200:
                    print("sendspin: prepare failed (%s): %s" % (status, body.get("error")), flush=True)
            elif kind == "start":
                _, src, aidx, start_s, gen, pos_at_us = action
                with core._lock:
                    if _hifi["gen"] != gen:
                        continue
                    player_url = _hifi["player_url"]
                # Idempotent when already connected; recovers after either service
                # restarts or the hifi client temporarily drops off the network.
                status, body = _ss_call("/connect", {"url": player_url}, timeout=config.HIFI_CONNECT_TIMEOUT_S)
                with core._lock:
                    if status == 200 and body.get("connected"):
                        _hifi["connected"] = True
                    if _hifi["gen"] != gen:
                        continue
                if status == 200:
                    with core._lock:
                        delay_ms = _hifi["delay_ms"]
                        centre = _hifi["film_centre"]
                    status, body = _ss_call("/start", {"src": src, "aidx": aidx, "centre": centre,
                                                         "start_s": start_s, "gen": gen,
                                                         "pos_at_us": pos_at_us,
                                                         "delay_ms": delay_ms},
                                            timeout=config.HIFI_CALL_TIMEOUT_S)
                    if status in (200, 202):
                        with core._lock:
                            _hifi["applied_delay_ms"] = delay_ms
                with core._lock:
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
                status, body = _ss_call("/delay", {"ms": ms}, timeout=config.HIFI_CALL_TIMEOUT_S)
                if status == 200 and body.get("delay_ms") is not None:
                    with core._lock:
                        _hifi["applied_delay_ms"] = body["delay_ms"]
            elif kind == "stop":
                _ss_call("/stop", timeout=config.HIFI_CALL_TIMEOUT_S)
            elif kind == "release":
                _ss_call("/release", timeout=config.HIFI_CALL_TIMEOUT_S)
                with core._lock:
                    _hifi["connected"] = False
            elif kind == "volume":
                _, payload = action
                status, body = _ss_call("/volume", payload)
                if status == 200 and body.get("volume") is not None:
                    with core._lock:
                        _hifi["volume"] = body["volume"]
        except Exception as ex:
            print("sendspin: worker error: %s" % ex, flush=True)
if nowplaying._now.get("hifi_src"):
    # Restart mid-film: the first playing heartbeat with hifi on restarts the
    # audio from the TV's position, exactly as after a pause.
    _hifi["src"] = nowplaying._now["hifi_src"]
    _hifi["aidx"] = int(nowplaying._now.get("hifi_aidx") or 0)
    _hifi["film_centre"] = bool(nowplaying._now.get("hifi_centre"))
    _hifi["job"] = nowplaying._now.get("hifi_job")
if nowplaying._now.get("hifi_delay_ms") is not None:
    _hifi["delay_ms"] = max(-2000, min(5000, int(nowplaying._now["hifi_delay_ms"])))
