"""A complete, working provider package -- the smallest one worth reading.

It fills four roles (catalogue, metadata, streams, and the optional channels
role) from a JSON file you host yourself: a handful of films, and a couple of
followed channels with their uploads. It exists so that nobody has to
reconstruct the contract from providers/contract.py and providers/host.py to
write their first package. Copy this directory, replace the library, keep the
shapes.

How it is run: Cinematica launches `python3 -m providers.host <this dir>` and
speaks one JSON request per line to it. The host imports this file and calls
the function named after the operation, with the dots replaced by underscores
-- `catalogue.browse` calls catalogue_browse(config, params). An operation
whose function is missing is answered as "unsupported"; a module-level
`handle(op, config, params)` catches everything instead, if you prefer one
entry point.

Every reply is normalised by the host before core sees it (contract.py's
normalise_entry / normalise_candidate), so extra keys are dropped, types are
coerced, and a shape that cannot be made sense of comes back as a named error
against this provider rather than an exception somewhere in core. Return
plain dicts and lists; do not import anything from Cinematica except
`providers.contract`, which is on the path for exactly this purpose.

Errors: raise contract.ProviderError(code, message). The code decides what the
interface shows and whether the answer is cached -- E_CONFIG for "the operator
has not filled this in", E_NOTFOUND for "that id does not exist here",
E_UPSTREAM for "the service I depend on failed", E_UNSUPPORTED for "I do not
do that". Anything else you raise becomes E_INTERNAL, which is honest but
tells the operator nothing.
"""
import json
import os

from providers import contract

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_LIBRARY = os.path.join(_HERE, "library.example.json")


# ---- configuration ----------------------------------------------------------
# config is the dict the operator filled in from manifest.json's "config"
# fields. It arrives with every request -- never stashed in a module global,
# because the same process may be reconfigured, and a secret held in a global
# outlives the request that was allowed to see it.
def _base_url(config):
    base = str((config or {}).get("base_url") or "").strip().rstrip("/")
    if not base:
        raise contract.ProviderError(
            contract.E_CONFIG, "Base URL is not set: nothing can be played without it.")
    return base


def _read_library_file(config):
    path = str((config or {}).get("library_file") or "").strip() or _DEFAULT_LIBRARY
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise contract.ProviderError(contract.E_CONFIG, "No library file at %s" % path)
    except (OSError, ValueError) as exc:
        # redact() strips anything that looks like a credential out of a
        # message before it reaches a log or the interface. Use it on anything
        # that quotes an exception, a URL or a configured value.
        raise contract.ProviderError(
            contract.E_CONFIG, "Library file %s could not be read: %s" % (path, contract.redact(exc)))
    # Read on every request on purpose: editing the JSON takes effect without
    # reinstalling. A provider doing real I/O would cache here instead, keyed
    # on whatever its service calls a version.
    return data, path


def _library(config):
    data, path = _read_library_file(config)
    titles = data.get("titles") if isinstance(data, dict) else data
    if not isinstance(titles, list):
        raise contract.ProviderError(contract.E_CONFIG, "Library file has no 'titles' list.")
    return [t for t in titles if isinstance(t, dict) and t.get("id")]


def _channels(config):
    """The channel rows for the optional channels role. Its own top-level key
    -- "channels" is not a title, so it does not belong mixed into "titles"
    the way _library() reads them."""
    data, path = _read_library_file(config)
    channels = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(channels, list):
        raise contract.ProviderError(contract.E_CONFIG, "Library file has no 'channels' list.")
    return [c for c in channels if isinstance(c, dict) and c.get("id")]


def _entry(row):
    """One library row -> one catalogue entry.

    Only "id" and "kind" are required; every other field is optional and
    degrades to empty. ids are yours and opaque to core, which qualifies them
    as "<provider id>:<your id>" so that swapping catalogues cannot make a
    saved id mean a different film.
    """
    return {
        "id": str(row["id"]),
        "kind": contract.KIND_MOVIE,
        "title": row.get("title") or str(row["id"]),
        "overview": row.get("overview") or "",
        "year": row.get("year"),
        "genres": row.get("genres") or [],
        "genre_ids": [str(g).lower() for g in (row.get("genres") or [])],
        "poster": row.get("poster") or "",
        "backdrop": row.get("backdrop") or "",
        "original_language": row.get("original_language") or "",
        # An IMDb id here is what lets a DIFFERENT provider recognise the same
        # film -- it is the one identifier with agreed meaning across
        # providers. Omit it rather than invent one.
        "external_ids": row.get("external_ids") or {},
    }


def _find(rows, local_id):
    for row in rows:
        if str(row["id"]) == str(local_id):
            return row
    raise contract.ProviderError(contract.E_NOTFOUND, "No title %r in this library." % local_id)


# ---- the two operations every provider must answer --------------------------
def provider_describe(config, params):
    """Called to confirm the package runs at all. No network, no credentials."""
    return {"id": "example-library", "capabilities": ["catalogue", "metadata", "streams", "channels"],
            "titles": len(_library(config)), "channels": len(_channels(config)),
            # channels.search and channels.popular are optional within the
            # channels role (see docs/PROVIDERS.md); advertising them here is
            # what lets core offer them for this install without every
            # channels provider being obliged to implement both.
            "channel_ops": ["channels.search", "channels.popular"]}


def config_test(config, params):
    """What the interface's test button runs.

    A provider that talks to a service should make its smallest authenticated
    call here -- that is the whole point of the button. This one has no
    service, so it checks the two things that can actually be wrong.
    """
    base, rows = _base_url(config), _library(config)
    return {"ok": True, "base_url": base, "titles": len(rows)}


# ---- catalogue role ---------------------------------------------------------
def catalogue_genres(config, params):
    """[{"id", "name"}]. The ids are matched against an entry's genre_ids."""
    names = []
    for row in _library(config):
        for g in row.get("genres") or []:
            if g not in names:
                names.append(g)
    return [{"id": str(g).lower(), "name": str(g)} for g in sorted(names)]


def catalogue_browse(config, params):
    """One page of the catalogue.

    params: kind, page (1-based), page_size, sort, filters. Apply only the
    filters the manifest declares in "filters" -- this one declares none, so
    core knows not to ask and never assumes a filter took effect. Returning
    has_more explicitly beats letting core guess from the page length.
    """
    params = params or {}
    if params.get("kind") not in (None, contract.KIND_MOVIE):
        return {"items": [], "has_more": False}   # series: this library has none
    rows = _library(config)
    page = max(1, int(params.get("page") or 1))
    size = max(1, int(params.get("page_size") or 20))
    start = (page - 1) * size
    window = rows[start:start + size]
    return {"items": [_entry(r) for r in window], "has_more": start + size < len(rows)}


def catalogue_search(config, params):
    params = params or {}
    query = str(params.get("query") or "").strip().lower()
    limit = max(1, int(params.get("limit") or 20))
    if not query:
        return {"items": []}
    hits = [r for r in _library(config) if query in str(r.get("title") or "").lower()]
    return {"items": [_entry(r) for r in hits[:limit]]}


# ---- metadata role ----------------------------------------------------------
def metadata_details(config, params):
    """Everything a catalogue entry has, plus whatever a title page wants."""
    params = params or {}
    row = _find(_library(config), params.get("id"))
    detail = _entry(row)
    detail["runtime"] = row.get("runtime")
    return detail


def metadata_episodes(config, params):
    """Required by the metadata role, even here.

    This library holds films only, so the honest answer is an empty list. A
    provider that genuinely cannot answer an operation should raise
    ProviderError(E_UNSUPPORTED, ...) instead -- an empty list means "none",
    which is a different thing.
    """
    return []


# ---- streams role -----------------------------------------------------------
def streams_lookup(config, params):
    """The playable sources for one title.

    params["identity"] is the title core wants: local_id, title, kind and
    external_ids. For a series, params["season"] and params["episode"] are
    filled in too. Return every candidate you have; core scores them.

    Two transports exist. "http" needs a url, and may carry proxy_headers,
    which the server keeps and replays when it fetches the media -- they never
    reach the TV. "torrent" needs a 40-character info_hash instead. Unknown
    size, codec or seeders are omitted rather than sent as zero: zero is a
    measurement, and it is scored like one.
    """
    params = params or {}
    identity = params.get("identity") or {}
    local_id = identity.get("local_id") or identity.get("id")
    row = _find(_library(config), local_id)
    path = str(row.get("path") or "").lstrip("/")
    if not path:
        raise contract.ProviderError(contract.E_NOTFOUND, "%r has no file path in the library." % local_id)
    return {"candidates": [{
        "transport": "http",
        "url": "%s/%s" % (_base_url(config), path),
        "source": "Example Library",
        "display": os.path.basename(path),
        "quality": row.get("quality") or "",
        "codec": row.get("codec") or "",
        "size_gb": row.get("size_gb"),
        "languages": row.get("languages") or [],
        # "proxy_headers": {"Authorization": "Bearer ..."} if your files need
        # one. Whatever is put here stays on the server.
    }]}


# ---- channels role (optional) ------------------------------------------------
# Followed channels and their uploads, played through an external app rather
# than Cinematica's own stream path -- see docs/PROVIDERS.md's channels
# section. Setup never requires this role; resolve/details/latest/play are
# what the role obliges, search/popular are optional and advertised through
# provider_describe's "channel_ops" above.
def _find_channel(rows, local_id):
    for row in rows:
        if str(row["id"]).lower() == str(local_id).lower():
            return row
    raise contract.ProviderError(contract.E_NOTFOUND, "No channel %r in this library." % local_id)


def _channel_entry(row):
    return {
        "id": str(row["id"]),
        "title": row.get("title") or str(row["id"]),
        "avatar": row.get("avatar") or "",
        "banner": row.get("banner") or "",
        "subscribers": row.get("subscribers"),
        "description": row.get("description") or "",
    }


def _video_entry(row):
    return {
        "id": str(row["id"]),
        "title": row.get("title") or str(row["id"]),
        "published": row.get("published"),
        "duration_s": row.get("duration_s"),
        "thumb": row.get("thumb") or "",
        "description": row.get("description") or "",
    }


def channels_resolve(config, params):
    """A channel by id, or "@id" -- the shape a person pastes in, not a
    search. An unmatched query is a named not-found, not an empty result."""
    params = params or {}
    query = str(params.get("query") or "").strip()
    if query.startswith("@"):
        query = query[1:]
    for row in _channels(config):
        if str(row["id"]).lower() == query.lower():
            return _channel_entry(row)
    raise contract.ProviderError(contract.E_NOTFOUND, "No channel matches %r." % query)


def channels_details(config, params):
    params = params or {}
    return _channel_entry(_find_channel(_channels(config), params.get("id")))


def channels_latest(config, params):
    """Uploads, newest first -- what channels.latest promises, regardless of
    the order they happen to sit in the library file."""
    params = params or {}
    row = _find_channel(_channels(config), params.get("id"))
    videos = sorted(row.get("videos") or [], key=lambda v: v.get("published") or 0, reverse=True)
    return {"videos": [_video_entry(v) for v in videos]}


def channels_search(config, params):
    params = params or {}
    query = str(params.get("query") or "").strip().lower()
    limit = max(1, int(params.get("limit") or 20))
    if not query:
        return {"items": []}
    hits = [c for c in _channels(config) if query in str(c.get("title") or "").lower()]
    return {"items": [_channel_entry(c) for c in hits[:limit]]}


def channels_popular(config, params):
    """No real popularity signal in a static file -- every channel, in
    library order, which is an honest answer for an example."""
    return {"items": [_channel_entry(c) for c in _channels(config)]}


def channels_play(config, params):
    """Hands back a URL and which app to open it with. No stream/buffer path
    involved, unlike streams_lookup above -- the TV opens "package", or any
    app that handles the URL when "package" is blank."""
    params = params or {}
    channel = _find_channel(_channels(config), params.get("id"))
    video_id = params.get("video")
    match = next((v for v in (channel.get("videos") or []) if str(v.get("id")) == str(video_id)), None)
    if match is None:
        raise contract.ProviderError(
            contract.E_NOTFOUND, "No video %r on channel %r." % (video_id, channel["id"]))
    return {
        "url": "%s/channels/%s/%s.mp4" % (_base_url(config), channel["id"], match["id"]),
        "package": "",
        "label": "the example player",
    }
