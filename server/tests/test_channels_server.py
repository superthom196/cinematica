"""Channels wiring: server.py's routes and shelf.py's own channel state.

No provider process here -- gateway.channel_* is monkeypatched directly, the
same trade-off test_shelf_wiring.py makes for the catalogue side. What is
under test:

- shelf.py: follow/seen clear and set the NEW count correctly, the wall
  order (new first, then latest upload), the 500-cap on "opened", a shelf
  file written before "channels" existed still loads, and a channel record
  never leaks into favourites()/pins()/view() -- those only ever look at
  _state["titles"].
- server.py: the /api/movies channel branch (wall + popular), /api/channel,
  /api/channel/videos, /api/search/stream's channel branch (resolve vs.
  search), and the four POST routes (follow/seen/opened/play).

Run: python3 -m unittest tests.test_channels_server
     python3 -m unittest discover -s tests -t .
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server  # noqa: E402
import shelf   # noqa: E402
from providers import contract  # noqa: E402


def _handler(path, body=None):
    """A bare H, no socket -- exactly test_shelf_wiring.py's _handler, plus
    a real BytesIO wfile so an SSE route's self.wfile.write() lands
    somewhere readable instead of raising AttributeError."""
    h = server.H.__new__(server.H)
    raw = json.dumps(body or {}).encode()
    h.headers = {"Host": "localhost", "Content-Length": str(len(raw))}
    h.path = path
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h.close_connection = False
    sent = {}
    h._send = lambda code, b, ctype="application/json": sent.update(code=code, body=b)
    h.send_response = lambda code: sent.setdefault("status", code)
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    return h, sent


def get(path):
    h, sent = _handler(path)
    server.H.do_GET(h)
    return sent["code"], sent["body"]


def get_sse(path):
    h, sent = _handler(path)
    server.H.do_GET(h)
    raw = h.wfile.getvalue().decode("utf-8")
    events = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        ev = lines[0][len("event: "):]
        data = json.loads(lines[1][len("data: "):])
        events.append((ev, data))
    return events


def post(path, body):
    h, sent = _handler(path, body)
    server.H.do_POST(h)
    return sent["code"], sent["body"]


def _channel(cid, title, **kw):
    ch = {"id": cid, "local_id": cid.split(":", 1)[-1], "kind": "channel",
          "title": title, "avatar": "", "banner": "", "subscribers": 0,
          "description": "", "latest_at": None}
    ch.update(kw)
    return ch


def _video(vid, title, published):
    return {"id": vid, "title": title, "published": published, "duration_s": 0,
            "thumb": "", "description": "", "views": 0}


class ChannelsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        shelf.init(os.path.join(self._tmp, "shelf.json"))
        # Every gateway.channel_* call server.py can make, stubbed so no
        # test here ever needs a real provider process.
        self._orig = {}
        for name in ("available", "channel_latest", "channel_details",
                      "channel_popular", "channel_search", "channel_resolve",
                      "channel_play", "channel_ops", "cache_tag"):
            self._orig[name] = getattr(server.gateway, name)
        server.gateway.available = lambda role: True
        server.gateway.channel_ops = lambda: {contract.OP_CH_VIDEOS, contract.OP_CH_SEARCH,
                                              contract.OP_CH_POPULAR}
        server.gateway.cache_tag = lambda role: "prov@1"
        server.gateway.channel_latest = lambda cid: {"videos": [], "next": None}
        server.gateway.channel_details = lambda cid: _channel(cid, "Channel " + cid)
        server.gateway.channel_popular = lambda limit=40: {"items": []}
        server.gateway.channel_search = lambda q, limit=20: {"items": []}
        server.gateway.channel_resolve = lambda q: _channel("prov:resolved", "Resolved")
        server.gateway.channel_play = lambda cid, vid: {"url": "http://x/y", "package": "",
                                                         "label": "another app"}
        server._channel_popular_cache.update(at=0, data=[], tag=None)
        server._channel_details_cache.clear()

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(server.gateway, name, fn)
        shutil.rmtree(self._tmp, ignore_errors=True)


# =============================================================================
# shelf.py unit tests
# =============================================================================
class ShelfChannelsTest(ChannelsTestBase):
    def test_new_count_only_videos_after_the_follow(self):
        follow_at = 1000
        shelf.follow("prov:a", True, snap={"title": "A"}, now=follow_at)
        shelf.set_latest("prov:a", [
            _video("v1", "old", follow_at - 100),
            _video("v2", "new", follow_at + 100),
        ], now=follow_at + 200)
        self.assertEqual(shelf.channel_view("prov:a")["new"], 1)

    def test_seen_clears_new(self):
        shelf.follow("prov:a", True, now=1000)
        shelf.set_latest("prov:a", [_video("v1", "new", 1100)], now=1200)
        self.assertEqual(shelf.channel_view("prov:a")["new"], 1)
        shelf.channel_seen("prov:a", now=1300)
        self.assertEqual(shelf.channel_view("prov:a")["new"], 0)
        # And a video published after the new "seen" is new again.
        shelf.set_latest("prov:a", [_video("v1", "new", 1100), _video("v2", "newer", 1400)],
                          now=1500)
        self.assertEqual(shelf.channel_view("prov:a")["new"], 1)

    def test_unfollowed_channel_has_no_new_count(self):
        shelf.set_latest("prov:a", [_video("v1", "x", 9999999999)], now=1)
        self.assertEqual(shelf.channel_view("prov:a")["new"], 0)
        self.assertFalse(shelf.channel_view("prov:a")["followed"])

    def test_wall_order_new_first_then_latest_upload(self):
        # b: followed, no new, latest_at=500
        shelf.follow("prov:b", True, now=100)
        shelf.set_latest("prov:b", [_video("v", "x", 400)], now=600)
        shelf.channel_seen("prov:b", now=900)
        # a: followed, has new, latest_at=2000 (newest of the "new" group)
        shelf.follow("prov:a", True, now=100)
        shelf.set_latest("prov:a", [_video("v", "x", 2000)], now=2100)
        # c: followed, has new too, latest_at=1500 (older than a, still new group)
        shelf.follow("prov:c", True, now=100)
        shelf.set_latest("prov:c", [_video("v", "x", 1500)], now=1600)
        # d: followed, no new, latest_at=50 (older than b, so last within the no-new group)
        shelf.follow("prov:d", True, now=100)
        shelf.set_latest("prov:d", [_video("v", "x", 50)], now=1000)
        shelf.channel_seen("prov:d", now=2000)
        ids = [it["id"] for it in shelf.followed_channels()]
        self.assertEqual(ids, ["prov:a", "prov:c", "prov:b", "prov:d"])

    def test_decorate_videos_marks_opened_and_new_only_when_followed(self):
        shelf.follow("prov:a", True, now=1000)
        shelf.video_opened("prov:a", "v1", True, now=1100)
        vids = [_video("v1", "old", 900), _video("v2", "new", 1200)]
        out = shelf.decorate_videos("prov:a", vids)
        self.assertTrue(out[0]["opened"])
        self.assertFalse(out[0]["new"])
        self.assertFalse(out[1]["opened"])
        self.assertTrue(out[1]["new"])
        # Unfollowed: never "new", regardless of publish date.
        shelf.follow("prov:a", False)
        out2 = shelf.decorate_videos("prov:a", vids)
        self.assertFalse(out2[1]["new"])

    def test_opened_capped_at_500(self):
        for i in range(520):
            shelf.video_opened("prov:a", "v%d" % i, True, now=i)
        with shelf._lock:
            opened = shelf._state["channels"]["prov:a"]["opened"]
        self.assertEqual(len(opened), 500)
        # The oldest 20 were dropped, the newest 500 kept.
        self.assertNotIn("v0", opened)
        self.assertIn("v519", opened)

    def test_shelf_file_without_channels_key_loads(self):
        path = os.path.join(self._tmp, "no_channels.json")
        with open(path, "w") as f:
            json.dump({"v": 1, "titles": {"cinemeta:tt1": {"fav": 1}}}, f)
        shelf.init(path)
        self.assertEqual(shelf.followed_ids(), [])
        shelf.follow("prov:a", True)
        self.assertEqual(shelf.followed_ids(), ["prov:a"])

    def test_channels_never_appear_as_titles(self):
        shelf.follow("prov:a", True, snap={"title": "A"}, now=1)
        shelf.set_fav("cinemeta:tt1", True, snap={"title": "Real Film", "kind": "movie"})
        self.assertEqual([f["id"] for f in shelf.favourites()], ["cinemeta:tt1"])
        self.assertEqual([p["id"] for p in shelf.pins("movie")], [])
        with shelf._lock:
            self.assertNotIn("prov:a", shelf._state["titles"])


# =============================================================================
# server.py wiring
# =============================================================================
class ServerChannelsTest(ChannelsTestBase):
    def test_wall_lists_followed_then_popular_excluding_followed(self):
        shelf.follow("prov:a", True, snap={"title": "A"}, now=100)
        server.gateway.channel_popular = lambda limit=40: {
            "items": [_channel("prov:a", "A"), _channel("prov:pop", "Pop")]}
        code, body = get("/api/movies?kind=channel")
        self.assertEqual(code, 200)
        self.assertEqual([m["id"] for m in body["movies"]], ["prov:a"])
        self.assertEqual([p["id"] for p in body["popular"]], ["prov:pop"])
        self.assertIn(contract.OP_CH_VIDEOS, body["channel_ops"])

    def test_popular_omitted_when_channel_ops_lacks_it(self):
        server.gateway.channel_ops = lambda: {contract.OP_CH_VIDEOS}
        called = []
        server.gateway.channel_popular = lambda limit=40: called.append(1) or {"items": []}
        code, body = get("/api/movies?kind=channel")
        self.assertEqual(body["popular"], [])
        self.assertEqual(called, [])   # never even asked

    def test_channels_role_unavailable(self):
        server.gateway.available = lambda role: False
        code, body = get("/api/movies?kind=channel")
        self.assertEqual(code, 200)
        self.assertEqual(body["movies"], [])
        self.assertEqual(body["popular"], [])
        self.assertEqual(body["err"], "channels are not set up")

    def test_search_stream_routes_handle_and_url_to_resolve(self):
        for term in ("@somechannel", "https://example.com/c/somechannel"):
            events = get_sse("/api/search/stream?kind=channel&q=" + term)
            kinds = [e for e, _ in events]
            self.assertEqual(kinds, ["movie", "done"])
            self.assertEqual(events[0][1]["id"], "prov:resolved")

    def test_search_stream_routes_plain_words_to_search(self):
        server.gateway.channel_search = lambda q, limit=20: {
            "items": [_channel("prov:x", "X"), _channel("prov:y", "Y")]}
        events = get_sse("/api/search/stream?kind=channel&q=nature")
        movies = [d for e, d in events if e == "movie"]
        self.assertEqual([m["id"] for m in movies], ["prov:x", "prov:y"])
        done = [d for e, d in events if e == "done"][0]
        self.assertEqual(done["found"], 2)

    def test_search_stream_falls_back_to_resolve_without_search_op(self):
        server.gateway.channel_ops = lambda: {contract.OP_CH_VIDEOS}
        events = get_sse("/api/search/stream?kind=channel&q=nature")
        self.assertEqual(events[0][0], "movie")
        self.assertEqual(events[0][1]["id"], "prov:resolved")

    def test_search_stream_provider_error_emits_fail(self):
        def boom(q):
            raise contract.ProviderError(contract.E_UPSTREAM, "no such channel")
        server.gateway.channel_resolve = boom
        events = get_sse("/api/search/stream?kind=channel&q=@nope")
        self.assertEqual(events, [("fail", {"err": "no such channel"})])

    def test_follow_then_play_records_opened_and_returns_play(self):
        code, body = post("/api/channel/follow", {"id": "prov:a", "on": True})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["channel"]["id"], "prov:a")
        self.assertTrue(shelf.channel_view("prov:a")["followed"])
        code, body = post("/api/channel/play", {"id": "prov:a", "video": "v1"})
        self.assertEqual(code, 200)
        self.assertEqual(body["play"]["url"], "http://x/y")
        vids = shelf.decorate_videos("prov:a", [_video("v1", "x", 1)])
        self.assertTrue(vids[0]["opened"])

    def test_play_provider_error_is_502(self):
        def boom(cid, vid):
            raise contract.ProviderError(contract.E_UPSTREAM, "gone")
        server.gateway.channel_play = boom
        code, body = post("/api/channel/play", {"id": "prov:a", "video": "v1"})
        self.assertEqual(code, 502)
        self.assertEqual(body, {"ok": False, "msg": "gone"})

    def test_play_missing_fields_is_400(self):
        code, body = post("/api/channel/play", {"id": "prov:a"})
        self.assertEqual(code, 400)
        code, body = post("/api/channel/follow", {"on": True})
        self.assertEqual(code, 400)

    def test_seen_route_clears_new(self):
        shelf.follow("prov:a", True, now=100)
        shelf.set_latest("prov:a", [_video("v1", "x", 500)], now=600)
        self.assertEqual(shelf.channel_view("prov:a")["new"], 1)
        code, body = post("/api/channel/seen", {"id": "prov:a"})
        self.assertEqual(code, 200)
        self.assertEqual(shelf.channel_view("prov:a")["new"], 0)

    def test_opened_route_toggles(self):
        code, body = post("/api/channel/opened", {"id": "prov:a", "video": "v1", "on": True})
        self.assertEqual(code, 200)
        self.assertTrue(shelf.decorate_videos("prov:a", [_video("v1", "x", 1)])[0]["opened"])
        post("/api/channel/opened", {"id": "prov:a", "video": "v1", "on": False})
        self.assertFalse(shelf.decorate_videos("prov:a", [_video("v1", "x", 1)])[0]["opened"])

    def test_channel_detail_route_for_followed_and_unfollowed(self):
        shelf.follow("prov:a", True, snap={"title": "A"}, now=1)
        code, body = get("/api/channel?id=prov:a")
        self.assertEqual(code, 200)
        self.assertEqual(body["title"], "A")
        self.assertTrue(body["followed"])
        code, body = get("/api/channel?id=prov:unfollowed")
        self.assertEqual(code, 200)
        self.assertEqual(body["title"], "Channel prov:unfollowed")
        self.assertFalse(body["followed"])

    def test_channel_videos_first_page_uses_stored_when_fresh(self):
        shelf.follow("prov:a", True, now=1)
        shelf.set_latest("prov:a", [_video("v1", "x", 5)], now=time.time())
        called = []
        server.gateway.channel_latest = lambda cid: called.append(1) or {"videos": [], "next": None}
        code, body = get("/api/channel/videos?id=prov:a")
        self.assertEqual(code, 200)
        self.assertEqual([v["id"] for v in body["videos"]], ["v1"])
        self.assertEqual(called, [])       # served from the shelf, not fetched
        self.assertEqual(body["next"], "")  # channels.videos is available

    def test_channel_videos_first_page_refetches_when_stale(self):
        shelf.follow("prov:a", True, now=1)
        shelf.set_latest("prov:a", [_video("v1", "old", 5)],
                          now=time.time() - server.CHANNEL_POLL_MIN * 60 - 10)
        server.gateway.channel_latest = lambda cid: {"videos": [_video("v2", "fresh", 9)],
                                                      "next": None}
        code, body = get("/api/channel/videos?id=prov:a")
        self.assertEqual([v["id"] for v in body["videos"]], ["v2"])

    def test_channel_videos_paging_needs_the_op(self):
        server.gateway.channel_ops = lambda: set()
        code, body = get("/api/channel/videos?id=prov:a&page=tok")
        self.assertEqual(body, {"videos": [], "next": None})


if __name__ == "__main__":
    unittest.main()
