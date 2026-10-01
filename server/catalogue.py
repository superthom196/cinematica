"""The browse wall: genres, series details and seasons, the candidate pools
behind every view, pages of tiles, search, and which episode comes next.
"""
import threading, time
from concurrent.futures import ThreadPoolExecutor
from providers import contract, gateway

import config, core, streams

# (_pool below replaces the old flat _movies cache)
_genres = {"at": 0, "data": [], "tag": None}
_genres_tv = {"at": 0, "data": [], "tag": None}    # TV genre ids differ from movie ids
_tvdet    = {}      # "<tag>@<id>" -> {"at":ts, ...series detail...}, 24h TTL
_tvseason = {}      # "<tag>@<id>:<n>" -> {"at":ts,"episodes":[...]}, 24h TTL

def _gw_kind(kind):
    """server.py's own convention is "movie"/"tv"; the contract speaks
    "movie"/"series" -- bridge the two here rather than push the contract's
    vocabulary through every call site."""
    return contract.KIND_SERIES if kind == "tv" else contract.KIND_MOVIE

def pool_served():
    """Films served across every view. Snapshot under the lock: get_page()
    inserts into _pool while this iterates, and a bare sum() over it would raise
    "dictionary changed size during iteration" -- turning the endpoint both UIs
    poll on a timer into a 500."""
    with core._lock:
        vals = list(_pool.values())
    return sum(len(v["served"]) for v in vals)

def get_genres(kind="movie"):
    cache = _genres_tv if kind == "tv" else _genres
    tag = gateway.cache_tag(contract.ROLE_CATALOGUE)
    with core._lock:
        if cache["data"] and cache.get("tag") == tag and time.time() - cache["at"] < 24 * 3600:
            return cache["data"]
    g = gateway.genres(_gw_kind(kind))
    with core._lock:
        cache.update(at=time.time(), data=g, tag=tag)
    return g

def tv_detail(tid):
    """Series detail, cached 24h the same way movie streams are. A series'
    IMDb id lives under its external_ids -- unlike a movie's, it is never a
    top-level field -- so it is read from there, whatever the provider."""
    tag = gateway.cache_tag(contract.ROLE_METADATA)
    key = "%s@%s" % (tag, tid)
    with core._lock:
        e = _tvdet.get(key)
        if e and time.time() - e["at"] < 24 * 3600:
            return e
    entry = gateway.details(tid, contract.KIND_SERIES)
    ext = entry.get("external_ids") or {}
    tr = streams.own_rating(entry)    # the series page's ★, as before providers
    e = {"at": time.time(), "id": entry.get("id"), "local_id": entry.get("local_id"),
         "kind": "tv",
         "title": entry.get("title"), "overview": entry.get("overview"),
         "tagline": entry.get("tagline"), "year": entry.get("year"),
         "runtime": entry.get("runtime"),
         "vote": tr[0] if tr else None, "votes": tr[1] if tr else None,
         "first_air": entry.get("first_air"), "last_air": entry.get("last_air"),
         "status": entry.get("status"),
         "genres": entry.get("genres") or [],
         "seasons": entry.get("seasons") or [],
         "backdrop": entry.get("backdrop"), "poster": entry.get("poster"),
         "ratings": entry.get("ratings") or {},
         "external_ids": ext,
         "imdb_id": ext.get("imdb")}
    with core._lock:
        _tvdet[key] = e
        core._evict(_tvdet, 24 * 3600, config.MAX_STREAM_ENTRIES)
    return e

def tv_season(tid, n):
    """One season's episodes, cached 24h. A provider lists a season's whole
    episode roster well before it airs, air date and all, so unaired
    episodes are dropped rather than offered as playable."""
    key = "%s@%s:%s" % (gateway.cache_tag(contract.ROLE_METADATA), tid, n)
    with core._lock:
        e = _tvseason.get(key)
        if e and time.time() - e["at"] < 24 * 3600:
            return e
    d = gateway.episodes(tid, n)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    episodes = []
    for ep in d.get("episodes") or []:
        air = ep.get("air")
        if not air or air > today:
            continue
        tr = streams.own_rating(ep)   # each episode row's ★, as before providers
        episodes.append({"season": ep.get("season"), "episode": ep.get("episode"),
                          "name": ep.get("name"), "overview": ep.get("overview"),
                          "runtime": ep.get("runtime"), "air": air,
                          "still": ep.get("still"), "vote": tr[0] if tr else None,
                          "ratings": ep.get("ratings") or {}})
    e = {"at": time.time(), "episodes": episodes}
    with core._lock:
        _tvseason[key] = e
        core._evict(_tvseason, 24 * 3600, config.MAX_STREAM_ENTRIES)
    return e

def tv_job_parts(job):
    """(title_id, season, episode) from a "tv:{id}:{s}:{e}" job id.

    rsplit from the right, not split from the left: a title id is now
    provider-qualified ("cinemeta:tt0903747") and carries its own colon, so
    unpacking four fields off a left split silently mis-parsed every episode
    job the moment ids stopped being bare numbers.
    """
    rest = job[3:] if job.startswith("tv:") else job
    tid, s, e = rest.rsplit(":", 2)
    return tid, int(s), int(e)


def next_episode(tid, s, e):
    """(season, episode) that follows (s, e) for autoplay, or None at the
    end of the show. Prefers the next aired episode in the same season;
    falls back to episode 1 of the next season if that season exists and
    has at least one aired episode. Provider errors are swallowed -- a
    failed lookup just means autoplay does not fire, not a broken heartbeat."""
    try:
        after = [ep["episode"] for ep in tv_season(tid, s)["episodes"]
                 if ep["episode"] > e]
        if after:
            return s, min(after)
        if any(sn["n"] == s + 1 for sn in tv_detail(tid)["seasons"]):
            nxt = [ep["episode"] for ep in tv_season(tid, s + 1)["episodes"]]
            if nxt:
                return s + 1, min(nxt)
        return None
    except Exception:
        return None

def _episode_name(tid, s, e):
    """The episode's title for the app's "Up next" countdown, or None. The
    season is already cached by next_episode(); a failure costs the label."""
    try:
        return next((ep.get("name") for ep in tv_season(tid, s)["episodes"]
                     if ep["episode"] == e), None)
    except Exception:
        return None

def _next_episode_info(tid, s, e):
    """{"s", "e", "name"} of the episode after (s, e), or False at the end of
    the show -- False rather than None so a stored answer of "nothing next"
    is told apart from no answer yet."""
    nxt = next_episode(tid, s, e)
    if not nxt:
        return False
    ns, ne = nxt
    return {"s": ns, "e": ne, "name": _episode_name(tid, ns, ne)}

_pool = {}     # genre key -> {"cands":[...], "ready":[...], "cursor":int, "at":ts}
# One lock per pool key, not the global _lock -- get_page's cursor/buf/served
# bookkeeping (and the network calls resolve_chunk makes while filling them)
# must be serialised PER VIEW so two requests for the same sort+genre can't
# duplicate or drop films, but different views still run concurrently. Lock
# objects are never evicted even though _pool entries are -- swapping the lock
# out from under a thread that is still inside it would reopen the same race.
_pool_locks = {}

def _pool_lock(key):
    with core._lock:
        lk = _pool_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _pool_locks[key] = lk
        return lk

# What a cold view is doing right now, per pool key, so a client can show a
# closing ring instead of a blank grid: a biased film pool is ~60 browse
# pages and takes minutes, and to a guest a blank wall looks like the
# service is broken. Read under _lock only -- never the pool lock, which the
# build holds.
_progress = {}

def _note(key, **kw):
    if not key:
        return
    with core._lock:
        is_new = key not in _progress
        p = _progress.setdefault(key, {"started": time.time()})
        p.update(kw)
        p["at"] = time.time()
        if is_new:               # one per pool, so held to the pools' own limits
            core._evict(_progress, config.TTL_LIST, config.MAX_POOL_ENTRIES)

def view_progress(key):
    """{"stage","fraction","label","elapsed"} for the progress endpoint."""
    with core._lock:
        p = dict(_progress.get(key) or {})
        warm = key in _pool and _pool[key].get("served")
    if not p:
        return {"stage": "ready" if warm else "starting", "fraction": 1.0 if warm else 0.0,
                "label": "" if warm else "Gathering the hottest streams", "elapsed": 0}
    stage = p.get("stage", "starting")
    done, total = p.get("done", 0), max(1, p.get("total", 1))
    if stage == "gathering":
        frac = 0.8 * min(1.0, done / total)
        label = "Gathering the hottest streams"
    elif stage == "checking":
        frac = 0.8 + 0.2 * min(1.0, done / max(1, config.RESOLVE_FIRST + config.RESOLVE_CHUNK))
        label = "Checking what's actually streamable"
    elif stage == "ready":
        frac, label = 1.0, ""
    else:
        frac, label = 0.0, "Gathering the hottest streams"
    return {"stage": stage, "fraction": round(frac, 3), "label": label,
            "elapsed": round(time.time() - p.get("started", time.time()), 1)}

def _key(ids, sort="top", ex=None, kind="movie", bias=False):
    ex = ex or []
    base = "%s|%s|%s" % (sort or "top", ",".join(map(str, ids)) or "all",
                         ",".join(map(str, ex)) or "-")
    # tv gets its own namespace so the two kinds never collide
    tagged = ("tv:" if kind == "tv" else "") + ("bias:" if bias else "") + base
    # Prefixed with the catalogue provider's cache_tag: a provider swap or a
    # config edit must never keep serving a pool gathered under the old one.
    return "%s|%s" % (gateway.cache_tag(contract.ROLE_CATALOGUE), tagged)

def this_year():
    return time.localtime().tm_year

def recency_bonus(year):
    try:
        age = this_year() - int(year)
    except (TypeError, ValueError):
        return 0.0
    return config.RECENCY_W * max(0.0, 1.0 - age / config.RECENCY_SPAN)

def home_bonus(c):
    """The bias's lift for a home-country title (HOME_W). Only a biased pool
    tags candidates "home" (by the tier that fetched them -- a catalogue's
    browse results carry no origin_countries worth reading per-entry), so
    it is 0 everywhere else."""
    return config.HOME_W if c.get("home") else 0.0

def build_pool(ids, sort="top", ex=None, kind="movie", bias=False, key=None, errs=None):
    """Candidate list ordered by rating. A catalogue provider rarely sorts by
    IMDb rating and often does not expose an IMDb id on its list endpoints,
    so the pool is ordered by whatever rating it reports and each film's REAL
    IMDb rating is attached once resolved, then used to order within each
    page.

    kind="tv" browses the series catalogue instead: series need a lower
    vote-count floor than film (TV_MIN_VOTES) since a provider's TV vote
    counts typically run much lower, and "recent" filters on first-air date
    rather than release date.

    bias=True builds the pool in tiers (HOME_COUNTRIES at the normal floor
    and everything else at the big floor, both in BIAS_LANG, then any
    language at WORLD_MIN_VOTES -- see BIAS) and merges them by rating plus
    home bonus: the pool resolves in order, so tiers appended one after the
    other would make the first pages all home tier and the last pages all
    world tier.

    Every tier here is expressed as browse filters, and not every provider
    can apply all of them -- see the supports_filters() check below, which
    drops a tier rather than let it silently issue the same query as another.
    """
    cands = []
    # "balanced" needs both ends of the catalogue in the pool, otherwise the
    # recency bonus has no recent films to lift. Pull half from each.
    sources = [("top", config.POOL_MAX)] if sort == "top" else \
              [("recent", config.POOL_MAX)] if sort == "recent" else \
              [("top", config.POOL_MAX // 2), ("recent", config.POOL_MAX // 2)]
    is_tv = kind == "tv"
    gw_kind = _gw_kind(kind)
    top_vote_floor = config.TV_MIN_VOTES if is_tv else 500
    recent_vote_floor = config.TV_MIN_VOTES if is_tv else 200
    # (origin countries or None, top floor, recent floor, home?, BIAS_LANG only?)
    tiers = [(None, top_vote_floor, recent_vote_floor, False, False)]
    supported = gateway.supports_filters()
    if bias and "min_votes" not in supported:
        # Every bias tier below differs from the single base tier (and from
        # each other) ONLY by its vote-count floor. A provider that cannot
        # filter by votes would issue the identical, unfiltered query for
        # all of them, and the per-block dedup further down would then
        # silently collapse them into one -- disabling the bias with no
        # error anywhere. Degrade to the base tier instead; home_bonus below
        # still applies per-entry, so a home title is still favoured on
        # merge even without a dedicated tier for it.
        bias = False
    if bias:
        big = config.TV_BIG_MIN_VOTES if is_tv else config.MOVIE_BIG_MIN_VOTES
        # a recent big-tier title with half the floor is already a hit
        tiers = [(None, big, max(recent_vote_floor, big // 2), False, True)]
        if config.HOME_COUNTRIES and "origin_countries" in supported:
            tiers.insert(0, (config.HOME_COUNTRIES, top_vote_floor, recent_vote_floor, True, True))
        # else: the home tier's only distinguishing filter is
        # origin_countries -- without it the tier would issue the same query
        # as the one above and get deduped into it, so it is left out rather
        # than spending a whole tier's page budget on a no-op.
        if config.WORLD_MIN_VOTES:
            # no halved floor here: it would let a two-year-old anime in
            tiers.append((None, config.WORLD_MIN_VOTES, config.WORLD_MIN_VOTES, False, False))
    # progress, in browse pages: a tier that runs dry early jumps ahead by the
    # pages it did not need, so the ring only ever closes, never reopens
    share = {cap: min(30, -(-cap // 20)) for _, cap in sources}
    pages_total = sum(share[cap] for _, cap in sources) * len(tiers)
    pages_base = 0
    _note(key, stage="gathering", done=0, total=pages_total)
    for src, cap in sources:
        block = []
        in_block = set()   # a title clearing two tiers keeps its first (home) copy
        for countries, top_floor, recent_floor, home, lang in tiers:
            page = 1
            taken = 0
            while taken < cap and page <= 30:
                _note(key, done=pages_base + page - 1)
                floor = recent_floor if src == "recent" else top_floor
                filters = {}
                if "min_votes" in supported:
                    filters["min_votes"] = floor
                if countries:
                    filters["origin_countries"] = list(countries)
                if lang and config.BIAS_LANG:
                    filters["original_language"] = config.BIAS_LANG
                if src == "recent":
                    filters["released_after"] = "%d-01-01" % (this_year() - config.RECENT_YEARS)
                if ids:
                    filters["genre_ids"] = [str(g) for g in ids]
                if ex:
                    filters["exclude_genre_ids"] = [str(g) for g in ex]
                try:
                    d = gateway.browse(gw_kind, page, config.PAGE, sort, filters)
                # Not `as ex`: that is the excluded-genres argument, and Python
                # deletes an except target when the block ends -- the next
                # tier's `if ex:` then raised UnboundLocalError and 500ed the
                # very wall this handler exists to keep alive.
                except contract.ProviderError as perr:
                    # A provider failure must never 500 a browse, so this page
                    # is abandoned like an empty one -- but it is REMEMBERED.
                    # Swallowing it outright left /api/movies answering 200 with
                    # an empty list, which looks exactly like "your filters
                    # matched nothing" and sent people hunting through their
                    # genre settings while the real problem was an add-on that
                    # had stopped answering.
                    if errs is not None:
                        errs.append(perr)
                    break
                res = d.get("entries") or []
                if not res:
                    break
                before = taken
                for r in res:
                    if r["id"] in in_block:
                        continue
                    in_block.add(r["id"])
                    c = _api_entry(r, kind)
                    c["home"] = home
                    block.append(c)
                    taken += 1
                # A real add-on that ignores page/skip returns the same page
                # forever; without this, a page that adds nothing new burns
                # up to 30 pointless fetches per tier instead of stopping.
                if taken == before:
                    break
                page += 1
            pages_base += share[cap]
        if len(tiers) > 1:
            # merge the tiers by merit, then hold this source to its share of
            # the pool so a "balanced" pool still gets its recent half.
            # Merit is the catalogue's own rating (vote, from _api_entry()):
            # a list entry has no IMDb rating to go by, and sorting on one
            # scored every title 0 + home bonus, so the home tier filled the
            # whole pool and the rest of the world was cut off.
            block.sort(key=lambda c: (c.get("vote") or 0) + home_bonus(c),
                       reverse=True)
            block = block[:cap]
        cands.extend(block)
    _note(key, done=pages_total)   # tiers usually run dry early: close the gathering arc
    # de-dup, keep first occurrence
    seen, out = set(), []
    for c in cands:
        if c["id"] in seen:
            continue
        seen.add(c["id"]); out.append(c)
    return out[:config.POOL_MAX]

def view_key(genres=None, sort="top", exclude=None, kind="movie", bias=None):
    """The normalised (ids, ex, bias, pool key) for a browse request -- one
    place, so /api/movies and /api/movies/progress can never disagree."""
    sort = sort if sort in config.SORTS else "top"
    ids = sorted(set(int(g) for g in (genres or []) if str(g).strip()))
    ex  = sorted(set(int(g) for g in (exclude or []) if str(g).strip()) - set(ids))
    # bias: None means "the server's default" (BIAS)
    bias = config.BIAS if bias is None else bool(bias)
    return ids, ex, bias, _key(ids, sort, ex, kind, bias)

def _api_entry(e, kind):
    """A catalogue entry in the clients' vocabulary, which is the one they
    were written against before providers were split out:

      * kind "tv", not the contract's "series". Both the TV app and the web
        page open the seasons-and-episodes page on kind == "tv"; given
        "series" they opened every series as a film.
      * vote/votes flat on the entry: the catalogue's own rating, which the
        tiles show and rank() falls back to when there is no IMDb rating.
        The contract files it by name under ratings, where no client looks.
      * numeric genre ids as numbers: the TV's model is List<Int>, and the
        contract turns every id into a string.
    """
    e = dict(e)
    e["kind"] = kind
    e["genre_ids"] = [int(g) if str(g).isdigit() else g for g in (e.get("genre_ids") or [])]
    if e.get("vote") is None:
        tr = streams.own_rating(e)
        if tr:
            e["vote"], e["votes"] = tr
    return e

def _tile(m, r):
    """Entry `m` plus its resolved stream entry `r`, as a wall/search tile.

    A list entry has no IMDb id, so its IMDb rating comes from the details
    get_stream()/get_stream_tv() fetched -- which is where the pre-provider
    server took both from, too. Merged into m's ratings before anything reads
    them, so the rating floor and rank() see the same numbers the tile shows.
    """
    m = dict(m)
    m["ratings"] = dict(m.get("ratings") or {}, **(r.get("ratings") or {}))
    ir = streams.rating_of(m, "imdb")
    m["imdb"] = {"rating": ir[0], "votes": ir[1],
                 "id": (m.get("external_ids") or {}).get("imdb") or r.get("imdb_id")} if ir else None
    m["stream"] = {"pick": r["pick"], "count": r.get("count"), "url": streams.stream_url(r["pick"])}
    return m

def get_page(genres=None, offset=0, limit=None, sort="top", exclude=None, kind="movie", bias=None):
    """
    Return `limit` playable films (or, kind="tv", series) starting at
    `offset`, ordered by real IMDb rating. Resolving happens lazily as you
    scroll.

    Ordering needs care: candidates arrive in the catalogue's own order, which
    only approximates IMDb. Sorting each batch in isolation made the grid
    sawtooth (an 8.9 appearing after a 6.7). Instead we keep a lookahead buffer
    of resolved-but-unserved films, sort THAT by IMDb rating, and serve the top
    slice. Once served, a film's position is frozen, so scrolling never
    reshuffles what you have already seen.
    """
    limit = limit or config.PAGE
    sort = sort if sort in config.SORTS else "top"
    ids, ex, bias, key = view_key(genres, sort, exclude, kind, bias)
    # Everything below reads and mutates this view's cursor/buf/served, and
    # resolve_chunk() makes the network calls that fill them. A per-key lock
    # serialises that against other requests for the SAME view (so pages can
    # never duplicate or drop films) while a different sort/genre combination
    # still runs fully concurrently.
    with _pool_lock(key):
        with core._lock:
            e = _pool.get(key)
        stale = (not e) or (time.time() - e["at"] > config.TTL_LIST)
        if stale:
            perr = []
            cands = build_pool(ids, sort, ex, kind, bias, key, errs=perr)
            # Hold the reference rather than re-indexing _pool below: _evict()
            # caps by count as well as age, so a concurrent rebuild for another
            # view could delete this key between the two lock acquisitions and
            # turn an ordinary page request into a 500.
            st = {"cands": cands, "served": [], "buf": [], "cursor": 0,
                  "at": time.time(),
                  # Only worth reporting when it actually cost the reader
                  # something: a tier that failed after the pool already filled
                  # is not what the grid being empty is about.
                  "err": (contract.redact(perr[0].message, gateway.secret_values())
                          if perr and not cands else None)}
            with core._lock:
                _pool[key] = st
                core._evict(_pool, config.TTL_LIST, config.MAX_POOL_ENTRIES)
        else:
            st = e

        def resolve_chunk(size=config.RESOLVE_CHUNK):
            chunk = st["cands"][st["cursor"]:st["cursor"] + size]
            if not st["served"]:
                _note(key, stage="checking", done=st["cursor"], total=len(st["cands"]))
            st["cursor"] += len(chunk)
            if not chunk:
                return
            # entry=x: get_stream() builds the identity straight from the
            # catalogue's own entry when it already carries the IMDb id and
            # runtime, and fetches details when it does not.
            with ThreadPoolExecutor(max_workers=3) as ex:
                if kind == "tv":
                    results = list(ex.map(lambda x: streams.get_stream_tv(x["id"], 1, 1), chunk))
                else:
                    results = list(ex.map(lambda x: streams.get_stream(x["id"], entry=x), chunk))
            for m, r in zip(chunk, results):
                if not r.get("pick"):
                    continue
                m = _tile(m, r)
                if sort in ("balanced", "recent") and not streams.passes_rating_floor(m):
                    # a recency bonus must never be a route in for badly-rated
                    # films -- but passes_rating_floor() already lets a title
                    # with NO imdb rating through, so a provider that never
                    # reports ratings at all cannot empty the whole pool the
                    # way a hard floor used to.
                    continue
                m["boost"] = round((recency_bonus(m.get("year")) if sort == "balanced" else 0)
                                   + (home_bonus(m) if bias else 0), 2)
                st["buf"].append(m)

        def rank(x):
            im = x.get("imdb")
            base = im["rating"] if im else (x.get("vote") or 0)
            if sort == "balanced":
                base += recency_bonus(x.get("year"))
            if bias:
                base += home_bonus(x)
            return base

        # serve from `served` if this page was already decided
        while len(st["served"]) < offset + limit and st["cursor"] < len(st["cands"]):
            # keep a lookahead of 2 pages so the sort has something to choose from
            while len(st["buf"]) < limit * 2 and st["cursor"] < len(st["cands"]):
                # A cold view resolved RESOLVE_CHUNK candidates before serving
                # anything, so the very first row cost ~19s. Resolve a smaller
                # first batch: the pool is already ordered by the catalogue's
                # own rating, so the top few are the same films either way,
                # and every later pass is full size -- which keeps the IMDb
                # re-sort choosing from a wide field, and happens ahead of the
                # reader rather than in front of them. If the small batch
                # yields too few playable films the loop simply goes round
                # again at full size.
                resolve_chunk(config.RESOLVE_FIRST if not st["served"] and not st["buf"]
                              else config.RESOLVE_CHUNK)
            if not st["buf"]:
                break
            st["buf"].sort(key=rank, reverse=True)
            take = st["buf"][:limit]
            st["buf"] = st["buf"][limit:]
            st["served"].extend(take)
        # pool exhausted: flush whatever is left, still rating-ordered
        if st["cursor"] >= len(st["cands"]) and st["buf"] and len(st["served"]) < offset + limit:
            st["buf"].sort(key=rank, reverse=True)
            st["served"].extend(st["buf"])
            st["buf"] = []

        _note(key, stage="ready")
        # st["buf"] holds resolved films from the lookahead that are still owed to the reader
        more = st["cursor"] < len(st["cands"]) or len(st["served"]) > offset + limit or bool(st["buf"])
        return (st["served"][offset:offset + limit], more, st["cursor"],
                len(st["cands"]), st.get("err"))


def search_movies(q, limit=24, on_found=None, on_movie=None, kind="movie"):
    """Find films (or, kind="tv", series) by title, franchise/theme, or the
    people in them -- whatever the catalogue provider's own search covers.
    A single gateway.search() call replaces what used to be three separate
    indexes (title, keyword->discover, person->discover) combined by hand;
    the provider's relevance order is trusted as-is and not re-ranked.

    Results with no playable stream are dropped, so candidates are resolved in
    chunks until `limit` playable films are found rather than resolving a fixed
    number and returning however few survive.
    """
    is_tv = kind == "tv"
    try:
        cands = (gateway.search(_gw_kind(kind), q, config.SEARCH_POOL).get("entries") or [])[:config.SEARCH_POOL]
    except contract.ProviderError:
        cands = []

    if on_found:
        on_found(len(cands))
    out, i = [], 0
    while i < len(cands) and len(out) < limit:
        chunk = cands[i:i + 8]
        i += len(chunk)
        # Iterate the map generator rather than list()-ing it: it yields in the
        # order submitted, as each completes, so a result reaches the page the
        # moment it resolves instead of waiting for its whole chunk. Order is
        # relevance order, so it must not be shuffled by completion time.
        fn = (lambda x: streams.get_stream_tv(x["id"], 1, 1)) if is_tv else (lambda x: streams.get_stream(x["id"], entry=x))
        with ThreadPoolExecutor(max_workers=3) as ex:
            for m, r in zip(chunk, ex.map(fn, chunk)):
                if not r.get("pick"):
                    continue                  # unplayable: hidden, not shown greyed
                m = _tile(_api_entry(m, kind), r)
                m["boost"] = 0
                out.append(m)
                if on_movie:
                    on_movie(m)          # reaches the page the moment it resolves
                if len(out) >= limit:
                    break
    return out, len(cands), i
