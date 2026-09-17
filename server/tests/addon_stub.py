"""In-process Stremio-protocol add-on for the provider test suite.

server/providers/addon.py is the PRIMARY way providers get added: an admin
pastes a manifest.json URL and the adapter speaks the Stremio HTTP protocol
from there. Testing that adapter against a real add-on would make the suite
flaky (network, add-on downtime, rate limits) and non-deterministic (real
catalogs and metadata drift over time). StubAddon serves the identical wire
protocol from a background thread on loopback, so a test gets the same
answer every run and can start/stop the server between assertions.

Deliberately does not import server/providers/contract.py: this module plays
the add-on's role, speaking raw Stremio JSON on the wire. Reshaping that
into contract shapes is addon.py's job -- importing contract types here
would blur which side of the boundary a test failure is on.
"""

import hashlib
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE_SIZE = 20
GENRES = ("Action", "Drama", "Comedy")
SEASONS = 2
EPISODES_PER_SEASON = 3
# "A few hundred KB" per the spec -- enough to exercise Range math on more
# than one chunk without slowing the suite down.
MEDIA_SIZE = 300 * 1024
# Deterministic, not random: a test asserting a byte-for-byte Range slice
# must see the same bytes on every run, including across processes.
_MEDIA = bytes((i * 7 + 3) % 256 for i in range(MEDIA_SIZE))


def _title_for(kind, index):
    return "Stub %s %d" % (kind.capitalize(), index + 1)


def _id_for(index):
    # tt9000001, tt9000002, ... -- the 9000000 offset keeps these unmistakably
    # synthetic (no real IMDb title will ever collide) while still matching
    # contract.RE_IMDB (tt + >=6 digits), which is what idPrefixes:["tt"]
    # commits this add-on to serving.
    return "tt%07d" % (9000001 + index)


def _index_from_id(base_id):
    try:
        return int(base_id[2:]) - 9000001
    except ValueError:
        return 0


def _genre_for(index):
    return GENRES[index % len(GENRES)]


def _strip_json(segment):
    return segment[:-5] if segment.endswith(".json") else segment


def _parse_range(value, total):
    """('bytes=a-b' | 'bytes=a-' | 'bytes=-n') -> (start, end) inclusive, or
    (None, None) if the range cannot be satisfied -- callers must 416."""
    m = re.match(r"^bytes=(\d*)-(\d*)$", value.strip())
    if not m or not (m.group(1) or m.group(2)):
        return None, None
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else total - 1
    else:
        length = int(m.group(2))
        start = max(0, total - length)
        end = total - 1
    end = min(end, total - 1)
    if start > end or start >= total:
        return None, None
    return start, end


class StubAddon:
    """A deterministic Stremio add-on, in-process, for one test.

    One class covers every scenario the suite needs rather than a fixture
    per behaviour, because the behaviours interact (e.g. require_header only
    means something once transport="http" produces a /media/ url) and a
    matrix of subclasses would drift out of sync with each other over time.
    """

    def __init__(self, resources=("catalog", "meta", "stream"), transport="torrent",
                 require_header=None, fail_with=None, delay=0.0, catalog_size=40):
        self.resources = tuple(resources)
        self.transport = transport
        self.require_header = require_header
        self.fail_with = fail_with
        self.delay = delay
        self.catalog_size = catalog_size
        self.requests = []
        self.headers_seen = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        addon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass  # a chatty handler on stdout drowns the suite's own PASS/FAIL lines

            def do_GET(self):
                addon._dispatch(self)

        # Port 0 -> the OS assigns one, so parallel tests never fight over a
        # fixed port. Threading (not the plain HTTPServer) matters because a
        # test that opens the manifest and, mid-request, a /media/ range GET
        # on the same server would otherwise deadlock on a single-threaded one.
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    @property
    def base_url(self):
        _, port = self._server.server_address
        return "http://127.0.0.1:%d" % port

    @property
    def manifest_url(self):
        return self.base_url + "/manifest.json"

    # -- request log -----------------------------------------------------------
    def _record(self, handler):
        with self._lock:
            self.requests.append(handler.path)
            self.headers_seen.append(dict(handler.headers.items()))

    # -- dispatch ---------------------------------------------------------------
    def _dispatch(self, handler):
        self._record(handler)
        path = urllib.parse.urlsplit(handler.path).path
        segments = [s for s in path.split("/") if s]
        if segments == ["manifest.json"]:
            self._serve_manifest(handler)
        elif len(segments) == 2 and segments[0] == "media":
            self._serve_media(handler, segments[1])
        elif segments and segments[0] in ("catalog", "meta", "stream"):
            self._serve_resource(handler, segments)
        else:
            self._send_json(handler, 404, {"error": "not found"})

    def _serve_resource(self, handler, segments):
        resource = segments[0]
        empty = {"catalog": {"metas": []}, "meta": {"meta": {}}, "stream": {"streams": []}}[resource]
        if self._apply_fail_modes(handler, empty):
            return
        if resource == "catalog":
            self._serve_catalog(handler, segments)
        elif resource == "meta":
            self._serve_meta(handler, segments)
        else:
            self._serve_stream(handler, segments)

    def _apply_fail_modes(self, handler, empty_response):
        """Sleep/short-circuit per fail_with before a real response is built.
        Returns True if the request is already fully handled."""
        if self.delay:
            time.sleep(self.delay)
        if self.fail_with == "timeout":
            # Never respond. A client with any sane deadline (Cinematica's
            # add-on adapter uses 15s) has already given up long before this
            # returns -- the point is to prove the caller enforces its own
            # timeout rather than hanging on a stuck add-on forever.
            time.sleep(30)
            return True
        if self.fail_with == "500":
            self._send_raw(handler, 500, b"internal error", "text/plain")
            return True
        if self.fail_with == "garbage":
            # Valid HTTP, invalid JSON -- proves the caller's json.loads()
            # failure is surfaced as a named protocol error, not a crash.
            self._send_raw(handler, 200, b"not json {", "text/plain")
            return True
        if self.fail_with == "empty":
            self._send_json(handler, 200, empty_response)
            return True
        return False

    # -- manifest -----------------------------------------------------------------
    def _serve_manifest(self, handler):
        # "empty" has no meaning for a manifest (there is no result list to
        # empty), so only the transport-level failure modes apply here.
        if self.delay:
            time.sleep(self.delay)
        if self.fail_with == "timeout":
            time.sleep(30)
            return
        if self.fail_with == "500":
            self._send_raw(handler, 500, b"internal error", "text/plain")
            return
        if self.fail_with == "garbage":
            self._send_raw(handler, 200, b"not json {", "text/plain")
            return

        extra = [{"name": "genre", "options": list(GENRES)}, {"name": "search"}, {"name": "skip"}]
        catalogs = [{"type": kind, "id": "stub-%ss" % kind, "name": "Stub %ss" % kind.capitalize(),
                     "extra": extra} for kind in ("movie", "series")]
        manifest = {
            "id": "org.cinematica.stub",
            "version": "1.0.0",
            "name": "Cinematica Stub Addon",
            "description": "Deterministic in-process Stremio add-on for tests.",
            "resources": list(self.resources),
            "types": ["movie", "series"],
            "idPrefixes": ["tt"],
            "catalogs": catalogs,
        }
        self._send_json(handler, 200, manifest)

    # -- catalog ------------------------------------------------------------------
    def _serve_catalog(self, handler, segments):
        if len(segments) == 3:
            kind, extra = segments[1], {}
        elif len(segments) == 4:
            # Stremio embeds `extra` as a literal querystring-shaped PATH
            # segment ("skip=20&genre=Action"), not a real query string --
            # parse_qsl on the segment (after the .json it always carries)
            # is exactly the inverse of the urlencode() the adapter used.
            kind, extra = segments[1], dict(urllib.parse.parse_qsl(_strip_json(segments[3])))
        else:
            self._send_json(handler, 404, {"error": "bad catalog path"})
            return

        metas = [
            {"id": _id_for(i), "type": kind, "name": _title_for(kind, i),
             "genres": [_genre_for(i)], "releaseInfo": str(2000 + i % 24),
             "description": "Synthetic %s number %d for tests." % (kind, i + 1)}
            for i in range(self.catalog_size)
        ]
        search = extra.get("search")
        if search:
            metas = [m for m in metas if search.lower() in m["name"].lower()]
        genre = extra.get("genre")
        if genre:
            metas = [m for m in metas if genre in m["genres"]]
        skip = int(extra.get("skip") or 0)
        self._send_json(handler, 200, {"metas": metas[skip: skip + PAGE_SIZE]})

    # -- meta ---------------------------------------------------------------------
    def _serve_meta(self, handler, segments):
        if len(segments) != 3:
            self._send_json(handler, 404, {"error": "bad meta path"})
            return
        kind, base_id = segments[1], _strip_json(segments[2])
        index = _index_from_id(base_id)
        meta = {
            "id": base_id, "type": kind, "name": _title_for(kind, index),
            "genres": [_genre_for(index)], "releaseInfo": str(2000 + index % 24),
            "description": "Synthetic %s number %d for tests." % (kind, index + 1),
            "runtime": "120 min",
        }
        if kind == "series":
            # Stremio has no season object of its own -- season/episode
            # metadata only ever exists implicitly in which pairs appear in
            # `videos` -- so this is the one place season structure is built.
            meta["videos"] = [
                {"id": "%s:%d:%d" % (base_id, season, ep), "season": season, "episode": ep,
                 "title": "S%02dE%02d" % (season, ep),
                 "released": "2020-%02d-%02dT00:00:00.000Z" % (season, ep),
                 "thumbnail": self.base_url + "/img/%s-s%de%d.jpg" % (base_id, season, ep)}
                for season in range(1, SEASONS + 1) for ep in range(1, EPISODES_PER_SEASON + 1)
            ]
        self._send_json(handler, 200, {"meta": meta})

    # -- stream -------------------------------------------------------------------
    def _serve_stream(self, handler, segments):
        if len(segments) != 3:
            self._send_json(handler, 404, {"error": "bad stream path"})
            return
        kind, stream_id = segments[1], _strip_json(segments[2])
        parts = stream_id.split(":")
        base_id = parts[0]
        season = episode = None
        if kind == "series" and len(parts) == 3:
            season, episode = int(parts[1]), int(parts[2])

        label = ("Stub S%02dE%02d" % (season, episode) if season is not None
                 else "Stub %s" % _title_for(kind, _index_from_id(base_id)))
        streams = [
            self._real_stream(label, stream_id),
            # Deliberately unsupported so a test can assert it is refused by
            # name and counted (see addon._stream_to_candidate), not silently
            # dropped -- a provider mixing playable and unplayable rows must
            # cost only the unplayable rows.
            {"name": "Stub Unsupported", "ytId": "abc"},
        ]
        self._send_json(handler, 200, {"streams": streams})

    def _real_stream(self, label, stream_id):
        if self.transport == "torrent":
            return {
                "name": label,
                # "|"-delimited, unlike the multi-line/emoji
                # layout -- the point is to prove the adapter reads
                # structured fields (infoHash/fileIdx), not one service's
                # free-text size convention.
                "title": "%s | 1080p | 2.4 GB" % label,
                "infoHash": hashlib.sha1(stream_id.encode("utf-8")).hexdigest(),
                "fileIdx": 0,
            }
        slug = stream_id.replace(":", "-")
        behavior_hints = {"videoSize": MEDIA_SIZE, "filename": "%s.mp4" % slug}
        if self.require_header:
            # Rides the candidate only as far as the /src/ proxy that injects
            # it (see contract.T_HTTP) -- carrying it here is what lets a
            # test prove the header survives that hop rather than leaking
            # into, or being dropped before, the actual media request.
            name, value = self.require_header
            behavior_hints["proxyHeaders"] = {"request": {name: value}}
        return {"name": label, "title": "%s | 1080p" % label,
                "url": self.base_url + "/media/%s.mp4" % slug, "behaviorHints": behavior_hints}

    # -- media --------------------------------------------------------------------
    def _serve_media(self, handler, name):
        if self.require_header:
            hname, hvalue = self.require_header
            if handler.headers.get(hname) != hvalue:
                self._send_raw(handler, 401, b"missing or wrong credential", "text/plain")
                return

        total = len(_MEDIA)
        range_header = handler.headers.get("Range")
        if not range_header:
            self._send_raw(handler, 200, _MEDIA, "video/mp4", {"Accept-Ranges": "bytes"})
            return

        start, end = _parse_range(range_header, total)
        if start is None:
            self._send_raw(handler, 416, b"", "video/mp4",
                            {"Accept-Ranges": "bytes", "Content-Range": "bytes */%d" % total})
            return
        headers = {"Accept-Ranges": "bytes", "Content-Range": "bytes %d-%d/%d" % (start, end, total)}
        self._send_raw(handler, 206, _MEDIA[start: end + 1], "video/mp4", headers)

    # -- wire helpers ---------------------------------------------------------------
    def _send_json(self, handler, status, obj):
        self._send_raw(handler, status, json.dumps(obj).encode("utf-8"), "application/json")

    def _send_raw(self, handler, status, body, content_type, extra_headers=None):
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        if body:
            handler.wfile.write(body)
