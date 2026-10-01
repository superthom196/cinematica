"""The TV app: its heartbeat and command channel, the handoff of a film to
it, what it says is playing, and the optional adb wake-up.
"""
import subprocess, threading, time
import shelf

import config, core, streams, catalogue, transcode, sendspin, jobs, browser_session, watching

def playing_now():
    """Cheap guard: measuring saturates the link, so it must yield to a film.

    The TV is not the only player any more, so a browser watching counts too.
    """
    try:
        return tv_playback_state() in (2, 3) or browser_session.browser_playing()
    except Exception:
        return False

def adb(*args, timeout=25):
    return subprocess.run([config.ADB, *args], capture_output=True, text=True, timeout=timeout)

def adb_state():
    """Returns one of: off | device | unauthorized | offline | absent.

    "off" is not an adb state at all: it means the phone remote was never turned
    on, and it is returned WITHOUT running adb. /api/health calls this on every
    poll, and an install that has no TV to pair with should not be shelling out
    to a binary it may not even have.
    """
    if not config.ADB_ENABLED:
        return "off"
    r = adb("devices")
    for line in r.stdout.splitlines():
        if line.startswith(config.ADB_TV):
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
    if not config.ADB_ENABLED:
        return False, "phone remote is off"
    st = adb_state()
    if st == "device":
        return True, "connected"
    if st == "unauthorized":
        return False, "TV is waiting for you to accept the USB-debugging prompt on screen"
    if st == "offline" or hard:
        adb("disconnect", config.ADB_TV)
        time.sleep(0.5)
    adb("connect", config.ADB_TV)
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
    if not config.ADB_ENABLED:
        return False, "phone remote is off"
    ok, msg = adb_ready()
    if not ok:
        return False, f"TV not reachable: {msg}"
    # Wake the panel BEFORE the start. A TV in standby accepts "am start"
    # perfectly happily and brings the app up on a screen that is off, so this
    # returns "woken", the film plays, and the room sees nothing. KEYCODE_WAKEUP
    # is what turns the panel on, and it is a no-op on a TV already awake.
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "am", "start", "-n", config.PLAYER, "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LEANBACK_LAUNCHER")
    deadline = time.time() + config.APP_WAKE_SECS
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
    transcode.transcode_stop_all()
    if config.SENDSPIN_ENABLED:
        sendspin._hifi_release()
    return True, "stopped"

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
_app_cv  = threading.Condition(core._lock)

def app_fresh():
    """The connected app, if its last heartbeat is recent enough to believe."""
    with core._lock:
        if _app and time.time() - _app["seen_at"] < config.APP_TTL:
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
        cmd = {"seq": _app_seq, "type": kind, "expires_at": time.time() + config.APP_CMD_TTL}
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
        if not prev or now - prev["seen_at"] >= config.APP_TTL:
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
        sendspin._hifi["on"] = hifi
        # The TV names which Sendspin player is next to it; a change mid-film
        # is left alone (the current stream keeps its player) and only takes
        # effect on the next film, but a change while nothing is playing can
        # drop the old connection right away so the next connect() picks up
        # the new one instead of the bridge's stale default.
        sendspin._hifi["centre"] = bool(d.get("hifi_centre"))
        new_player = d.get("hifi_player") or None
        if new_player != sendspin._hifi["player_url"]:
            sendspin._hifi["player_url"] = new_player
            if sendspin._hifi["connected"] and state not in ("playing", "paused") and sendspin._hifi["job"] is None:
                # Not while a film is being prepared: its start re-connects to
                # the new player anyway, keeping the decoded cache.
                sendspin._ss_q.put(("release",))
        position_s = _app["position_s"]
        seek_seq = d.get("seek_seq")
        viewer_seek = (seek_seq is not None and sendspin._hifi["seek_seq"] is not None
                       and seek_seq != sendspin._hifi["seek_seq"])
        sendspin._hifi["seek_seq"] = seek_seq
        if hifi and d.get("hifi_delay_ms") is not None:
            try:
                tv_ms = max(-2000, min(5000, int(d["hifi_delay_ms"])))
                # First beat of a session: the TV's stored value is the truth.
                # After that only a change to it is an instruction.
                if tv_ms != sendspin._hifi["tv_delay_ms"]:
                    sendspin._hifi["tv_delay_ms"] = tv_ms
                    sendspin._hifi_set_delay(tv_ms)
            except (TypeError, ValueError):
                pass
        active_audio = sendspin._hifi["streaming"] or sendspin._hifi["pending_since"] > 0 or sendspin._hifi["t0_us"] is not None
        if hifi and state == "idle" and sendspin._hifi["job"] is not None and not active_audio:
            # Idle because the next film is still pre-buffering: the bridge is
            # connected and decoding it on purpose. Releasing now would throw
            # that cache away and leave the start to a cold decoder.
            pass
        elif not hifi or state in ("idle", "ended", "error"):
            if active_audio or sendspin._hifi["connected"]:
                sendspin._hifi_invalidate()
                sendspin._hifi["connected"] = False
                sendspin._ss_q.put(("release",))
            sendspin._hifi["fail_count"] = 0
            sendspin._hifi["last_error"] = None
        elif state in ("paused", "buffering"):
            if active_audio:
                sendspin._hifi_invalidate()
                sendspin._ss_q.put(("stop",))
        elif state == "playing" and position_s is not None and not sendspin._hifi["src"]:
            sendspin._hifi["last_error"] = "no audio source for this film"
        elif state == "playing" and position_s is not None and job != sendspin._hifi["job"]:
            # The previous film is still on screen while the next one buffers
            # (its src already points at the new one). Nothing to start yet.
            pass
        elif state == "playing" and position_s is not None:
            if viewer_seek:
                sendspin._hifi_invalidate()
                sendspin._hifi["fail_count"] = 0
            pending = sendspin._hifi["pending_since"] > 0
            if pending and now - sendspin._hifi["pending_since"] > config.HIFI_START_TIMEOUT_S:
                print("hifi: start gen=%d produced no audio in %.0fs, will retry"
                      % (sendspin._hifi["gen"], config.HIFI_START_TIMEOUT_S), flush=True)
                sendspin._hifi_fail("no audio within %.0f s" % config.HIFI_START_TIMEOUT_S)
                pending = False
            if not pending and (not sendspin._hifi["streaming"] or sendspin._hifi["t0_us"] is None):
                if sendspin._hifi["done"]:
                    # The audio is over and the player has been let go. The
                    # film still on screen is its last frames, not a reason to
                    # take the player again.
                    pass
                elif sendspin._hifi_track_over(position_s):
                    print("hifi: audio track finished at %.1fs, releasing the player"
                          % position_s, flush=True)
                    sendspin._hifi_invalidate()
                    sendspin._hifi["done"] = True
                    sendspin._hifi["connected"] = False
                    # src and job stay: the film is still on screen, and
                    # clearing them would put "no audio source for this film"
                    # over its last seconds.
                    sendspin._ss_q.put(("release",))
                elif viewer_seek or now - sendspin._hifi["last_restart"] > sendspin._hifi_restart_gap_s():
                    sendspin._hifi_invalidate()
                    sendspin._hifi["last_restart"] = now
                    sendspin._hifi["pending_since"] = now
                    # Start at the reported position. The TV follows the actual
                    # first-sample timestamp, with no guessed/adaptive lead.
                    print("hifi: start gen=%d at %.3fs%s" % (sendspin._hifi["gen"], position_s,
                          ", viewer seeked" if viewer_seek else ""), flush=True)
                    # The position and the moment it was true, on this clock:
                    # the bridge pins its timeline to exactly that and serves
                    # from its cache, so nothing has to be chased afterwards.
                    sendspin._ss_q.put(("start", sendspin._hifi["src"], sendspin._hifi["aidx"], position_s, sendspin._hifi["gen"],
                               time.monotonic_ns() // 1000))
            elif sendspin._hifi["streaming"] and sendspin._hifi["t0_us"] is not None:
                audio_pos_s = ((time.monotonic_ns() // 1000
                                + sendspin._hifi["clock_offset_us"] - sendspin._hifi["t0_us"]) / 1e6)
                # audio_pos already carries the trim (it is in t0), so taking
                # it off again leaves err as the true picture-to-sound error:
                # the trim moves the sound, and the TV holds its ground.
                err_s = round(position_s - audio_pos_s
                              - sendspin._hifi["applied_delay_ms"] / 1000.0, 3)
                sendspin._hifi["err_s"] = err_s
                sync = {"gen": sendspin._hifi["gen"], "audio_pos_s": round(audio_pos_s, 3), "err_s": err_s}
        if hifi and state in ("playing", "paused", "buffering"):
            # What the TV shows in place of a silently muted film.
            if sync is not None:
                hst = "live"
            elif state != "playing":
                hst = "stopped"
            elif sendspin._hifi["done"]:
                # The track ended before the picture did. Stopped, not failed:
                # nothing went wrong and there is nothing to wait for.
                hst = "stopped"
            elif sendspin._hifi["pending_since"] > 0:
                hst = "starting"
            elif sendspin._hifi["last_error"]:
                hst = "failed"
            else:
                hst = "starting"
            hifi_status = {"state": hst, "msg": sendspin._hifi["last_error"] if hst == "failed" else None}
            if sendspin._hifi["volume"] is not None:
                hifi_status["volume"] = sendspin._hifi["volume"]
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
        watching.shelf_note(job, position_s if position_s is not None
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
    if state == "error" and job and err and jobs.job_get(job):
        jobs.job_set(job, stage="error", ok=False, msg=err)
    # Autoplay: catch the playing/paused -> ended edge exactly once (prev
    # is the heartbeat BEFORE this one, so a run of "ended" samples only
    # matches on the first) and, for an episode job with autoplay set,
    # hand the next one straight to start_play() without waiting for the
    # app or the phone to ask. The reply names the next job ("next"), so the
    # app can count down to it -- and cancel it -- instead of showing nothing
    # while it is found and buffered.
    next_up = None
    if (job and job.startswith("tv:") and state == "ended"
            and prev and prev.get("state") in ("playing", "paused")):
        try:
            ended = jobs.job_get(job) or {}
            if ended.get("autoplay"):
                tid, s, e = catalogue.tv_job_parts(job)
                # Worked out while the episode played (_remember_next_episode),
                # so this reply does not wait on the provider. Asked now only
                # for a job that predates that, e.g. across a server restart.
                known = ended.get("next_ep")
                if known is None:
                    known = catalogue._next_episode_info(tid, s, e)
                nxt = (known["s"], known["e"]) if known else None
                if nxt:
                    ns, ne = nxt
                    def _resolve():
                        en = streams.get_stream_tv(tid, ns, ne)
                        return en, en.get("runtime") or 45
                    nid = f"tv:{tid}:{ns}:{ne}"
                    print("autoplay: %s ended -> %s" % (job, nid), flush=True)
                    # Registered before the thread is away: the app starts
                    # polling this id off this very reply, and an id reused
                    # from an earlier play of the episode would otherwise
                    # still show that play's stage. It is also what
                    # /api/cancel finds while the stream is being resolved.
                    jobs.job_set(nid, stage="starting", got=0, target=0, pct=0,
                            ok=None, msg="Finding the next episode…")
                    threading.Thread(target=jobs._autoplay_start, args=(nid, _resolve),
                                     daemon=True).start()
                    next_up = {"job": nid, "s": ns, "e": ne, "name": known.get("name")}
                else:
                    print("autoplay: %s ended, no next episode" % job, flush=True)
        except Exception as ex:
            print("autoplay: %s failed: %s" % (job, ex), flush=True)
    reply = {"ok": True, "cmd": cmd}
    if next_up is not None:
        reply["next"] = next_up
    if sync is not None:
        reply["sync"] = sync
    if hifi_status is not None:
        reply["hifi_status"] = hifi_status
    return reply

def tv_playback_state():
    """3 = playing, 2 = paused, anything else idle.

    One source of truth for everything downstream -- playing_now(), cache_watch(),
    the calibration guard and /api/nowplaying all read it. The TV app wins when
    it is connected because it IS the player: its own state beats anything
    inferred from outside it, and it is already in hand, so no cache is wanted
    here.
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
    if last["state"] and time.time() - last["at"] < config.APP_GRACE:
        return {"playing": 3, "paused": 2}.get(last["state"])
    # Nothing else knows. adb's media-session list was read here once, but the
    # app registers no MediaSession, so it could only ever find other apps.
    return None

def launch(url, mid, pick, title, gen):
    """Hand the finished URL to the TV app.

    The app is the only player there is now. If it is not already in the
    foreground and adb is available, wake_app() brings it there; once it is
    fresh, the rest of this is the same handoff over the heartbeat channel
    either way.
    """
    app = app_fresh()
    if not app and config.ADB_ENABLED:
        ok, msg = wake_app()
        if not ok:
            return False, msg
        app = app_fresh()
    if app:
        jobs.job_set(mid, msg="Handing over to the TV app…")
        # Resume: the app seeks there itself once the stream is open. Only
        # present when the play route was given t=, so autoplay-next -- which
        # never sets one -- cannot carry the previous episode's offset.
        start_s = jobs.job_get(mid).get("start_s")
        seq = app_cmd("play", job=str(mid), url=url, title=title, pick=pick,
                      transcoded=bool(pick.get("transcoded")),
                      hifi=bool(sendspin._hifi["on"] and config.SENDSPIN_ENABLED),
                      hifi_centre=bool(sendspin._hifi["on"] and config.SENDSPIN_ENABLED and sendspin._hifi["film_centre"]),
                      **({"start_s": start_s} if start_s is not None else {}))
        print("app: play %s seq=%d -> %s" % (mid, seq, app["name"] or app["id"]),
              flush=True)
        deadline = time.time() + config.APP_HANDOFF_SECS
        while time.time() < deadline:
            if jobs.superseded(gen):
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
