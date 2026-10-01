"""Feeds shelf.py (watchlist, watched, resume points) from what the players
report, and decorates titles with it.
"""
import math, urllib.parse
import shelf

import config, core, catalogue, jobs

# Favourites / watched / resume, persisted beside nowplaying.json for the same
# reason: it is state about what this household is in the middle of, and a
# service restart (every deploy is one) must not lose where a film got to.
# shelf.py owns every rule about it; this file only says where the file lives
# and feeds it what the players report.
shelf.init(config.SHELF_FILE)

def _pool_item(tid):
    """This title as the browse pool already holds it, or None.

    Memory only, deliberately: the two callers are a play start and the
    /api/movies pinned row, and neither is worth a network fetch. A pool item
    carries the poster/year/external ids a shelf snapshot wants AND the
    stream/rating/quality fields a wall tile wants, which the snapshot alone
    can never have.
    """
    with core._lock:
        pools = list(catalogue._pool.values())
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
            det = catalogue.tv_detail(tid)
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
        out["shelf_has_next"] = catalogue.next_episode(tid, s, e) is not None
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
    j = jobs.job_get(job)
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
