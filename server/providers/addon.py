"""The generic Stremio-add-on adapter -- the PRIMARY way providers are added.

An admin pastes a manifest.json URL. This module speaks the Stremio HTTP
protocol to whatever is on the other end and reshapes its replies into the
normalised contract shapes core already works with. Nothing is downloaded and
executed: every function here either performs an HTTP GET or reshapes a dict
that already arrived. contract.py is imported, never modified.

Two entry points into the rest of the provider system:

  to_provider_manifest(raw, addon_url)  turns a fetched manifest.json into
                                         the dict contract.validate_manifest
                                         accepts, so an add-on installs
                                         exactly like a package provider.
  AddonProvider                         the instance core drives afterwards.
                                         It re-fetches the live manifest (a
                                         cheap in-process cache hides the
                                         repeat cost) rather than trusting a
                                         snapshot taken at install time, so a
                                         catalog the add-on later drops does
                                         not go on returning stale results.

No service-specific behaviour: everything below reads declared, structured
Stremio fields. The one deliberate exception -- a free-text size/seeder
scrape -- is fenced off in _extract_size_seeders() and documented there.
"""

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import contract

# ---- HTTP --------------------------------------------------------------------
# Some stream indexes sit behind Cloudflare and have to be fetched with a
# browser-like UA. An add-on URL here was pasted by the admin on
# purpose, so there is nothing to defeat -- a plain, honest UA is preferable
# because it is what shows up in the add-on's own logs if something goes wrong.
UA = "Cinematica-addon-client/1"
DEFAULT_TIMEOUT_S = 15
# A hostile or merely broken add-on could otherwise stream an unbounded body
# at a server that expects a few KB of JSON and exhaust memory doing so.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# One opener, reused for every request -- the point of naming it is so this
# stays true if this module ever needs to add cookie/redirect handling later.
_opener = urllib.request.build_opener()


def _read_capped(resp, cap=MAX_RESPONSE_BYTES):
    data = resp.read(cap + 1)
    if len(data) > cap:
        raise contract.ProviderError(contract.E_PROTOCOL,
            "add-on response exceeded %d bytes" % cap)
    return data


def _http_get_json(url, timeout=DEFAULT_TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = _read_capped(resp)
    except urllib.error.HTTPError as ex:
        # 429 is "back off and retry", not "this add-on is broken" -- conflating
        # them would make a rate-limited add-on look permanently dead.
        code = contract.E_RATE if ex.code == 429 else contract.E_UPSTREAM
        raise contract.ProviderError(code,
            "add-on returned HTTP %d for %s" % (ex.code, contract.redact(url))) from ex
    except (urllib.error.URLError, OSError, TimeoutError) as ex:
        raise contract.ProviderError(contract.E_UPSTREAM,
            "add-on unreachable: %s" % contract.redact(str(ex))) from ex
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as ex:
        raise contract.ProviderError(contract.E_PROTOCOL,
            "add-on did not return JSON for %s" % contract.redact(url)) from ex


# ---- manifest fetch + cache ---------------------------------------------------
# Short TTL: AddonProvider re-fetches the manifest on nearly every call (to see
# catalogs/idPrefixes as the add-on currently declares them, not as they were
# at install time), so without a cache every browse/search/details/streams
# call would cost two round trips instead of one.
MANIFEST_TTL_S = 300

_manifest_cache_lock = threading.Lock()
_manifest_cache = {}  # base_url -> (fetched_at_monotonic, manifest_dict)


def _normalise_addon_base(url):
    """Strip a trailing /manifest.json (and slash) so a manifest URL and the
    add-on's bare base URL land on the same cache entry and the same computed
    catalog/meta/stream endpoints, whichever form the admin pasted."""
    u = (url or "").strip()
    if u.lower().endswith("/manifest.json"):
        u = u[: -len("/manifest.json")]
    return u.rstrip("/")


def fetch_manifest(url, timeout=15):
    """Raw manifest.json dict for `url`, with or without the trailing filename."""
    base = _normalise_addon_base(url)
    if not base or not base.lower().startswith(("http://", "https://")):
        raise contract.ProviderError(contract.E_CONFIG,
            "add-on url %r is not a valid http(s) url" % (url or "")[:200])
    now = time.monotonic()
    with _manifest_cache_lock:
        hit = _manifest_cache.get(base)
        if hit is not None and now - hit[0] < MANIFEST_TTL_S:
            return hit[1]
    manifest = _http_get_json(base + "/manifest.json", timeout=timeout)
    if not isinstance(manifest, dict):
        raise contract.ProviderError(contract.E_PROTOCOL, "manifest.json is not an object")
    with _manifest_cache_lock:
        _manifest_cache[base] = (now, manifest)
    return manifest


# ---- kind <-> Stremio type -----------------------------------------------------
# Written out explicitly rather than relied on as a string coincidence: contract
# happens to spell KIND_MOVIE/KIND_SERIES the same as Stremio's own type
# strings today, but a future contract kind rename must not silently break this.
_KIND_TO_STREMIO = {contract.KIND_MOVIE: "movie", contract.KIND_SERIES: "series"}
_STREMIO_TO_KIND = {v: k for k, v in _KIND_TO_STREMIO.items()}


# ---- manifest -> provider manifest --------------------------------------------
_RESOURCE_TO_ROLE = {
    "catalog": contract.ROLE_CATALOGUE,
    "meta": contract.ROLE_METADATA,
    "stream": contract.ROLE_STREAMS,
}


def _resource_names(raw):
    """`resources` may be a list of strings or a list of {name,types,idPrefixes}
    objects (Stremio's scoped long form) -- both are handled everywhere a
    manifest is read, so an add-on that only uses one form is never treated as
    if it declared nothing."""
    names = set()
    for r in raw.get("resources") or []:
        if isinstance(r, str):
            names.add(r)
        elif isinstance(r, dict) and isinstance(r.get("name"), str):
            names.add(r["name"])
    return names


def _catalog_extra_names(catalog):
    """Extra parameter names a catalog declares, across the current manifest
    shape (`extra: [{name,...}]`) and the pre-SDK3 one (`extraSupported`) --
    add-ons in the wild still ship the old form, and reading only the new one
    would make genre/search support silently vanish for them."""
    names = set()
    for e in catalog.get("extra") or []:
        if isinstance(e, dict) and isinstance(e.get("name"), str):
            names.add(e["name"])
    for n in catalog.get("extraSupported") or []:
        if isinstance(n, str):
            names.add(n)
    return names


def _catalog_genre_options(catalog):
    for e in catalog.get("extra") or []:
        if isinstance(e, dict) and e.get("name") == "genre" and isinstance(e.get("options"), list):
            return [str(o) for o in e["options"] if isinstance(o, str)]
    legacy = catalog.get("genres")  # some pre-SDK3 add-ons put options here
    if isinstance(legacy, list):
        return [str(g) for g in legacy if isinstance(g, str)]
    return []


def _slugify_provider_id(raw_id, addon_url):
    s = re.sub(r"[^a-z0-9-]+", "-", (raw_id or "").strip().lower()).strip("-")
    if not contract.RE_PROVIDER_ID.match(s):
        # No usable id, or one too short/odd-shaped to pass the contract's
        # pattern -- fall back to a hash of the URL so two such add-ons still
        # get distinct, STABLE ids across restarts rather than colliding on
        # one fallback name (which would make the second overwrite the first).
        s = "addon-" + hashlib.sha1(addon_url.encode("utf-8")).hexdigest()[:12]
    return s[:64]


def _safe_version(v):
    s = str(v or "").strip()
    return s if contract.RE_VERSION.match(s) else "0.0.0"


def to_provider_manifest(raw, addon_url):
    """A fetched manifest.json -> the dict contract.validate_manifest accepts."""
    if not isinstance(raw, dict):
        raise contract.ProviderError(contract.E_PROTOCOL, "manifest.json is not an object")

    resource_names = _resource_names(raw)
    caps = sorted({_RESOURCE_TO_ROLE[n] for n in resource_names if n in _RESOURCE_TO_ROLE})
    if not caps:
        raise contract.ProviderError(contract.E_UNSUPPORTED,
            "add-on declares no usable resource (got %s; need one of catalog/meta/stream)"
            % (", ".join(sorted(resource_names)) or "none"))

    kinds = [k for k in (_STREMIO_TO_KIND.get(t) for t in (raw.get("types") or [])) if k]

    # One "which catalog to browse" choice, built from whatever catalogs the
    # add-on declares -- this is what lets the settings page render a picker
    # with no code change per add-on. Deduplicated by id: Stremio add-ons
    # routinely reuse the same catalog id across movie and series (Cinemeta's
    # "top" is both), and listing it twice would just be a confusing UI, not a
    # different capability.
    choices, seen = [], set()
    for c in raw.get("catalogs") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        choices.append({"value": cid, "label": str(c.get("name") or cid)[:120]})
    config = []
    if choices:
        config.append({
            "key": "catalog",
            "type": contract.F_CHOICE,
            "label": "Catalogue",
            "help": "Which of this add-on's catalogues to browse.",
            "choices": choices,
            "default": choices[0]["value"],
        })

    # Stremio has no vote/country/language extras, and no numeric genre ids --
    # so genre_ids is the only browse filter that can ever be declared here,
    # and only when some catalog actually offers a `genre` extra. Leaving
    # every other FILTERS entry undeclared is deliberate: core drops the bias
    # tiers that exist only to express a filter nothing can apply, rather than
    # sending every tier the same unfiltered query.
    has_genre_extra = any(
        "genre" in _catalog_extra_names(c) for c in (raw.get("catalogs") or []) if isinstance(c, dict))
    filters = ["genre_ids"] if has_genre_extra else []

    built = {
        "id": _slugify_provider_id(raw.get("id"), addon_url),
        "name": str(raw.get("name") or "")[:120],
        "version": _safe_version(raw.get("version")),
        "contract": contract.CONTRACT_VERSION,
        "capabilities": caps,
        "description": str(raw.get("description") or ""),
        "instructions": str(raw.get("instructions") or ""),
        "config": config,
        "filters": filters,
        "kinds": kinds,
        "runtime": "addon",
        "addon_url": _normalise_addon_base(addon_url),
    }
    try:
        return contract.validate_manifest(built)
    except contract.ContractError as ex:
        # Should not happen given the construction above; surfaced as a named
        # provider error rather than a ContractError leaking out of this
        # module's only advertised contract, so callers only ever catch one
        # exception type from this function.
        raise contract.ProviderError(contract.E_PROTOCOL, "built an invalid manifest: %s" % ex) from ex


# ---- Stremio meta -> contract entry --------------------------------------------
def _year_from_release_info(v):
    """releaseInfo is "1999" or a range like "2005-2013" -- only the first
    year is meaningful to core, which has no concept of a year range."""
    m = re.match(r"^\s*(\d{4})", str(v or ""))
    return m.group(1) if m else None


def _parse_runtime(v):
    """Stremio's `runtime` is documented as a string ("148 min") in practice,
    though some add-ons just put an int. Either way only the leading number
    is usable -- contract.normalise_entry's int() coercion would otherwise
    silently drop the whole field for every string-runtime add-on."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    m = re.match(r"^\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def _meta_to_entry(meta):
    """Reshape a MetaPreview/MetaDetail into what contract.normalise_entry
    expects. Only renames and the two fields (releaseInfo, runtime) whose
    shape Stremio does not pin down are touched -- everything else (poster,
    backdrop, description, ...) already matches a contract field name or is
    simply not read by normalise_entry, so it passes through untouched."""
    out = dict(meta)
    out["title"] = meta.get("name")
    out["overview"] = meta.get("description")
    year = _year_from_release_info(meta.get("releaseInfo"))
    if year:
        out["year"] = year
    runtime = _parse_runtime(meta.get("runtime"))
    if runtime is not None:
        out["runtime"] = runtime
    genres = meta.get("genres")
    if isinstance(genres, list):
        # Stremio genres ARE the genre ids -- they are names, not numbers --
        # so a catalog entry can be genre-filtered downstream with no separate
        # id lookup (see genres() below for why that is exactly the point).
        out["genre_ids"] = genres
    mid = meta.get("id")
    if isinstance(mid, str) and contract.RE_IMDB.match(mid):
        ext = dict(meta.get("external_ids") or {})
        ext.setdefault("imdb", mid)
        out["external_ids"] = ext
    rating = meta.get("imdbRating")
    if rating is not None:
        try:
            out["ratings"] = {"imdb": {"value": float(rating)}}
        except (TypeError, ValueError):
            pass
    return out


def _video_to_episode(v):
    if not isinstance(v, dict) or v.get("season") is None or v.get("episode") is None:
        return None
    return {
        "season": v.get("season"), "episode": v.get("episode"),
        # Documented field is "title", but plenty of add-ons (this includes
        # the reference Cinemeta implementation, checked live) put the
        # episode's own title under "name" instead -- the same title-or-name
        # fallback contract.normalise_entry already applies to a catalogue
        # entry's own title, applied here for the same generic reason.
        "name": v.get("title") or v.get("name"), "overview": v.get("overview"),
        "air": v.get("released"), "still": v.get("thumbnail"),
    }


def _seasons_from_videos(videos):
    """Stremio has no season object of its own -- season metadata (which
    numbers exist, how many episodes each has) only ever exists implicitly in
    which season/episode pairs appear in `videos`, so it is derived rather
    than read from a dedicated field."""
    counts = {}
    for v in videos:
        if isinstance(v, dict) and v.get("season") is not None:
            counts[v["season"]] = counts.get(v["season"], 0) + 1
    return [{"n": n, "episodes": counts[n]} for n in sorted(counts)]


# ---- generic size/seeder extractor ---------------------------------------------
# Stremio's Stream object has exactly one structured size signal
# (behaviorHints.videoSize, in bytes) and none at all for seeders. Most
# torrent-backed add-ons that DO report a size or a swarm count put it as
# free text in name/title/description instead. What follows is a lowest-
# common-denominator scrape of that free text: a "12.4 GB" style size pattern
# and a bare count labelled seeds/seeders/peers or marked with the
# community-conventional (not specific to any one add-on -- widely copied
# between them) person emoji. It is NOT modelled on any
# particular add-on's layout, and it is best-effort BY DESIGN: a miss yields
# None (unknown), never 0, because an add-on that simply never reports size or
# seeders must not end up looking emptier or less-seeded than one that does.
_RE_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)\b", re.I)
_RE_SEED_EMOJI = re.compile(r"\U0001F464\s*(\d+)")
_RE_SEED_WORD = re.compile(r"(\d+)\s*(?:seeds?|seeders?|peers?)\b|\b(?:seeds?|seeders?|peers?)[:=]?\s*(\d+)", re.I)
_SIZE_UNIT_BYTES = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def _extract_size_seeders(stream):
    bh = stream.get("behaviorHints") or {}
    size_gb = None
    video_size = bh.get("videoSize")
    if isinstance(video_size, (int, float)) and not isinstance(video_size, bool) and video_size > 0:
        size_gb = video_size / (1024.0 ** 3)

    blob = " ".join(str(stream.get(k) or "") for k in ("name", "title", "description"))
    blob += " " + str(bh.get("filename") or "")

    if size_gb is None:
        m = _RE_SIZE.search(blob)
        if m:
            size_gb = float(m.group(1)) * _SIZE_UNIT_BYTES[m.group(2).upper()] / (1024.0 ** 3)

    seeders = None
    m = _RE_SEED_EMOJI.search(blob) or _RE_SEED_WORD.search(blob)
    if m:
        digits = next(g for g in m.groups() if g is not None)
        seeders = int(digits)

    return size_gb, seeders


# ---- Stremio stream -> contract candidate --------------------------------------
def _stream_to_candidate(s, provider_id):
    if not isinstance(s, dict):
        return None, "not an object"

    if s.get("infoHash"):
        v = {"transport": contract.T_TORRENT, "infoHash": s.get("infoHash"), "fileIdx": s.get("fileIdx")}
    elif isinstance(s.get("url"), str) and s["url"].lower().startswith(("http://", "https://")):
        # Core never lets these headers leave the server (see contract.T_HTTP) --
        # they ride the candidate only as far as the /src/ proxy that injects them.
        headers = ((s.get("behaviorHints") or {}).get("proxyHeaders") or {}).get("request")
        v = {"transport": contract.T_HTTP, "url": s["url"], "proxy_headers": headers or {}}
    elif "ytId" in s:
        return None, "ytId (not a playable transport)"
    elif "externalUrl" in s:
        return None, "externalUrl (not a playable transport)"
    else:
        return None, "no infoHash or http(s) url"

    size_gb, seeders = _extract_size_seeders(s)
    v["size_gb"] = size_gb
    v["seeders"] = seeders
    v["quality"] = s.get("name") or s.get("title") or ""
    v["display"] = (s.get("behaviorHints") or {}).get("filename") or s.get("title") or s.get("name") or ""

    cand, reason = contract.normalise_candidate(v, provider_id)
    if cand is None:
        return None, reason
    return cand, None


def _resolve_stream_id(raw, identity):
    """The id to put in /stream/{type}/{id}.json -- see AddonProvider.streams()."""
    local_id = str((identity or {}).get("local_id") or "")
    if contract.RE_IMDB.match(local_id):
        return local_id
    id_prefixes = raw.get("idPrefixes")
    wants_imdb = isinstance(id_prefixes, list) and any(str(p) == "tt" for p in id_prefixes)
    imdb = ((identity or {}).get("external_ids") or {}).get("imdb")
    if wants_imdb:
        if imdb and contract.RE_IMDB.match(imdb):
            return imdb
        raise contract.ProviderError(contract.E_UNSUPPORTED,
            "add-on only accepts ids prefixed with %r; %r has no IMDb id"
            % (id_prefixes, (identity or {}).get("title") or local_id))
    if local_id:
        return local_id
    raise contract.ProviderError(contract.E_UNSUPPORTED,
        "no usable id for this title (add-on accepts: %s)" % (id_prefixes or ["its own ids"]))


def _catalogs_for_kind(raw, kind):
    stremio_type = _KIND_TO_STREMIO.get(kind)
    cats = [c for c in (raw.get("catalogs") or []) if isinstance(c, dict) and c.get("type") == stremio_type]
    return cats, stremio_type


def _pint(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class AddonProvider:
    """Talks to one already-installed Stremio add-on over HTTP.

    `provider_manifest` is the contract.validate_manifest output produced by
    to_provider_manifest(); `config` is the admin's chosen field values (today
    just {"catalog": <id>}). Every method re-fetches the live manifest (via
    the module-level cache) rather than trusting either of those to still
    describe what the add-on currently serves.
    """

    def __init__(self, provider_manifest, config):
        self.manifest = dict(provider_manifest or {})
        self.config = dict(config or {})
        self.provider_id = self.manifest.get("id", "")
        self.addon_url = self.manifest.get("addon_url", "")
        if not self.addon_url:
            raise contract.ProviderError(contract.E_CONFIG, "add-on has no addon_url configured",
                                          provider=self.provider_id)

    # -- internal helpers --
    def _raw(self):
        return fetch_manifest(self.addon_url)

    def _select_catalog(self, kind):
        raw = self._raw()
        cats, stremio_type = _catalogs_for_kind(raw, kind)
        if not cats:
            raise contract.ProviderError(contract.E_UNSUPPORTED,
                "add-on declares no %s catalog" % (stremio_type or kind), provider=self.provider_id)
        wanted = self.config.get("catalog")
        chosen = next((c for c in cats if str(c.get("id")) == str(wanted)), None) or cats[0]
        return raw, chosen, stremio_type

    def _endpoint(self, resource, stremio_type, ident, extra=None):
        path = "/%s/%s/%s" % (resource, urllib.parse.quote(stremio_type, safe=""),
                               urllib.parse.quote(ident, safe=":"))
        if extra:
            # Stremio embeds `extra` as a literal querystring-shaped PATH
            # segment ("skip=100&genre=Action"), not a real query string on
            # the request -- urlencode() happens to produce exactly that shape.
            path += "/" + urllib.parse.urlencode(extra)
        return _normalise_addon_base(self.addon_url) + path + ".json"

    # -- ops --
    def describe(self):
        return dict(self.manifest)

    def test(self):
        raw = fetch_manifest(self.addon_url, timeout=10)
        return {
            "ok": True,
            "name": raw.get("name") or self.manifest.get("name"),
            "resources": sorted(_resource_names(raw)),
            "types": [t for t in (raw.get("types") or []) if isinstance(t, str)],
            "catalogs": len([c for c in (raw.get("catalogs") or []) if isinstance(c, dict)]),
        }

    def genres(self, kind):
        _, catalog, _ = self._select_catalog(kind)
        return [{"id": g, "name": g} for g in _catalog_genre_options(catalog)]

    def browse(self, kind, page=1, page_size=20, sort=None, filters=None):
        filters = filters or {}
        page = max(1, _pint(page, 1))
        page_size = max(1, _pint(page_size, 20))
        # Stremio has no generic sort extra -- a catalog's order is whatever
        # the add-on chose. `sort` is accepted for interface parity and
        # otherwise ignored: pretending an order was applied would be worse
        # than admitting there isn't one.
        raw, catalog, stremio_type = self._select_catalog(kind)

        extra = {}
        skip = (page - 1) * page_size
        if skip:
            extra["skip"] = skip
        genre_ids = filters.get("genre_ids") or []
        want_genre = genre_ids[0] if genre_ids else None
        if want_genre:
            if "genre" not in _catalog_extra_names(catalog):
                raise contract.ProviderError(contract.E_UNSUPPORTED,
                    "catalog %r does not support genre filtering" % catalog.get("id"),
                    provider=self.provider_id)
            offered = _catalog_genre_options(catalog)
            if want_genre not in offered:
                raise contract.ProviderError(contract.E_UNSUPPORTED,
                    "genre %r is not offered by catalog %r (offered: %s)"
                    % (want_genre, catalog.get("id"), ", ".join(offered) or "none"),
                    provider=self.provider_id)
            extra["genre"] = want_genre

        url = self._endpoint("catalog", stremio_type, str(catalog.get("id")), extra)
        body = _http_get_json(url)
        metas = body.get("metas") if isinstance(body, dict) else None
        metas = metas if isinstance(metas, list) else []

        entries = []
        for m in metas:
            if not isinstance(m, dict):
                continue
            try:
                entries.append(contract.normalise_preview(_meta_to_entry(m), self.provider_id, strict_kind=kind))
            except contract.ContractError:
                continue  # one bad row costs a row, not the page
        # "has_more" reflects the wire response, not how many rows survived
        # normalisation -- otherwise a page with one malformed entry would
        # look short and stop pagination one page early.
        return {"entries": entries, "has_more": len(metas) >= page_size}

    def search(self, kind, query, limit=20):
        raw = self._raw()
        cats, stremio_type = _catalogs_for_kind(raw, kind)
        searchable = [c for c in cats if "search" in _catalog_extra_names(c)]
        if not searchable:
            raise contract.ProviderError(contract.E_UNSUPPORTED,
                "no %s catalog on this add-on declares a 'search' extra" % (stremio_type or kind),
                provider=self.provider_id)
        catalog = searchable[0]
        url = self._endpoint("catalog", stremio_type, str(catalog.get("id")), {"search": query})
        body = _http_get_json(url)
        metas = body.get("metas") if isinstance(body, dict) else None
        metas = metas if isinstance(metas, list) else []

        entries = []
        for m in metas[: max(1, _pint(limit, 20))]:
            if not isinstance(m, dict):
                continue
            try:
                entries.append(contract.normalise_preview(_meta_to_entry(m), self.provider_id, strict_kind=kind))
            except contract.ContractError:
                continue
        return {"entries": entries}

    def details(self, local_id, kind):
        stremio_type = _KIND_TO_STREMIO.get(kind)
        if stremio_type is None:
            raise contract.ProviderError(contract.E_UNSUPPORTED, "unknown kind %r" % (kind,),
                                          provider=self.provider_id)
        url = self._endpoint("meta", stremio_type, local_id)
        body = _http_get_json(url)
        meta = body.get("meta") if isinstance(body, dict) else None
        if not isinstance(meta, dict):
            raise contract.ProviderError(contract.E_NOTFOUND,
                "add-on has no meta for %s %s" % (kind, local_id), provider=self.provider_id)

        entry = _meta_to_entry(meta)
        videos = meta.get("videos") if isinstance(meta.get("videos"), list) else []
        entry["episodes"] = [e for e in (_video_to_episode(v) for v in videos) if e]
        entry["seasons"] = _seasons_from_videos(videos)
        try:
            return contract.normalise_detail(entry, self.provider_id, strict_kind=kind)
        except contract.ContractError as ex:
            raise contract.ProviderError(contract.E_PROTOCOL, str(ex), provider=self.provider_id) from ex

    def episodes(self, local_id, season):
        detail = self.details(local_id, contract.KIND_SERIES)
        want = _pint(season, None)
        eps = [e for e in detail.get("episodes", []) if want is None or e.get("season") == want]
        return {"episodes": eps}

    def streams(self, identity, season=None, episode=None):
        identity = identity or {}
        kind = identity.get("kind")
        stremio_type = _KIND_TO_STREMIO.get(kind)
        if stremio_type is None:
            raise contract.ProviderError(contract.E_UNSUPPORTED, "unknown kind %r" % (kind,),
                                          provider=self.provider_id)
        raw = self._raw()
        stream_id = _resolve_stream_id(raw, identity)
        if kind == contract.KIND_SERIES and season is not None and episode is not None:
            stream_id = "%s:%d:%d" % (stream_id, int(season), int(episode))

        url = self._endpoint("stream", stremio_type, stream_id)
        body = _http_get_json(url)
        raw_streams = body.get("streams") if isinstance(body, dict) else None
        raw_streams = raw_streams if isinstance(raw_streams, list) else []

        candidates, rejected = [], {}
        for s in raw_streams:
            cand, reason = _stream_to_candidate(s, self.provider_id)
            if cand is None:
                rejected[reason] = rejected.get(reason, 0) + 1
            else:
                candidates.append(cand)
        return {"candidates": candidates, "rejected": rejected}
