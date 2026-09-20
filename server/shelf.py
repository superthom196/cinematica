"""Favourites / watched / resume state, kept outside server.py so the
persistence rules (what counts as "pinned", when a resume point survives a
stop) live in one place clients and server.py both defer to. See
server/tests/../.. contract doc (shelf-contract.md) for the binding rules;
this module is the implementation of it, not a second source of truth.

Pure stdlib, Python 3.9+. No import of server.py -- server.py imports this,
never the other way, so this module can be unit tested in isolation.
"""

import json
import os
import threading
import time

# Constants per the shelf contract. Clients mirror PIN_FROM / DONE_AT only;
# everything else is a server-side implementation detail.
PIN_FROM = 0.20       # below this a stop is an early bail: never pinned
DONE_AT = 0.90        # at/after this (or state "ended") the thing is watched
FADE_DAYS = 30        # a pinned title untouched this long unpins
RESUME_MIN_S = 60     # positions under a minute are not worth a resume point
RESUME_BACK_S = 5     # resume_s handed to clients is already (pos - 5)
FLUSH_S = 30          # disk write throttle

_lock = threading.Lock()
_path = None
_state = {"v": 1, "titles": {}}
_last_save = 0.0
_dirty = False


def _new_rec():
    return {
        "kind": None,
        "snap": {"title": None, "year": None, "poster": None, "imdb_id": None},
        "fav": None,
        "watched": None,
        "dropped": None,
        "touched": None,
        "pos": None,
        "aired": None,
        "has_next": None,
        "eps": {},
        "muted": [],
    }


def init(path):
    """Point the module at a JSON file and load it. A missing or corrupt
    file is not fatal -- an unreadable shelf just starts empty rather than
    taking the server down."""
    global _path, _state, _last_save, _dirty
    with _lock:
        _path = path
        _state = {"v": 1, "titles": {}}
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("titles"), dict):
                _state = {"v": 1, "titles": data["titles"]}
        except Exception:
            pass
        # Reset the throttle window so the first real save() after init
        # (i.e. after an actual mutation) is never blocked by a timer left
        # over from a previous process.
        _last_save = 0.0
        _dirty = False


def save(force=False):
    """Atomic write: json.dump to path+'.tmp' then os.replace. Throttled to
    once per FLUSH_S unless force=True, and always a no-op if nothing has
    changed since the last write -- force only waives the throttle, not the
    "nothing to do" check, so a shutdown-time flush doesn't touch disk for
    no reason."""
    global _last_save, _dirty
    with _lock:
        if _path is None:
            return
        if not _dirty:
            return
        now = time.time()
        if not force and (now - _last_save) < FLUSH_S:
            return
        try:
            tmp = _path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(_state, f)
            os.replace(tmp, _path)
        except Exception:
            return
        _last_save = now
        _dirty = False


def parse_job(job):
    """film: (job, None, None). "tv:{tid}:{s}:{e}" parsed from the right so
    a title id that itself contains colons (every provider-qualified id
    does) still comes out intact. Anything else is garbage."""
    if not isinstance(job, str) or not job:
        return (None, None, None)
    if not job.startswith("tv:"):
        return (job, None, None)
    body = job[3:]
    parts = body.rsplit(":", 2)
    if len(parts) != 3 or not parts[0]:
        return (None, None, None)
    tid, s, e = parts
    try:
        return (tid, int(s), int(e))
    except ValueError:
        return (None, None, None)


def _is_muted(rec, job, tid, s):
    muted = rec.get("muted") or []
    if job in muted:
        return True
    if s is not None and ("tv:%s:*" % tid) in muted:
        return True
    return False


def begin(job):
    """A new play started for this exact job: un-mute it. An episode play
    also clears a series-wide mute left by drop(title_id) -- starting one
    episode is evidence the viewer is back, not just replaying the one
    thing they dropped."""
    global _dirty
    tid, s, e = parse_job(job)
    if tid is None:
        return
    with _lock:
        rec = _state["titles"].get(tid)
        if rec is None:
            return
        muted = rec.get("muted") or []
        changed = False
        if job in muted:
            muted.remove(job)
            changed = True
        if s is not None:
            star = "tv:%s:*" % tid
            if star in muted:
                muted.remove(star)
                changed = True
        if changed:
            rec["muted"] = muted
            _dirty = True


def _clear_unwatched_positions(rec):
    rec["pos"] = None
    for ep in rec.get("eps", {}).values():
        if not ep.get("watched"):
            ep["s"] = None
            ep["dur"] = None
            ep["at"] = None


def drop(title_id=None, job=None, now=None):
    """"Done with this": clears every in-progress position for the WHOLE
    title (film position, every unwatched episode) and stamps `dropped`,
    but only mutes the specific target given -- an exact job if one was
    passed, else the title id plus a series-wide sentinel -- so a later
    begin() on that same scope is what makes the title eligible again."""
    global _dirty
    now_ts = now if now is not None else time.time()
    if job:
        tid, _s, _e = parse_job(job)
        if tid is None:
            return
        mute_add = [job]
    elif title_id:
        tid = title_id
        mute_add = [tid, "tv:%s:*" % tid]
    else:
        return
    with _lock:
        rec = _state["titles"].setdefault(tid, _new_rec())
        rec["dropped"] = now_ts
        _clear_unwatched_positions(rec)
        rec["muted"] = list(set(rec.get("muted") or []) | set(mute_add))
        _dirty = True


def _merge_snap(rec, snap):
    """Fill in missing snapshot fields only. Never overwrite a value the
    record already has -- in particular never clobber a good poster with a
    None from a caller that didn't have one to hand this time."""
    if not snap:
        return False
    dst = rec.setdefault(
        "snap", {"title": None, "year": None, "poster": None, "imdb_id": None}
    )
    changed = False
    for k in ("title", "year", "poster", "imdb_id"):
        if not dst.get(k) and snap.get(k) is not None:
            dst[k] = snap[k]
            changed = True
    return changed


def note_progress(
    job,
    pos_s,
    dur_s,
    state,
    runtime_s=None,
    snap=None,
    kind=None,
    aired=None,
    has_next=None,
    now=None,
):
    """Record one heartbeat/stop event. Returns whether anything in the
    stored record changed, so the caller knows whether a save() is worth
    scheduling. The caller (not this function) is responsible for calling
    save() -- this just mutates in-memory state under the lock."""
    global _dirty
    if state not in ("playing", "paused", "ended"):
        return False
    tid, s, e = parse_job(job)
    if tid is None or pos_s is None:
        return False

    with _lock:
        existing = _state["titles"].get(tid)
        if existing is not None and _is_muted(existing, job, tid, s):
            return False

        now_ts = now if now is not None else time.time()
        rec = _state["titles"].setdefault(tid, _new_rec())

        changed = False
        for key, val in (("kind", kind), ("aired", aired), ("has_next", has_next)):
            if val is not None and rec.get(key) != val:
                rec[key] = val
                changed = True
        if _merge_snap(rec, snap):
            changed = True

        # Effective duration: a still-converting film reports "converted so
        # far" as its duration, which understates the real length. If we
        # know the true runtime and the reported duration looks like that
        # partial-conversion figure, trust the runtime instead.
        eff_dur = dur_s
        if runtime_s is not None and (dur_s is None or dur_s < 0.8 * runtime_s):
            eff_dur = runtime_s
        usable = eff_dur is not None and eff_dur > 0

        is_ep = e is not None
        ep = None
        if is_ep:
            key = "%d:%d" % (s, e)
            ep = rec["eps"].setdefault(
                key, {"s": None, "dur": None, "at": None, "watched": None}
            )

        watched_now = state == "ended" or (usable and pos_s / eff_dur >= DONE_AT)
        if watched_now:
            if is_ep:
                ep["watched"] = now_ts
                ep["s"] = None
                ep["dur"] = None
                ep["at"] = None
            else:
                rec["watched"] = now_ts
                rec["pos"] = None
            rec["touched"] = now_ts
            _dirty = True
            return True

        if not usable:
            # No duration worth trusting: keep the bare position (dur 0)
            # so a resume point is still possible, but this can never be
            # classified as pinned or watched by ratio.
            if is_ep:
                ep["s"] = pos_s
                ep["dur"] = 0
                ep["at"] = now_ts
            else:
                rec["pos"] = {"s": pos_s, "dur": 0, "at": now_ts}
            rec["touched"] = now_ts
            rec["dropped"] = None
            _dirty = True
            return True

        if pos_s < RESUME_MIN_S:
            # Not worth a resume point; leave any existing one alone.
            if changed:
                _dirty = True
            return changed

        if is_ep:
            ep["s"] = pos_s
            ep["dur"] = eff_dur
            ep["at"] = now_ts
        else:
            rec["pos"] = {"s": pos_s, "dur": eff_dur, "at": now_ts}
        rec["touched"] = now_ts
        rec["dropped"] = None
        _dirty = True
        return True


def set_fav(title_id, on, snap=None, now=None):
    global _dirty
    now_ts = now if now is not None else time.time()
    with _lock:
        rec = _state["titles"].setdefault(title_id, _new_rec())
        rec["fav"] = now_ts if on else None
        _merge_snap(rec, snap)
        # A title favourited but never played has no other source for its
        # kind, and without one the favourites wall cannot tell a series from
        # a film -- the client would open the wrong detail screen.
        if not rec.get("kind") and snap and snap.get("kind") in ("movie", "tv"):
            rec["kind"] = snap["kind"]
        _dirty = True


def set_watched(title_id, on, s=None, e=None, now=None):
    """on=False clears the watched stamp (film or the one episode). on=True
    for a whole series (no s/e) marks every episode already recorded as
    watched and sets rec["watched"] too, so view() can honour either the
    explicit stamp or the aired-count rule for a series."""
    global _dirty
    now_ts = now if now is not None else time.time()
    with _lock:
        rec = _state["titles"].setdefault(title_id, _new_rec())
        if s is not None and e is not None:
            key = "%d:%d" % (s, e)
            ep = rec["eps"].setdefault(
                key, {"s": None, "dur": None, "at": None, "watched": None}
            )
            if on:
                ep["watched"] = now_ts
                ep["s"] = None
                ep["dur"] = None
                ep["at"] = None
            else:
                ep["watched"] = None
        else:
            if on:
                rec["watched"] = now_ts
                rec["pos"] = None
                for ep in rec["eps"].values():
                    ep["watched"] = now_ts
                    ep["s"] = None
                    ep["dur"] = None
                    ep["at"] = None
            else:
                rec["watched"] = None
        _dirty = True


def _resume_from(pos, dur):
    """Shared film/episode resume rule: only when pos is worth resuming and
    it isn't already past the "done" threshold (dur 0 counts as unknown,
    which still permits a resume point but never a ratio classification)."""
    if pos is None or pos < RESUME_MIN_S:
        return None
    if dur and dur > 0 and pos / dur >= DONE_AT:
        return None
    return max(0, int(pos - RESUME_BACK_S))


def view(title_id, now=None):
    now_ts = now if now is not None else time.time()
    default = {
        "fav": False,
        "watched": False,
        "pinned": False,
        "progress": None,
        "resume_s": None,
        "next": None,
    }
    with _lock:
        rec = _state["titles"].get(title_id)
        if rec is None:
            return default
        # Work from a value, not the live dict, so nothing here needs to
        # keep holding the lock past this point.
        rec = dict(rec)
        rec["eps"] = dict(rec.get("eps") or {})

    is_series = rec.get("kind") == "tv" or bool(rec.get("eps"))
    dropped = rec.get("dropped") is not None
    touched = rec.get("touched")
    faded = touched is None or (now_ts - touched) > FADE_DAYS * 86400
    fav = rec.get("fav") is not None

    if not is_series:
        watched = rec.get("watched") is not None
        pos = rec.get("pos")
        progress = None
        resume_s = None
        pinned = False
        if pos:
            s = pos.get("s")
            dur = pos.get("dur") or 0
            if dur > 0:
                progress = s / dur
                pinned = (
                    PIN_FROM <= progress < DONE_AT and not dropped and not faded
                )
            resume_s = _resume_from(s, dur)
        return {
            "fav": fav,
            "watched": watched,
            "pinned": pinned,
            "progress": progress,
            "resume_s": resume_s,
            "next": None,
        }

    # Series.
    eps = rec.get("eps", {})
    watched_count = 0
    watched_pairs = []
    in_progress = None  # (s, e, ep) with the latest "at"
    for key, ep in eps.items():
        if ep.get("watched"):
            watched_count += 1
            try:
                sp, ep_n = key.split(":")
                watched_pairs.append((int(sp), int(ep_n)))
            except ValueError:
                pass
        elif ep.get("at") is not None:
            if in_progress is None or ep["at"] > in_progress[2]["at"]:
                try:
                    sp, ep_n = key.split(":")
                    in_progress = (int(sp), int(ep_n), ep)
                except ValueError:
                    pass

    aired = rec.get("aired")
    watched = bool(rec.get("watched")) or (
        isinstance(aired, int) and aired > 0 and watched_count >= aired
    )

    progress = None
    next_obj = None
    if in_progress is not None:
        s_i, e_i, ep = in_progress
        dur = ep.get("dur") or 0
        pos = ep.get("s")
        if dur > 0 and pos is not None:
            progress = pos / dur
        next_obj = {"s": s_i, "e": e_i, "resume_s": _resume_from(pos, dur)}
    elif watched_pairs and rec.get("has_next") is True:
        s_h, e_h = max(watched_pairs)
        next_obj = {"s": s_h, "e": e_h + 1, "resume_s": None}

    pinned = (
        not dropped
        and not faded
        and (
            in_progress is not None
            or (watched_count >= 2 and rec.get("has_next") is True)
        )
    )

    return {
        "fav": fav,
        "watched": watched,
        "pinned": pinned,
        "progress": progress,
        "resume_s": None,
        "next": next_obj,
    }


def episode_view(title_id, s, e):
    with _lock:
        rec = _state["titles"].get(title_id)
        ep = None
        if rec is not None:
            ep = (rec.get("eps") or {}).get("%d:%d" % (s, e))
        ep = dict(ep) if ep else None
    if ep is None:
        return {"watched": False, "progress": None, "resume_s": None}
    watched = bool(ep.get("watched"))
    dur = ep.get("dur") or 0
    pos = ep.get("s")
    progress = pos / dur if (pos is not None and dur > 0) else None
    resume_s = None if watched else _resume_from(pos, dur)
    return {"watched": watched, "progress": progress, "resume_s": resume_s}


def decorate(item, now=None):
    """Tolerates items without an "id" -- view(None) falls through the
    unknown-id default, so there's nothing special to branch on here."""
    item["shelf"] = view(item.get("id") if isinstance(item, dict) else None, now=now)
    return item


def _snap_item(tid, rec, kind, now_ts):
    snap = rec.get("snap") or {}
    return {
        "id": tid,
        "kind": kind,
        "title": snap.get("title"),
        "year": snap.get("year"),
        "poster": snap.get("poster"),
        "imdb_id": snap.get("imdb_id"),
        "shelf": view(tid, now=now_ts),
    }


def pins(kind, now=None):
    now_ts = now if now is not None else time.time()
    with _lock:
        items = list(_state["titles"].items())
    rows = []
    for tid, rec in items:
        if rec.get("kind") != kind:
            continue
        if not view(tid, now=now_ts)["pinned"]:
            continue
        rows.append((rec.get("touched") or 0, _snap_item(tid, rec, kind, now_ts)))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [item for _, item in rows]


def favourites(now=None):
    now_ts = now if now is not None else time.time()
    with _lock:
        items = list(_state["titles"].items())
    rows = []
    for tid, rec in items:
        if rec.get("fav") is None:
            continue
        rows.append((rec["fav"], _snap_item(tid, rec, rec.get("kind"), now_ts)))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [item for _, item in rows]


def snapshot_needed(title_id):
    with _lock:
        rec = _state["titles"].get(title_id)
        if rec is None:
            return True
        return not (rec.get("snap") or {}).get("poster")
