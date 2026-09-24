"""The provider wire contract, version 1.

One module, no dependencies beyond the stdlib, imported by BOTH sides: the
Cinematica server and the provider process. It holds three things.

  * The protocol constants -- version, operation names, error codes.
  * Normalisers. A provider returns plausible JSON; core needs the exact
    shapes get_page()/best_stream() already work with. Every value that
    crosses the boundary goes through normalise_entry() /
    normalise_candidate(), which coerce types, drop unknown keys, and raise
    ContractError on anything that cannot be made sense of. A provider bug
    must surface as a named error, never as a TypeError six frames deep in
    the scoring code.
  * redact(). Secrets reach providers, so secrets can reach tracebacks,
    logs and error strings on the way back. Everything user-visible is
    filtered through this.

Design note on identifiers. A provider's ids are ITS OWN -- a string, opaque
to core, never assumed numeric. Core qualifies them as "<provider_id>:<id>"
(see qualify/unqualify) so a catalogue swap cannot make the TV's saved
"movie 603" mean a different film. The only ids with agreed meaning across
providers are the external ones (IMDb, and any other public id a provider
happens to know), which is what stream lookup matches on.
"""

import hashlib
import json
import re
import urllib.parse

CONTRACT_VERSION = 1

# ---- roles ------------------------------------------------------------------
# Three, not two. Splitting metadata out of catalogue is what lets one add-on
# supply the lists and another supply the artwork and episodes -- Stremio
# add-ons routinely implement "catalog" without "meta" and vice versa, and
# collapsing them would have forced a single provider to do both.
ROLE_CATALOGUE = "catalogue"   # browse lists, search -> previews
ROLE_METADATA  = "metadata"    # details, artwork, episodes, ratings
ROLE_STREAMS   = "streams"     # playable sources
ROLE_CHANNELS  = "channels"    # followed channels, their uploads, handing a video to an external player
ROLES = (ROLE_CATALOGUE, ROLE_METADATA, ROLE_STREAMS, ROLE_CHANNELS)
# Setup only ever needs these three -- channels is optional and never blocks
# "configured" (see registry.setup_state).
CORE_ROLES = (ROLE_CATALOGUE, ROLE_METADATA, ROLE_STREAMS)
CAPABILITIES = ROLES           # a provider's manifest declares the roles it fills

# ---- operations -------------------------------------------------------------
# Every provider must answer these two whatever it declares.
OP_DESCRIBE  = "provider.describe"
OP_TEST      = "config.test"
# catalogue role
OP_GENRES    = "catalogue.genres"
OP_BROWSE    = "catalogue.browse"
OP_SEARCH    = "catalogue.search"
# metadata role
OP_DETAILS   = "metadata.details"
OP_EPISODES  = "metadata.episodes"
OP_RATINGS   = "metadata.ratings"     # optional within the role
# streams role
OP_STREAMS   = "streams.lookup"
# channels role -- resolve/details/latest/play are the core the role obliges;
# videos/search/popular are optional and advertised per install (see
# gateway.channel_ops), because a channels provider may have no search index
# or no separate "popular" concept at all.
OP_CH_RESOLVE = "channels.resolve"
OP_CH_DETAILS = "channels.details"
OP_CH_LATEST  = "channels.latest"
OP_CH_VIDEOS  = "channels.videos"
OP_CH_SEARCH  = "channels.search"
OP_CH_POPULAR = "channels.popular"
OP_CH_PLAY    = "channels.play"

ROLE_OPS = {
    ROLE_CATALOGUE: (OP_BROWSE, OP_SEARCH, OP_GENRES),
    ROLE_METADATA:  (OP_DETAILS, OP_EPISODES),
    ROLE_STREAMS:   (OP_STREAMS,),
    ROLE_CHANNELS:  (OP_CH_RESOLVE, OP_CH_DETAILS, OP_CH_LATEST, OP_CH_PLAY),
}
CAP_OPS = ROLE_OPS
OPTIONAL_OPS = (OP_RATINGS, OP_GENRES, OP_CH_VIDEOS, OP_CH_SEARCH, OP_CH_POPULAR)

# Which normalised browse filters a provider can actually apply. Core asks, via
# provider.describe; a filter the provider does not support must be declared,
# not silently ignored. build_pool() drops the bias tiers that exist ONLY to
# express a filter nobody can apply -- if it did not, every tier would issue the
# same unfiltered query and the per-block dedup would collapse them into one,
# quietly disabling the bias with no error anywhere.
FILTERS = ("genre_ids", "exclude_genre_ids", "min_votes",
           "original_language", "origin_countries", "released_after")

# ---- error codes ------------------------------------------------------------
# Stable strings: the web UI maps them to a state badge, so adding one is a
# contract change and renaming one breaks the UI.
E_CONFIG     = "config"        # missing/invalid configuration -- "Needs configuration"
E_AUTH       = "auth"          # credentials rejected by the service
E_RATE       = "rate_limit"    # back off and retry; NOT a cacheable answer
E_UPSTREAM   = "upstream"      # the service failed or was unreachable
E_NOTFOUND   = "not_found"     # the id genuinely does not exist there
E_UNSUPPORTED= "unsupported"   # provider does not implement this op
E_TIMEOUT    = "timeout"       # raised by the runner, not the provider
E_CRASH      = "crash"         # provider process died or spoke nonsense
E_PROTOCOL   = "protocol"      # well-formed process, malformed reply
E_INTERNAL   = "internal"      # provider raised something it did not expect

# Errors that are a real answer about a title and may be cached for the full
# TTL. Everything else is a transport failure: cache briefly and retry, or the
# film silently vanishes from the grid for hours. (Core learned this the hard
# way with stream-index 429s -- see SOFT_ERRS in server.py.)
CACHEABLE_ERRORS = (E_NOTFOUND, E_UNSUPPORTED)


class ContractError(Exception):
    """A provider's reply could not be made to fit the contract."""

    def __init__(self, message, code=E_PROTOCOL):
        super().__init__(message)
        self.code = code
        self.message = message


class ProviderError(Exception):
    """A provider answered, and the answer was an error."""

    def __init__(self, code, message, retryable=None, provider=None, op=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.provider = provider
        self.op = op
        self.retryable = (code not in CACHEABLE_ERRORS) if retryable is None else retryable

    def as_dict(self):
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "provider": self.provider, "op": self.op}


# ---- configuration field types ----------------------------------------------
# What a manifest may declare, and therefore what the settings page knows how
# to render. Deliberately short: adding a type means adding a widget, and the
# whole point is that installing a provider never touches the web UI.
F_TEXT   = "text"
F_URL    = "url"
F_SECRET = "secret"      # never leaves the server; masked in every response
F_BOOL   = "bool"
F_CHOICE = "choice"
F_NUMBER = "number"
FIELD_TYPES = (F_TEXT, F_URL, F_SECRET, F_BOOL, F_CHOICE, F_NUMBER)

SECRET_TYPES = (F_SECRET,)

RE_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
RE_VERSION     = re.compile(r"^\d+(\.\d+){0,3}(-[0-9A-Za-z.-]+)?$")
RE_IMDB        = re.compile(r"^tt\d{6,}$")
RE_HASH40      = re.compile(r"^[0-9a-fA-F]{40}$")

# Transports the playback engine can actually play.
#
#   torrent  infoHash (+ optional fileIdx), streamed through the Stremio server
#            exactly as before -- this path is untouched.
#   http     a direct URL, optionally needing request headers. Core NEVER hands
#            those headers onwards; it publishes the source as a local /src/
#            URL and injects them itself (see source_key). That is what keeps
#            ffprobe, the transcoder, the TV player and the Sendspin bridge
#            working on a plain URL, with no change to any of them.
#
# Anything else -- ytId, externalUrl, a DRM manifest -- is refused by name and
# counted, so "47 streams, 0 playable" can say WHY. Advertising a transport
# that does not work is worse than refusing it.
T_TORRENT = "torrent"
T_HTTP    = "http"
TRANSPORTS = (T_TORRENT, T_HTTP)
SUPPORTED_SOURCE_TYPES = TRANSPORTS


# ---- identifiers ------------------------------------------------------------
def qualify(provider_id, local_id):
    """"acme-catalogue:603". Provider-qualified so switching catalogues cannot
    resurrect a saved selection that now points at a different film."""
    return "%s:%s" % (provider_id, local_id)


def unqualify(qualified):
    """(provider_id, local_id). Splits on the FIRST colon only -- a provider's
    own ids may contain colons (an episode key, a slug with a namespace)."""
    s = str(qualified or "")
    if ":" not in s:
        return None, s
    pid, _, local = s.partition(":")
    return pid, local


def config_revision(provider_id, version, config, addon_url=""):
    """A short digest of everything that could change what a provider returns.

    Cache keys carry it, so re-pointing a provider at a different add-on
    configuration (or a different account) cannot serve results gathered under
    the old one. Secrets are hashed, not stored: the digest goes in cache keys and
    in /api/providers responses, and must be safe in both.

    `addon_url` is part of the identity because for a Stremio add-on it IS the
    configuration. The settings live in a path segment of the manifest URL --
    ".../apikey=KEY|sort=quality/manifest.json" -- so re-pointing a
    provider at a different add-on config, or at a different account entirely,
    changes nothing else this digest can see: the provider id is slugified
    from the add-on's own id, the manifest version does not move, and
    `config` holds the declared fields, which for an add-on are usually none.
    Leave it out and every cached page gathered under the old URL keeps being
    served under the new one, and gateway's instance cache (keyed on this same
    revision) hands back the adapter still pointed at the old URL.
    """
    blob = json.dumps({"p": provider_id, "v": version,
                       "c": {k: config[k] for k in sorted(config or {})},
                       "u": addon_url or ""},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ---- redaction --------------------------------------------------------------
# Deliberately broad. A provider is free-form Python written by someone else;
# its exception messages will contain whatever it felt like interpolating, and
# those strings reach the web UI, the journal and /api/providers.
_RE_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")
_RE_QS_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|apikey|key|token|access[_-]?token|auth|password|passwd|pwd|secret|sig|signature)=)([^&\s\"']+)")
_RE_BEARER = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/-]{8,}=*)")
_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")


def redact(text, extra_secrets=()):
    """Strip credentials out of a string bound for a log, an error or the UI.

    Two passes. First the literal secret values this install actually holds --
    the only way to catch an API key a provider pasted into a message in a
    shape no pattern anticipates. Then the generic shapes: userinfo in URLs,
    well-known query parameters, Bearer headers, bare JWTs.
    """
    s = "" if text is None else str(text)
    for secret in extra_secrets or ():
        if secret and isinstance(secret, str) and len(secret) >= 6:
            s = s.replace(secret, "[redacted]")
    s = _RE_URL_CRED.sub(r"\1\2:[redacted]@", s)
    s = _RE_QS_SECRET.sub(r"\1[redacted]", s)
    s = _RE_BEARER.sub(r"\1[redacted]", s)
    s = _RE_JWT.sub("[redacted]", s)
    return s


def url_secrets(url):
    """The credential-bearing substrings of a configured URL, for redact()'s
    literal pass.

    A Stremio add-on is configured by its URL: the account token, the debrid
    API key and the sort preferences all live in a path segment of the
    manifest URL, e.g.
    ".../apikey=API_KEY|sort=quality/manifest.json". redact()'s
    pattern pass does not see those -- they are not userinfo, not a query
    parameter and not a Bearer header, just an ordinary-looking path segment
    -- so the literal pass has to be told about them by name.

    Returns the configuration-bearing pieces, not the whole URL: replacing
    the pieces turns a message quoting the full URL into exactly what
    mask_url() would have produced -- the host still named, the credential
    gone -- whereas registering the whole URL would blank the host too and
    leave an admin reading "add-on returned HTTP 500 for [redacted]".

    Nothing shorter than 8 characters is included. A literal that short is as
    likely to be an innocent word elsewhere in the message ("stream",
    "config") as it is to be a secret, and redact() replaces every occurrence
    it finds, wherever it finds it.
    """
    u = (url or "").strip()
    if not u:
        return []
    out = []
    try:
        parts = urllib.parse.urlsplit(u)
    except ValueError:
        return []
    if parts.username:
        out.append(parts.username)
    if parts.password:
        out.append(parts.password)
    if parts.query:
        out.append(parts.query)
    for seg in (parts.path or "").split("/"):
        # The trailing filename is structural, never configuration; every
        # other segment of an add-on URL is where the configuration goes.
        if seg and seg.lower() not in ("manifest.json",):
            out.append(seg)
    seen, keep = set(), []
    for v in out:
        if len(v) >= 8 and v not in seen:
            seen.add(v)
            keep.append(v)
    return keep


def mask_url(url):
    """What a configured URL looks like in an API response: which add-on it
    is, never what it was configured with.

    The host stays -- an admin has to be able to tell one add-on from another
    on the settings page, and a hostname is not a credential. Everything that
    can carry one goes: userinfo, the query string, and every path segment but
    a trailing manifest.json, which is the segment Stremio add-ons keep their
    configuration (and, with a debrid service, an account API key) in.

    A public add-on configured by nothing therefore still reads in full --
    "https://v3-cinemeta.strem.io/manifest.json" comes back exactly as typed
    -- while a configured one comes back as its shape.
    """
    u = (url or "").strip()
    if not u:
        return ""
    if u == "package":          # the literal registry.py uses for a non-addon
        return u
    try:
        parts = urllib.parse.urlsplit(u)
    except ValueError:
        return "[redacted]"
    if not parts.scheme or not parts.hostname:
        return "[redacted]"
    netloc = parts.hostname + (":%d" % parts.port if parts.port else "")
    if parts.username or parts.password:
        netloc = "[redacted]@" + netloc
    segs = [("" if not seg else
             seg if seg.lower() in ("manifest.json",) else "[redacted]")
            for seg in (parts.path or "").split("/")]
    path = "/".join(segs)
    return "%s://%s%s%s" % (parts.scheme, netloc, path,
                            "?[redacted]" if parts.query else "")


def mask(value):
    """What a secret looks like in an API response: whether it is set, and a
    hint at which one it is -- never the value. Leaving this field alone on a
    save must not erase it, so the UI needs to distinguish set from empty."""
    if not value:
        return {"set": False, "hint": ""}
    v = str(value)
    hint = ("•" * 4) + v[-4:] if len(v) > 8 else "•" * len(v)
    return {"set": True, "hint": hint}


# ---- small coercions --------------------------------------------------------
def _s(v, default=""):
    if v is None:
        return default
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return str(v)
    return default


def _i(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _f(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _b(v):
    return bool(v) if not isinstance(v, str) else v.strip().lower() in ("1", "true", "yes", "on")


def _url(v):
    """A complete http(s) URL or nothing.

    Artwork is the reason this is strict. A provider returns whole URLs -- core
    never pastes a path fragment onto a base it guessed, which is exactly the
    single-service assumption the TV app used to bake in. A bare "/abc.jpg" is dropped
    rather than turned into a broken image on the grid.
    """
    s = _s(v).strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return s
    if low.startswith("data:image/"):
        return s
    return None


def _year(v):
    """A 4-digit year from a year, a date, or a datetime."""
    s = _s(v).strip()
    m = re.match(r"^(\d{4})", s)
    if m:
        y = int(m.group(1))
        return str(y) if 1870 <= y <= 2200 else ""
    return ""


def _date(v):
    """ISO yyyy-mm-dd, or "" -- core compares these as strings (tv_season drops
    unaired episodes with `air > today`), so a half-parsed date is worse than
    none."""
    s = _s(v).strip()
    return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else ""


def _strlist(v, cap=32):
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v[:cap]:
        s = _s(x).strip()
        if s and s not in out:
            out.append(s)
    return out


# ---- normalised catalogue entry ---------------------------------------------
KIND_MOVIE  = "movie"
KIND_SERIES = "series"
KINDS = (KIND_MOVIE, KIND_SERIES)


def _kind(v):
    s = _s(v).strip().lower()
    if s in ("movie", "film"):
        return KIND_MOVIE
    if s in ("series", "tv", "show", "tvshow"):
        return KIND_SERIES
    return None


def normalise_external_ids(v):
    """{"imdb": "tt0111161", "catalogue": "278", ...} -- values kept as strings.

    IMDb ids are validated because stream lookup interpolates them into a URL
    path; a junk value there is a request to somewhere unintended. Everything
    else is passed through as an opaque string for whichever provider knows
    what to do with it.
    """
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, val in list(v.items())[:16]:
        key = _s(k).strip().lower()
        sval = _s(val).strip()
        if not key or not sval or len(sval) > 128:
            continue
        if key == "imdb" and not RE_IMDB.match(sval):
            continue
        out[key] = sval
    return out


def normalise_ratings(v):
    """{"imdb": {"value": 8.7, "votes": 2800000}, "critics": {...}}.

    Named, because "the rating" means nothing once the catalogue is pluggable:
    core's MIN_RATING floor is an IMDb floor and must not be silently applied
    to some other provider's 0-100 scale. A rating whose name core does not
    recognise is carried through and displayed, not scored on.
    """
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, val in list(v.items())[:8]:
        name = _s(k).strip().lower()
        if not name:
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            value, votes = _f(val), None
        elif isinstance(val, dict):
            value, votes = _f(val.get("value")), _i(val.get("votes"))
        else:
            continue
        if value is None:
            continue
        out[name] = {"value": round(value, 3), "votes": votes if (votes or 0) >= 0 else None}
    return out


def normalise_season(v):
    if not isinstance(v, dict):
        return None
    n = _i(v.get("n") if v.get("n") is not None else v.get("season"))
    if n is None or n < 0:
        return None
    return {"n": n,
            "name": _s(v.get("name"))[:200] or ("Season %d" % n),
            "episodes": _i(v.get("episodes")),
            "air": _date(v.get("air") or v.get("air_date")),
            "poster": _url(v.get("poster"))}


def normalise_episode(v, season_hint=None):
    """One episode. season/episode numbers are mandatory -- they are what
    stream lookup keys on, and an episode without them is unplayable."""
    if not isinstance(v, dict):
        return None
    s = _i(v.get("season"))
    e = _i(v.get("episode"))
    if s is None:
        s = _i(season_hint)
    if s is None or e is None:
        return None
    return {"season": s, "episode": e,
            "name": _s(v.get("name"))[:400],
            "overview": _s(v.get("overview"))[:4000],
            "runtime": _i(v.get("runtime")),
            "air": _date(v.get("air") or v.get("air_date")),
            "still": _url(v.get("still")),
            "ratings": normalise_ratings(v.get("ratings"))}


def normalise_entry(v, provider_id, strict_kind=None):
    """A provider's title -> the normalised catalogue entry core works with.

    Raises ContractError when there is nothing usable: no id, or no kind. Every
    other field degrades to empty rather than failing, because a provider that
    omits a backdrop should cost a backdrop, not a page of results.
    """
    if not isinstance(v, dict):
        raise ContractError("catalogue entry is %s, expected an object" % type(v).__name__)
    local_id = _s(v.get("id")).strip()
    if not local_id or len(local_id) > 256:
        raise ContractError("catalogue entry has no usable id")
    kind = _kind(v.get("kind")) or _kind(strict_kind)
    if kind is None:
        raise ContractError("catalogue entry %r has no kind (movie/series)" % local_id)

    air = _date(v.get("first_air") or v.get("first_air_date"))
    rel = _date(v.get("release_date") or v.get("release"))
    year = _year(v.get("year") or rel or air)

    out = {
        "id": qualify(provider_id, local_id),
        "provider": provider_id,
        "local_id": local_id,
        "kind": kind,
        "title": _s(v.get("title") or v.get("name"))[:400] or "?",
        "overview": _s(v.get("overview") or v.get("description"))[:8000],
        "tagline": _s(v.get("tagline"))[:400],
        "year": year,
        "release_date": rel,
        "first_air": air,
        "last_air": _date(v.get("last_air") or v.get("last_air_date")),
        "status": _s(v.get("status"))[:64],
        "runtime": _i(v.get("runtime")),
        "poster": _url(v.get("poster")),
        "backdrop": _url(v.get("backdrop")),
        "genres": _strlist(v.get("genres")),
        "genre_ids": [_s(g) for g in _strlist(v.get("genre_ids"), 64)],
        "origin_countries": [c.upper()[:2] for c in _strlist(v.get("origin_countries"), 16) if len(c) >= 2],
        "original_language": _s(v.get("original_language"))[:8].lower(),
        "ratings": normalise_ratings(v.get("ratings")),
        "external_ids": normalise_external_ids(v.get("external_ids")),
    }
    if kind == KIND_SERIES:
        seasons = [normalise_season(s) for s in (v.get("seasons") or [])]
        out["seasons"] = [s for s in seasons if s]
    eps = v.get("episodes")
    if isinstance(eps, list):
        got = [normalise_episode(e) for e in eps]
        out["episodes"] = [e for e in got if e]
    return out


# ---- normalised stream candidate --------------------------------------------
def source_key(transport, info_hash=None, url=None):
    """The 40-hex identity a candidate is known by everywhere downstream.

    The transcoder names its output directory after it, the cache purge removes
    it by name, and /audio/<key> serves it. All of that already assumed a
    torrent info hash and validated the 40-hex shape before letting it near a
    path or a `docker exec`. Rather than loosen those checks for HTTP sources --
    which would weaken a real defence -- an HTTP source gets a key of the same
    shape: the SHA-1 of its URL. Same guarantees, no downstream change.
    """
    if transport == T_TORRENT:
        return (info_hash or "").lower()
    return hashlib.sha1(("http\x00" + (url or "")).encode("utf-8")).hexdigest()


def normalise_headers(v):
    """Request headers a source needs, e.g. an Authorization or a Referer.

    These are credentials. They stay in the server process: they are attached
    to the candidate, used by the /src/ proxy, and stripped from anything that
    reaches the TV, the API or a log.
    """
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, val in list(v.items())[:24]:
        name = _s(k).strip()
        sval = _s(val).strip()
        # no newlines: these are written into a request, and a header value
        # carrying CRLF is a request-splitting bug
        if not name or not sval or len(name) > 100 or len(sval) > 4096:
            continue
        if re.search(r"[\r\n\x00]", name + sval) or not re.match(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$", name):
            continue
        out[name] = sval
    return out


def normalise_candidate(v, provider_id):
    """A provider's playable source -> the shape the selection engine scores.

    Returns (candidate, None) or (None, "reason"). A provider mixing playable
    and unplayable rows costs only the unplayable rows, and the reason is
    counted and reported rather than swallowed.

    Field names deliberately match what parse_stream() used to return, so
    score(), probe_and_buffer(), stream_url() and the transcoder see exactly
    what they saw before.

    Note what is NOT required: seeders, size, codec. A direct HTTP source has no
    swarm and often no advertised size. Those come back as None meaning
    *unknown*, never 0 -- scoring treats unknown as "do not penalise", because a
    missing size used to cost 100 points and would wipe an HTTP-only provider's
    entire catalogue off the grid.
    """
    if not isinstance(v, dict):
        return None, "not an object"
    transport = _s(v.get("transport") or v.get("type"), T_TORRENT).strip().lower() or T_TORRENT
    if transport not in TRANSPORTS:
        return None, "unsupported transport %r" % transport[:40]

    info_hash = _s(v.get("info_hash") or v.get("infoHash")).strip().lower()
    url = _s(v.get("url")).strip()
    headers = normalise_headers(v.get("proxy_headers") or v.get("headers"))

    if transport == T_TORRENT:
        if not RE_HASH40.match(info_hash):
            return None, "malformed info hash"
        url, headers = "", {}
    else:
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            return None, "http source has no usable url"
        if len(url) > 4096:
            return None, "http source url is absurdly long"
        info_hash = ""

    idx = _i(v.get("file_index") if v.get("file_index") is not None else v.get("fileIdx"))
    if idx is not None and (idx < 0 or idx > 10000):
        idx = None

    size_gb = _f(v.get("size_gb"))
    if size_gb is not None and size_gb <= 0:
        size_gb = None
    seeders = _i(v.get("seeders"))
    if seeders is not None and seeders < 0:
        seeders = None

    codec = _s(v.get("codec")).strip().upper()
    codec = {"X265": "HEVC", "H265": "HEVC", "H.265": "HEVC",
             "X264": "H264", "H.264": "H264", "AVC": "H264",
             "AV01": "AV1"}.get(codec, codec)
    if codec not in ("HEVC", "H264", "AV1"):
        codec = "?"

    quality = _s(v.get("quality") or v.get("tag"))[:120]
    is4k = v.get("is_4k")
    if is4k is None:
        low = quality.lower()
        is4k = "4k" in low or "2160" in low

    return {
        "transport": transport,
        "provider": provider_id,
        "key": source_key(transport, info_hash, url),
        "infoHash": info_hash or None,
        "url": url or None,
        "headers": headers,
        "fileIdx": idx,
        # None, not 0: unknown must not be scored as "none"
        "seeders": seeders,
        "gb": round(size_gb, 2) if size_gb else None,
        "provider_name": _s(v.get("source") or v.get("provider_name"))[:120] or "?",
        "tag": quality,
        "codec": codec,
        "pack": _b(v.get("pack")),
        "langs": [l.lower()[:8] for l in _strlist(v.get("languages") or v.get("langs"), 24)],
        "hardsub": _b(v.get("hardsub")),
        "display": _s(v.get("display") or v.get("filename"))[:400],
        "is4k": _b(is4k),
    }, None


def public_candidate(c):
    """A candidate with every credential removed, for the API, the TV and logs.

    The /src/ proxy URL is what the rest of the system plays; the real upstream
    URL and its headers never leave this process.
    """
    if not isinstance(c, dict):
        return {}
    out = {k: v for k, v in c.items() if k not in ("headers", "url")}
    if c.get("transport") == T_HTTP:
        out["source"] = "direct"
    return out


# ---- manifest ---------------------------------------------------------------
def validate_field(f, where="config"):
    if not isinstance(f, dict):
        raise ContractError("%s: field is %s, expected an object" % (where, type(f).__name__))
    key = _s(f.get("key")).strip()
    if not re.match(r"^[a-z0-9][a-z0-9_]{0,48}$", key):
        raise ContractError("%s: bad field key %r" % (where, key[:40]))
    ftype = _s(f.get("type"), F_TEXT).strip().lower() or F_TEXT
    if ftype not in FIELD_TYPES:
        raise ContractError("%s: field %r has unknown type %r (expected one of %s)"
                            % (where, key, ftype[:24], ", ".join(FIELD_TYPES)))
    out = {"key": key, "type": ftype,
           "label": _s(f.get("label"))[:120] or key,
           "help": _s(f.get("help"))[:1000],
           "placeholder": _s(f.get("placeholder"))[:200],
           "required": _b(f.get("required")),
           "default": f.get("default")}
    if ftype == F_CHOICE:
        choices = []
        for c in (f.get("choices") or [])[:64]:
            if isinstance(c, dict):
                cv, cl = _s(c.get("value")), _s(c.get("label"))
            else:
                cv = cl = _s(c)
            if cv:
                choices.append({"value": cv, "label": cl or cv})
        if not choices:
            raise ContractError("%s: choice field %r declares no choices" % (where, key))
        out["choices"] = choices
    if ftype == F_BOOL:
        out["default"] = _b(out["default"])
    if ftype == F_NUMBER:
        out["default"] = _f(out["default"])
        out["min"], out["max"] = _f(f.get("min")), _f(f.get("max"))
    if ftype in SECRET_TYPES:
        # A default secret would mean shipping a credential inside a package.
        out["default"] = None
        out["required"] = _b(f.get("required"))
    return out


def validate_manifest(m):
    """Check a package's manifest.json and return it normalised.

    Runs before anything is installed and again before anything is executed,
    so a package that cannot be described is never run.
    """
    if not isinstance(m, dict):
        raise ContractError("manifest is %s, expected an object" % type(m).__name__)
    pid = _s(m.get("id")).strip().lower()
    if not RE_PROVIDER_ID.match(pid):
        raise ContractError("manifest id %r must be 2-64 chars of a-z, 0-9 and '-'" % pid[:40])
    version = _s(m.get("version")).strip()
    if not RE_VERSION.match(version):
        raise ContractError("manifest version %r is not a version number" % version[:40])
    contract = _i(m.get("contract"))
    if contract is None:
        raise ContractError("manifest declares no contract version")
    if contract != CONTRACT_VERSION:
        raise ContractError(
            "provider speaks contract v%d; this Cinematica speaks v%d"
            % (contract, CONTRACT_VERSION), code=E_UNSUPPORTED)
    caps = [c for c in _strlist(m.get("capabilities"), 8) if c in ROLES]
    if not caps:
        raise ContractError("manifest declares no usable role (expected %s)"
                            % " or ".join(ROLES))
    entry = _s(m.get("entry"), "provider.py").strip() or "provider.py"
    if "/" in entry or "\\" in entry or entry.startswith(".") or not entry.endswith(".py"):
        raise ContractError("manifest entry %r must be a .py file in the package root" % entry[:60])
    fields, seen = [], set()
    for f in (m.get("config") or [])[:64]:
        nf = validate_field(f, "manifest %s" % pid)
        if nf["key"] in seen:
            raise ContractError("manifest %s: duplicate config field %r" % (pid, nf["key"]))
        seen.add(nf["key"])
        fields.append(nf)
    return {
        "id": pid,
        "name": _s(m.get("name"))[:120] or pid,
        "version": version,
        "contract": contract,
        "capabilities": caps,
        "entry": entry,
        "description": _s(m.get("description"))[:2000],
        "instructions": _s(m.get("instructions"))[:8000],
        "setup_url": _url(m.get("setup_url")) or "",
        "homepage": _url(m.get("homepage")) or "",
        "author": _s(m.get("author"))[:120],
        "config": fields,
        # what this provider can additionally do inside its role
        "provides_ratings": _strlist(m.get("provides_ratings"), 8),
        "optional_ops": [o for o in _strlist(m.get("optional_ops"), 8) if o in OPTIONAL_OPS],
        # Which browse filters it can apply. Absent means "none" -- the safe
        # reading: core then drops the bias tiers rather than assuming a filter
        # took effect when it did not.
        "filters": sorted(f for f in _strlist(m.get("filters"), 16) if f in FILTERS),
        "kinds": [k for k in (_kind(x) for x in _strlist(m.get("kinds"), 4)) if k] or list(KINDS),
        # how the provider is reached: an installed python package, or an
        # HTTP add-on that core talks to over the network
        "runtime": "addon" if _s(m.get("runtime")) == "addon" else "package",
        "addon_url": _url(m.get("addon_url")) or "",
    }


def ops_for(manifest):
    """Every op this provider is expected to answer."""
    ops = {OP_DESCRIBE, OP_TEST}
    for role in manifest.get("capabilities") or ():
        ops.update(ROLE_OPS.get(role, ()))
    ops.update(manifest.get("optional_ops") or ())
    return ops


def normalise_preview(v, provider_id, strict_kind=None):
    """A browse/search result: identity, artwork and enough to rank it."""
    return normalise_entry(v, provider_id, strict_kind)


def normalise_detail(v, provider_id, strict_kind=None):
    """A title page: everything a preview has, plus seasons and episodes.

    Same normaliser -- the difference is which fields a provider bothers to
    fill, not a different shape. Keeping one shape means a catalogue entry can
    be shown immediately and enriched in place when metadata arrives, instead
    of the UI juggling two half-overlapping types.
    """
    e = normalise_entry(v, provider_id, strict_kind)
    e.setdefault("seasons", [])
    e.setdefault("episodes", [])
    return e


# ---- normalised channel / video / play ---------------------------------------
def normalise_channel(v, provider_id):
    """A followed channel: identity plus enough to show a row for it. Raises
    ContractError when there is no usable id or title -- every other field
    degrades to empty, the same trade-off normalise_entry makes."""
    if not isinstance(v, dict):
        raise ContractError("channel is %s, expected an object" % type(v).__name__)
    raw_id = _s(v.get("id")).strip()
    minted_by, unqualified = unqualify(raw_id)
    local_id = unqualified if minted_by == provider_id else raw_id
    local_id = local_id.strip()
    title = _s(v.get("title")).strip()
    if not local_id or not title:
        raise ContractError("channel has no usable id or title")
    return {
        "id": qualify(provider_id, local_id),
        "local_id": local_id,
        "kind": "channel",
        "title": title[:300],
        "avatar": _url(v.get("avatar")) or "",
        "banner": _url(v.get("banner")) or "",
        "subscribers": _i(v.get("subscribers")),
        "description": _s(v.get("description"))[:2000],
        "latest_at": _i(v.get("latest_at")),
    }


def normalise_video(v):
    """One upload: identity, when it appeared, and enough to show a row for
    it. Raises ContractError when there is no usable id or title."""
    if not isinstance(v, dict):
        raise ContractError("video is %s, expected an object" % type(v).__name__)
    local_id = _s(v.get("id")).strip()
    title = _s(v.get("title")).strip()
    if not local_id or not title:
        raise ContractError("video has no usable id or title")
    return {
        "id": local_id[:64],
        "title": title[:300],
        "published": _i(v.get("published")) or 0,
        "duration_s": _i(v.get("duration_s")),
        "thumb": _url(v.get("thumb")) or "",
        "description": _s(v.get("description"))[:2000],
        "views": _i(v.get("views")),
    }


def normalise_play(v):
    """{"url", "package", "label"} -- what the TV hands to an external app.
    "url" must be a complete http(s) URL (an app link/deep link is not a
    playable URL Cinematica's proxy or the TV's external-player intent can
    use); "package" names the app to open it with and may be empty, in which
    case the TV falls back to any app that handles the URL."""
    if not isinstance(v, dict):
        raise ContractError("play result is %s, expected an object" % type(v).__name__)
    url = _url(v.get("url"))
    if not url or not (url.lower().startswith("http://") or url.lower().startswith("https://")):
        raise ContractError("play result has no usable http(s) url")
    return {
        "url": url,
        "package": _s(v.get("package"))[:200],
        "label": _s(v.get("label"))[:80] or "another app",
    }
