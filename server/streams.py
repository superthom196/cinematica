"""Which source plays a title: ratings, audio-language matching, scoring,
ranking, direct (non-torrent) sources, and the cached per-film and
per-episode stream lookups.
"""
import time
from providers import contract, gateway

import config, core, mediaprobe, netprofile, catalogue

# Cinematica used to download IMDb's whole daily ratings dataset on startup,
# because the old fixed catalogue couldn't sort by IMDb rating and didn't
# return an IMDb id on its list endpoints. That was a third-party bulk
# download nobody asked for, and a fresh install must not make it. Ratings
# now arrive named, on the entry itself, from whichever provider supplied
# the title -- Cinemeta carries imdbRating inline, and a metadata provider
# that wants the dataset can still fetch it itself if its owner turns that
# on.
#
# Named, because "the rating" stopped meaning anything once the catalogue became
# pluggable: MIN_RATING is an IMDb floor on an IMDb ten-point scale and must not
# be applied to some other provider's number.

def rating_of(entry, name="imdb"):
    """(value, votes) for a named rating, or None. votes may be None."""
    r = ((entry or {}).get("ratings") or {}).get(name)
    if not r:
        return None
    v = r.get("value")
    return (v, r.get("votes")) if v is not None else None


def own_rating(entry):
    """(value, votes) of the catalogue's own rating -- the first one it reports
    that is not IMDb's -- else IMDb's, else None.

    This is what `vote`/`votes` always meant to the clients: the catalogue's
    number, shown on the detail pages and used by rank() when a title has no
    IMDb rating. The contract files it under the catalogue's own name.
    """
    names = [n for n in ((entry or {}).get("ratings") or {}) if n != "imdb"]
    for name in names + ["imdb"]:
        r = rating_of(entry, name)
        if r:
            return r
    return None


def passes_rating_floor(entry):
    """Whether a title clears the quality floor for the balanced/recent sorts.

    A title with NO imdb rating passes. That is deliberate and it is a change:
    the old code dropped anything the dataset did not know about, which with a
    provider that reports no ratings at all would empty the grid completely.
    A missing optional rating must never be the reason a film cannot be played.
    """
    ir = rating_of(entry, "imdb")
    if not ir:
        return True
    value, votes = ir
    if value < config.MIN_RATING:
        return False
    return not (votes is not None and votes < config.MIN_IMDB_VOTES)


# ffprobe writes ISO 639-2 ("eng", and both the bibliographic and terminological
# forms exist for some languages), the listing layer and PREF_LANG speak 639-1.
LANG_TAGS = {
    "en": ("en", "eng"), "es": ("es", "spa"), "ru": ("ru", "rus"),
    "fr": ("fr", "fre", "fra"), "de": ("de", "ger", "deu"), "it": ("it", "ita"),
    "ja": ("ja", "jpn"), "ko": ("ko", "kor"), "pt": ("pt", "por"),
    "hi": ("hi", "hin"), "zh": ("zh", "chi", "zho"), "sv": ("sv", "swe"),
    "nl": ("nl", "dut", "nld"), "pl": ("pl", "pol"), "tr": ("tr", "tur"),
    "uk": ("uk", "ukr"),
}
LANG_NAME = {
    "en": "English", "es": "Spanish", "ru": "Russian", "fr": "French",
    "de": "German", "it": "Italian", "ja": "Japanese", "ko": "Korean",
    "pt": "Portuguese", "hi": "Hindi", "zh": "Chinese", "sv": "Swedish",
    "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "uk": "Ukrainian",
}

def audio_has_lang(tags, want):
    """Does this file carry an audio track in `want` (a 639-1 code)?

    "und" is an untagged track, which is no evidence either way -- plenty of
    English rips tag nothing at all -- so a file whose tracks are all "und"
    passes. Only a file that names its languages and does not name this one
    fails. "en-US"-style tags are matched on their first subtag."""
    known = [t for t in tags if t and t != "und"]
    if not known:
        return True
    alts = set(LANG_TAGS.get(want, (want,)))
    return any(t.split("-")[0] in alts for t in known)

def audio_track_for(tags, want):
    """Index of the first audio track in `want`, else 0 -- the track the conversion should keep."""
    alts = set(LANG_TAGS.get(want, (want,)))
    for i, t in enumerate(tags):
        if t and t != "und" and t.split("-")[0] in alts:
            return i
    return 0

def score(c, runtime_min=None, relax=False, kind="movie"):
    """Higher is better. Encodes everything the TV and the link can actually do.
    relax=True builds the FALLBACK tail: 1080p allowed, looser peer floor.
    Better to drop to 1080p than to run out of candidates and play nothing.
    kind="tv" relaxes rules that only make sense for a single film: packs are
    the norm for a series and share a swarm across episodes, 1080p episodes
    run ~1GB, and 4K series releases are rare enough not to demand them."""
    # Unknown is not zero. A torrent index reports peers and sizes; a direct
    # HTTP source usually reports neither, and scoring "unknown" as 0 meant a
    # sub-1GB penalty, a failed peer floor and a budget check against a
    # zero-peer swarm -- three separate reasons to reject every single
    # candidate an HTTP-only provider could ever return.
    seeders = c.get("seeders")
    gb = c.get("gb")
    transport = c.get("transport") or ("torrent" if c.get("infoHash") else None)

    # HEVC_ONLY, FOURK_ONLY and the small-file penalty are PREFERENCES, not
    # capabilities. They are on by default because this panel is 4K and a big
    # torrent index offers hundreds of releases of the same film, so there is
    # always another candidate to move to.
    #
    # A modest provider has no such abundance -- a public-domain index carries
    # one 1080p H.264 print of a 1921 film and nothing else -- and enforcing a
    # preference absolutely there rejects the entire catalogue. That is what
    # `relax` already exists for: best_stream() runs a strict pass, then a
    # relaxed pass to build the fallback tail. FOURK_ONLY honoured `relax`;
    # HEVC_ONLY and the sub-1GB penalty did not, so no amount of relaxing could
    # rescue a non-HEVC film and a provider-independent Cinematica would show
    # an empty grid. All three now relax together.
    #
    # AV1 is not in that set: that is a decoder the panel lacks, and no
    # preference relaxes a file that cannot be played.
    if c["codec"] == "AV1":          return -1
    if config.HEVC_ONLY and not relax and c["codec"] != "HEVC": return -1
    # Burnt into the picture, so no player setting can escape them.
    if config.REJECT_HARDSUB and c.get("hardsub"): return -1
    # A listing that names its languages and does not name ours is a foreign
    # dub. A listing that names none is NOT rejected -- most English releases
    # carry no flag at all, and probe_media() checks the file itself later.
    langs = c.get("langs") or []
    if config.REJECT_LANG and langs and config.PREF_LANG not in langs and "multi" not in langs:
        return -1
    if seeders is not None and seeders < (config.MIN_SEEDERS // 2 if relax else config.MIN_SEEDERS):
        return -1
    if transport is None:            return -1       # nothing playable to point at
    if config.FOURK_ONLY and not relax and not c["is4k"] and kind != "tv":
        return -1  # 4K or nothing
    if gb is not None and gb > netprofile.max_gb():
        if kind == "tv" and c["pack"]:
            # Some indexes report the whole-torrent size for packs; treat it as
            # unknown here -- the probe corrects it later.
            gb = None
        else:
            return -1
    # can the link actually keep up with this file's bitrate?
    req = netprofile.required_mbps(gb, runtime_min)
    if req is not None:
        c["req_mbps"] = round(req, 1)
        c["thin"] = req < (config.LOW_MBPS_4K if c["is4k"] else config.LOW_MBPS_HD)
        budget = netprofile.sustainable_mbps(seeders)
        c["budget_mbps"] = round(budget, 1)
        if req > budget:
            return -1
    s = 0.0
    c["audio"] = mediaprobe.audio_kind(c)
    if c["audio"] == "ac3":
        s_audio = 260          # decodes cleanly on this panel
    elif c["audio"] == "aac":
        s_audio = -240         # drifts and breaks up; avoid unless nothing else
    else:
        s_audio = 0
    s += s_audio
    s += 400 if c["is4k"] else 100
    s += 120 if c["codec"] == "HEVC" else (60 if c["codec"] == "H264" else 0)
    if kind == "tv":
        s += 20 if c["pack"] else 0          # packs share a swarm across episodes
    else:
        s -= 150 if c["pack"] else 0         # packs rank high on seeders but often stall
    s += min(seeders, 400) * 0.4 if seeders is not None else 0
    # Only a claimed size can be suspiciously small, and only when a big
    # release was the expectation. An unstated size says nothing, and in the
    # relaxed pass a small file is often the only print that exists.
    if kind != "tv" and not relax and gb is not None and gb < 1.0: s -= 100
    return s

def best_stream(identity, runtime_min=None, kind="movie", season=None, episode=None):
    """Ask the streams provider for this title's candidates, then run the same
    strict/relaxed split as before: a strict pass demands the panel's
    preferences (4K, HEVC, native audio...), a relaxed pass builds the
    fallback tail so a run of candidates that fail strict scoring degrades to
    a watchable stream instead of nothing playing at all.

    Per-provider throttling (a stream index that rate-limits can answer a
    429, and that used to be cached as "no usable stream" for three hours --
    silently deleting films from the catalogue) now lives in
    gateway._invoke(), which applies it to every provider automatically
    instead of relying on one hand-wired call site to remember to ask for it.

    Raises contract.ProviderError on failure -- callers (get_stream(),
    get_stream_tv()) already catch it and know how to turn a real "not
    found"/"unsupported" answer into a cached soft error versus a transport
    failure into a short retry.
    """
    res = gateway.streams(identity, season, episode)
    cands = res.get("candidates") or []
    rejected = dict(res.get("rejected") or {})
    for c in cands:
        c["score"] = score(c, runtime_min, kind=kind)
    strict = sorted([c for c in cands if c["score"] > 0], key=lambda c: -c["score"])
    # Fallback tail: same title at 1080p / lower peer floor, so a run of dead
    # strict candidates degrades to a watchable stream instead of failing
    # outright.
    seen = {c["key"] for c in strict}
    tail = []
    for c in cands:
        if c["key"] in seen:
            continue
        sc = score(c, runtime_min, relax=True, kind=kind)
        if sc > 0:
            c = dict(c); c["score"] = sc; c["fallback"] = True
            tail.append(c)
    tail.sort(key=lambda c: -c["score"])
    return strict + tail, len(cands), rejected

# A provider may hand back a plain HTTP URL that needs request headers -- an
# Authorization, a Referer, a signed cookie. Those are credentials, and four
# separate consumers would otherwise need to carry them: ffprobe, the
# transcoder, the TV's player, and the Sendspin bridge's decoder.
#
# None of them do. Core registers the source here and publishes it as a local
# /src/<key> URL; the proxy below is the only place the real URL and its
# headers exist. Every consumer keeps working on a plain, credential-free URL
# exactly as it did when every source was a torrent, which is why none of the
# playback or audio code had to change to support this.
_sources = {}        # key -> {"url":..., "headers":{...}, "at": ts}
TTL_SOURCE = 12 * 3600

def register_source(c):
    """Remember a direct source's real URL and headers; return its local key."""
    if not c or c.get("transport") != "http" or not c.get("url"):
        return None
    key = c.get("key") or contract.source_key("http", url=c["url"])
    with core._lock:
        _sources[key] = {"url": c["url"], "headers": dict(c.get("headers") or {}),
                         "at": time.time()}
        core._evict(_sources, TTL_SOURCE, config.MAX_STREAM_ENTRIES)
    return key

def source_for(key):
    with core._lock:
        e = _sources.get(key)
        if e:
            # Touch it: a film that is still playing must not have its own
            # source evicted out from under it by an hour of browsing.
            e["at"] = time.time()
        return e

def stream_url(c):
    """The URL the rest of the system plays. Never carries a credential."""
    if c.get("transport") == "http":
        key = register_source(c)
        return "http://%s:%d/src/%s" % (config.PUBLIC_HOST, config.PORT, key)
    idx = c.get("fileIdx")
    # fileIdx is genuinely absent on some streams; omit it and let the server pick
    return f"{config.STREMIO}/{c['infoHash']}" + (f"/{idx}" if idx is not None else "")

def stream_url_internal(c):
    """Same source, reached from inside this box -- what ffprobe and the
    transcoder open. The proxy is local either way, so an HTTP source is the
    same URL; only the Stremio path differs between internal and public."""
    if c.get("transport") == "http":
        return "http://127.0.0.1:%d/src/%s" % (config.PORT, register_source(c))
    idx = c.get("fileIdx")
    return f"{config.STREMIO_IN}/{c['infoHash']}" + (f"/{idx}" if idx is not None else "")

def stream_url_public(c):
    """The same source as a RELATIVE url, for a browser.

    stream_url() bakes in PUBLIC_HOST and, for a torrent, the streaming server's
    own port -- both correct for the TV, which is on the LAN, and both useless to
    a browser that reached this page over a tailnet address. A relative url rides
    whatever origin the page was actually opened on, and carries no credential.
    """
    if c.get("transport") == "http":
        return "/src/%s" % register_source(c)
    idx = c.get("fileIdx")
    return "/t/%s" % c["infoHash"] + (("/%d" % idx) if idx is not None else "")
# Every key below carries the producing role's gateway.cache_tag() ("<provider
# id>@<config rev>") as well as the id it caches -- a provider swap or a
# config edit (new API key, different index) must never keep serving results
# gathered under the old one, so the tag is part of the key, not a value
# checked after the fact.
_streams= {}        # "<tag>@<id>" (or "<tag>@tv:{id}:{s}:{e}") -> {"at":ts,"pick":c|None,"count":n,"err":str|None}

# "no usable stream" is a real answer about a film and keeps the full TTL. A
# transport failure (429, a Cloudflare 403, a timeout) is not an answer at
# all, and caching it for TTL_STREAM drops the film out of the grid for three
# hours -- resolve_chunk() silently skips anything without a pick. That is
# what TTL_FAIL is for. get_stream()/get_stream_tv() decide which bucket a
# contract.ProviderError falls into by its .code: contract.CACHEABLE_ERRORS
# ("not found"/"unsupported") is a real answer and gets the soft string
# below; anything else is a transport failure and gets a harder one instead.
# The bucket is now carried on the entry as "soft" rather than inferred by
# string-matching the message. Those were two different questions wearing one
# answer: how long to cache, and what to tell the user. Matching on the text
# meant that reporting a provider's real reason -- which is the only useful
# thing to show -- silently reclassified a real answer as a transport failure.
SOFT_ERRS = ("no usable stream",)

def entry_ttl(e):
    e = e or {}
    err = e.get("err")
    if not err:
        return config.TTL_STREAM
    if "soft" in e:
        return config.TTL_STREAM if e["soft"] else config.TTL_FAIL
    return config.TTL_FAIL if err not in SOFT_ERRS else config.TTL_STREAM

def stream_miss(count, rejected):
    """Why nothing was playable, when the provider did answer.

    "no usable stream" on its own is true and useless. If forty sources came
    back and every one was an unsupported transport, or every one scored below
    the quality floor, that is what the admin needs to read.
    """
    if not count:
        return "no usable stream"
    if rejected:
        why = ", ".join("%s x%d" % (r, n) for r, n in
                        sorted(rejected.items(), key=lambda kv: -kv[1])[:3])
        return "no usable stream (%d returned; %s)" % (count, why)
    return "no usable stream (%d returned, none met the quality rules)" % count

def _cached_stream(key, force):
    """The cached entry under `key` if it is still fresh, else None."""
    with core._lock:
        e = _streams.get(key)
        if e and not force and time.time() - e["at"] < entry_ttl(e):
            return e
    return None

def _store_stream(key, e):
    with core._lock:
        _streams[key] = e
        core._evict(_streams, config.TTL_STREAM, config.MAX_STREAM_ENTRIES)
    return e

def _stream_entry(identity, runtime, title, ratings, extra=None, **best_kw):
    """Rank the sources for one film or episode into the cache's entry shape.

    No short-circuit on a missing imdb id: the streams provider may accept
    the catalogue's own id just fine, and a title that happens to have no
    IMDb id must still get a real attempt, not an automatic "no imdb_id"."""
    ranked, n, rejected = best_stream(identity, runtime, **best_kw)
    e = {"at": time.time(), "pick": (ranked[0] if ranked else None),
         # picks is what the TV plays, ranked by score() and cut to ATTEMPTS.
         # picks_all keeps the relaxed tail too, because a browser may need an
         # H.264 candidate that the TV's HEVC-first ranking pushed past the cut.
         "picks": ranked[:config.ATTEMPTS], "picks_all": ranked[:25], "count": n,
         "rejected": rejected,
         "imdb_id": (identity.get("external_ids") or {}).get("imdb"),
         "runtime": runtime, "title": title,
         # A list entry has no IMDb id, so no IMDb rating either: the
         # wall takes it from the details fetched here (_tile()).
         "ratings": ratings or {},
         "soft": True,
         "err": None if ranked else stream_miss(n, rejected)}
    e.update(extra or {})
    return e

def _stream_failure(ex, extra=None):
    """The entry for a lookup that raised. A provider's error says what
    actually happened, cacheable or not: hiding its reason behind "no usable
    stream" is how a whole catalogue came to look unplayable with nothing
    anywhere explaining it. Anything else carries no "soft", so entry_ttl()
    retries it soon."""
    e = {"at": time.time(), "pick": None, "count": 0, "rejected": {}, "imdb_id": None}
    if isinstance(ex, contract.ProviderError):
        e.update(soft=ex.code in contract.CACHEABLE_ERRORS,
                 err="%s: %s" % (ex.code, ex.message))
    else:
        e["err"] = f"{type(ex).__name__}: {ex}"
    e.update(extra or {})
    return e

def get_stream(mid, force=False, entry=None):
    """`entry`, when the caller already holds this title's catalogue entry (a
    browse/search result), is used to build the identity directly instead of
    a details() round trip -- this used to fetch the film's details fresh on
    EVERY stream lookup, even for a film the pool had just resolved from its
    own listing. When the caller has nothing cheaper, a details() lookup
    still happens below; there is no cache for it here because there never
    was one."""
    key = "%s@%s" % (gateway.cache_tag(contract.ROLE_STREAMS), mid)
    cached = _cached_stream(key, force)
    if cached:
        return cached
    try:
        det = entry
        # A list entry stands in for the details only when it carries what the
        # lookup needs: the IMDb id (the key a stream index is most likely to
        # accept, and for some the only one) and the runtime (the bitrate
        # check's divisor). A catalogue's browse and search results commonly
        # carry neither, and trusting them sent every film to the streams
        # provider id-less -- an empty wall, a Play that failed, and a
        # calibration with nothing to measure. Before providers were split out,
        # every film's details were fetched here.
        if det is None or not (det.get("external_ids") or {}).get("imdb") \
                or not det.get("runtime"):
            det = gateway.details(mid, contract.KIND_MOVIE)
        identity = {"id": mid, "local_id": det.get("local_id"), "kind": contract.KIND_MOVIE,
                    "title": det.get("title"), "year": det.get("year"),
                    "runtime": det.get("runtime"), "external_ids": det.get("external_ids") or {}}
        e = _stream_entry(identity, det.get("runtime"), det.get("title"), det.get("ratings"))
    except Exception as ex:
        e = _stream_failure(ex)
    return _store_stream(key, e)

def get_stream_tv(tid, s, e, force=False, entry=None):
    """get_stream() for one episode. Shares the SAME _streams dict as films --
    same TTLs, entry_ttl(), _evict(), MAX_STREAM_ENTRIES -- just keyed by
    season+episode so a show's other episodes don't collide with each other
    or with a film of the same id. `entry`, like get_stream()'s, lets a
    caller that already holds the series' catalogue entry skip tv_detail();
    when it does not, tv_detail() is already a 24h cache, not a fresh call."""
    key = "%s@%s" % (gateway.cache_tag(contract.ROLE_STREAMS), "tv:%s:%s:%s" % (tid, s, e))
    cached = _cached_stream(key, force)
    if cached:
        return cached
    where = {"kind": "tv", "season": s, "episode": e}
    try:
        det = entry if entry is not None else catalogue.tv_detail(tid)
        ep = next((x for x in catalogue.tv_season(tid, s)["episodes"] if x.get("episode") == e), None)
        runtime = (ep.get("runtime") if ep else None) or det.get("runtime") or 45
        ep_name = (ep.get("name") if ep else None) or ""
        title = f"{det.get('title')} · S{s:02d}E{e:02d}" + (f" · {ep_name}" if ep_name else "")
        identity = {"id": tid, "local_id": det.get("local_id"), "kind": contract.KIND_SERIES,
                    "title": det.get("title"), "year": det.get("year"), "runtime": runtime,
                    "external_ids": det.get("external_ids") or {}}
        ent = _stream_entry(identity, runtime, title, det.get("ratings"), where,
                            kind="tv", season=s, episode=e)
    except Exception as ex:
        ent = _stream_failure(ex, where)
    return _store_stream(key, ent)
