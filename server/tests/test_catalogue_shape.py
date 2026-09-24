"""The walls, search and detail pages against a catalogue shaped like a real
one, rather than the stub whose ids happen to BE IMDb ids.

A real catalogue's browse and search results are thin: an id, a title,
artwork, genre ids and the catalogue's own rating. No IMDb id, no runtime --
those only come back from a details call. The provider split trusted those
thin entries as the identity for the stream lookup, and against a stream index
that only accepts IMDb ids every film came back "unsupported": an empty film
wall, a Play that failed, and a calibration with nothing to measure. Nothing
in the suite noticed, because the stub's entries already carried the id.

The clients were also written against the pre-provider tile: kind "tv" (not
the contract's "series" -- a series given "series" opened as a film), a flat
vote/votes, numeric genre ids, and an IMDb rating on every tile that has one.

Run: python3 -m pytest tests/test_catalogue_shape.py -q
"""

import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server  # noqa: E402
from providers import contract  # noqa: E402

CAT = "listcat"
HASH = "ab" * 20

# What the catalogue's browse/search hand back: thin.
FILM_LIST = [
    {"id": "101", "kind": "movie", "title": "First Film", "release_date": "2010-07-15",
     "genre_ids": ["28", "878"], "ratings": {"cat": {"value": 8.3, "votes": 40000}}},
    {"id": "102", "kind": "movie", "title": "Second Film", "release_date": "2008-07-18",
     "genre_ids": ["18"], "ratings": {"cat": {"value": 8.5, "votes": 30000}}},
]
SERIES_LIST = [
    {"id": "201", "kind": "series", "title": "A Series", "first_air": "2008-01-20",
     "genre_ids": ["18"], "ratings": {"cat": {"value": 8.9, "votes": 18000}}},
]
# What a details call adds: the IMDb id, the runtime, the IMDb rating.
DETAILS = {
    "101": {"runtime": 148, "imdb": "tt0000101", "imdb_rating": 8.8},
    "102": {"runtime": 152, "imdb": "tt0000102", "imdb_rating": 9.0},
    "201": {"runtime": 47, "imdb": "tt0000201", "imdb_rating": 9.5},
}


def _entry(raw):
    return contract.normalise_entry(raw, CAT)


def _local(qid):
    return qid.split(":", 1)[1] if ":" in qid else qid


class FakeGateway:
    """Stands in for providers.gateway: a thin-list catalogue plus a stream
    index that, like a real one, refuses anything without an IMDb id."""

    def __init__(self):
        self.details_calls = []
        self.identities = []

    def cache_tag(self, role):
        return "t"

    def supports_filters(self, role=contract.ROLE_CATALOGUE):
        return {"min_votes", "genre_ids", "exclude_genre_ids", "released_after"}

    def browse(self, kind, page=1, page_size=20, sort=None, filters=None):
        if page > 1:
            return {"entries": []}
        rows = SERIES_LIST if kind == contract.KIND_SERIES else FILM_LIST
        return {"entries": [_entry(r) for r in rows]}

    def search(self, kind, query, limit=20):
        return self.browse(kind)

    def details(self, qid, kind):
        self.details_calls.append(qid)
        local = _local(qid)
        d = DETAILS[local]
        base = next(r for r in FILM_LIST + SERIES_LIST if r["id"] == local)
        raw = dict(base, runtime=d["runtime"], external_ids={"imdb": d["imdb"]},
                   ratings=dict(base["ratings"], imdb={"value": d["imdb_rating"], "votes": 100000}))
        if kind == contract.KIND_SERIES:
            raw["seasons"] = [{"n": 1, "name": "Season 1", "episodes": 2}]
        return _entry(raw)

    def episodes(self, qid, season):
        return {"episodes": [
            contract.normalise_episode({"season": season, "episode": n, "name": "Ep %d" % n,
                                        "air": "2008-01-%02d" % (19 + n), "runtime": 47,
                                        "ratings": {"cat": {"value": 8.0 + n / 10, "votes": 300}}})
            for n in (1, 2)]}

    def streams(self, identity, season=None, episode=None):
        self.identities.append(identity)
        if not (identity.get("external_ids") or {}).get("imdb"):
            raise contract.ProviderError(contract.E_UNSUPPORTED, "only accepts IMDb ids")
        c, _ = contract.normalise_candidate({
            "transport": "torrent", "info_hash": HASH, "file_index": 0, "size_gb": 4.0,
            "seeders": 150, "codec": "HEVC", "quality": "4k", "is_4k": True,
            "display": "Title.2160p.BluRay.x265.DDP5.1.mkv"}, "idx")
        return {"candidates": [c], "rejected": {}}


class CatalogueShapeTest(unittest.TestCase):
    def setUp(self):
        self.gw = FakeGateway()
        self._saved = {n: getattr(server.gateway, n) for n in
                       ("cache_tag", "supports_filters", "browse", "search",
                        "details", "episodes", "streams")}
        for n in self._saved:
            setattr(server.gateway, n, getattr(self.gw, n))
        # The budget is loaded from this machine's netprofile.json, if any;
        # pin it so a 4 GB, 148-minute film fits here as it does anywhere.
        self._sustain = server.SUSTAIN_MBPS
        server.SUSTAIN_MBPS = 12.0
        for cache in (server._pool, server._streams, server._tvdet, server._tvseason):
            cache.clear()

    def tearDown(self):
        for n, fn in self._saved.items():
            setattr(server.gateway, n, fn)
        server.SUSTAIN_MBPS = self._sustain
        for cache in (server._pool, server._streams, server._tvdet, server._tvseason):
            cache.clear()

    # -- films ----------------------------------------------------------------
    def test_film_wall_is_not_empty_when_list_entries_have_no_ids(self):
        ms, _more, _cursor, pool, err = server.get_page(kind="movie", bias=False)
        self.assertIsNone(err)
        self.assertEqual(pool, 2)
        self.assertEqual(sorted(m["title"] for m in ms), ["First Film", "Second Film"])

    def test_stream_lookup_gets_the_imdb_id_and_runtime_from_details(self):
        server.get_page(kind="movie", bias=False)
        films = [i for i in self.gw.identities if i["kind"] == contract.KIND_MOVIE]
        self.assertEqual(sorted(i["external_ids"]["imdb"] for i in films),
                         ["tt0000101", "tt0000102"])
        self.assertTrue(all(i["runtime"] for i in films))

    def test_play_after_the_wall_finds_the_same_stream(self):
        server.get_page(kind="movie", bias=False)
        e = server.get_stream("%s:101" % CAT)
        self.assertIsNone(e["err"])
        self.assertEqual(e["pick"]["infoHash"], HASH)
        self.assertEqual(e["runtime"], 148)

    def test_entry_that_already_carries_ids_needs_no_details(self):
        rich = _entry(dict(FILM_LIST[0], runtime=148, external_ids={"imdb": "tt0000101"}))
        e = server.get_stream(rich["id"], entry=rich)
        self.assertIsNone(e["err"])
        self.assertEqual(self.gw.details_calls, [])

    def test_film_tile_has_the_fields_the_clients_read(self):
        ms, *_ = server.get_page(kind="movie", bias=False)
        m = next(x for x in ms if x["title"] == "First Film")
        self.assertEqual(m["kind"], "movie")
        self.assertEqual((m["vote"], m["votes"]), (8.3, 40000))
        self.assertEqual(m["genre_ids"], [28, 878])
        self.assertEqual(m["imdb"], {"rating": 8.8, "votes": 100000, "id": "tt0000101"})
        self.assertEqual(m["stream"]["pick"]["infoHash"], HASH)

    def test_wall_is_ranked_by_imdb_rating(self):
        ms, *_ = server.get_page(kind="movie", bias=False)
        self.assertEqual([m["title"] for m in ms], ["Second Film", "First Film"])

    def test_search_tiles_match_wall_tiles(self):
        ms, found, _ = server.search_movies("film", kind="movie")
        self.assertEqual(found, 2)
        m = next(x for x in ms if x["title"] == "First Film")
        self.assertEqual(m["kind"], "movie")
        self.assertEqual(m["vote"], 8.3)
        self.assertEqual(m["imdb"]["rating"], 8.8)

    def test_film_detail_carries_the_catalogue_rating(self):
        code, body = _get("/api/movie/%s:101" % CAT)
        self.assertEqual(code, 200)
        self.assertEqual((body["vote"], body["votes"]), (8.3, 40000))
        self.assertEqual(body["imdb_id"], "tt0000101")

    def test_biased_pool_merges_tiers_by_the_catalogues_rating(self):
        # Home tier, big tier and world tier each answer one title; the pool
        # holds two. A list entry has no IMDb rating, so the merge has to go by
        # the catalogue's own: sorting on the missing IMDb one scored all three
        # 0 + home bonus, and the home tier alone filled the pool.
        def tiered_browse(kind, page=1, page_size=20, sort=None, filters=None):
            filters = filters or {}
            if page > 1:
                return {"entries": []}
            if filters.get("origin_countries"):
                raw = {"id": "301", "title": "Home, Rated 7", "ratings": {"cat": {"value": 7.0}}}
            elif filters.get("original_language"):
                raw = {"id": "302", "title": "Big Tier, Rated 9", "ratings": {"cat": {"value": 9.0}}}
            else:
                raw = {"id": "303", "title": "World Tier, Rated 8", "ratings": {"cat": {"value": 8.0}}}
            return {"entries": [_entry(dict(raw, kind="movie"))]}

        server.gateway.browse = tiered_browse
        server.gateway.supports_filters = lambda role=None: {
            "min_votes", "origin_countries", "original_language"}
        saved = server.POOL_MAX, server.HOME_COUNTRIES, server.BIAS_LANG, server.WORLD_MIN_VOTES
        server.POOL_MAX, server.HOME_COUNTRIES, server.BIAS_LANG, server.WORLD_MIN_VOTES = \
            2, ["GB"], "en", 10000
        try:
            pool = server.build_pool([], "top", [], "movie", True, "k")
        finally:
            server.POOL_MAX, server.HOME_COUNTRIES, server.BIAS_LANG, server.WORLD_MIN_VOTES = saved
        self.assertEqual([c["title"] for c in pool], ["Big Tier, Rated 9", "World Tier, Rated 8"])

    def test_a_failing_tier_is_remembered_and_the_rest_still_build(self):
        # The home tier times out; the others answer. The error used to be
        # caught `as ex` -- the excluded-genres argument's name -- which Python
        # deletes at the end of the block, so the next tier's `if ex:` raised
        # UnboundLocalError and the whole wall 500ed.
        def flaky_browse(kind, page=1, page_size=20, sort=None, filters=None):
            filters = filters or {}
            if filters.get("origin_countries"):
                raise contract.ProviderError(contract.E_TIMEOUT, "home tier timed out")
            if page > 1:
                return {"entries": []}
            return {"entries": [_entry({"id": "302", "title": "Still Here", "kind": "movie",
                                         "ratings": {"cat": {"value": 9.0}}})]}

        server.gateway.browse = flaky_browse
        server.gateway.supports_filters = lambda role=None: {
            "min_votes", "origin_countries", "original_language"}
        saved = server.HOME_COUNTRIES, server.BIAS_LANG
        server.HOME_COUNTRIES, server.BIAS_LANG = ["GB"], "en"
        errs = []
        try:
            pool = server.build_pool([], "top", [], "movie", True, "k", errs=errs)
        finally:
            server.HOME_COUNTRIES, server.BIAS_LANG = saved
        self.assertEqual([c["title"] for c in pool], ["Still Here"])
        self.assertEqual([e.code for e in errs], [contract.E_TIMEOUT])

    # -- series ---------------------------------------------------------------
    def test_series_tile_says_tv(self):
        # The TV app and the web page both open the seasons-and-episodes page
        # on kind == "tv"; "series" opened every series as a film.
        ms, *_ = server.get_page(kind="tv", bias=False)
        self.assertEqual([m["kind"] for m in ms], ["tv"])
        self.assertEqual(ms[0]["vote"], 8.9)
        self.assertEqual(ms[0]["imdb"]["rating"], 9.5)

    def test_series_search_tile_says_tv(self):
        ms, _, _ = server.search_movies("series", kind="tv")
        self.assertEqual([m["kind"] for m in ms], ["tv"])

    def test_series_page_and_episode_rows_carry_ratings(self):
        d = server.tv_detail("%s:201" % CAT)
        self.assertEqual(d["kind"], "tv")
        self.assertEqual((d["vote"], d["votes"]), (8.9, 18000))
        eps = server.tv_season("%s:201" % CAT, 1)["episodes"]
        self.assertEqual([e["vote"] for e in eps], [8.1, 8.2])


def _get(path):
    h = server.H.__new__(server.H)
    h.headers = {"Host": "localhost", "Content-Length": "0"}
    h.path = path
    h.rfile = io.BytesIO(b"")
    h.close_connection = False
    sent = {}
    h._send = lambda code, b, ctype="application/json": sent.update(code=code, body=b)
    server.H.do_GET(h)
    body = sent["body"]
    return sent["code"], json.loads(body) if isinstance(body, (bytes, str)) else body


if __name__ == "__main__":
    unittest.main()
