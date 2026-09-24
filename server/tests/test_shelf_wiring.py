"""server.py's side of the shelf: the wiring, not the rules.

shelf.py's own behaviour (the pinning bands, the drop/mute table, the fade)
is test_shelf.py's subject. What is under test here is everything server.py
has to get right for those rules to ever see the truth:

- a TV heartbeat is the only thing that knows where a film got to, so
  playing -> paused has to leave a pinned title behind and playing -> ended
  has to mark a FILM watched, not just an episode;
- a TV that has been told to resume at start_s reports 0 for the beats
  before it seeks, and recording those would overwrite the very resume
  point the viewer just used;
- `t=` has to reach the play command as start_s, and autoplay-next must
  never carry one;
- decoration writes to COPIES -- the browse pool and the season cache are
  shared by every later request, and a shelf state baked into them would
  outlive the fact;
- the browser's beat is its only progress report;
- after a drop, the stop arriving right behind it must not record the
  position back again.

Run: python3 -m pytest tests/test_shelf_wiring.py -q
     python3 -m unittest discover -s server/tests -t server
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

IDLE_BX = dict(server._bx)   # the real idle shape, captured before any test
# touches it -- see the same line in test_ownership.py for why it is not a
# hand-written literal.


def _handler(path, body=None):
    """A bare H, no socket: _host_ok/_origin_ok/_body read headers and rfile
    and nothing else, exactly as test_host_guard.py drives _host_ok."""
    h = server.H.__new__(server.H)
    raw = json.dumps(body or {}).encode()
    h.headers = {"Host": "localhost", "Content-Length": str(len(raw))}
    h.path = path
    h.rfile = io.BytesIO(raw)
    h.close_connection = False
    sent = {}
    h._send = lambda code, b, ctype="application/json": sent.update(code=code, body=b)
    return h, sent


def get(path):
    h, sent = _handler(path)
    server.H.do_GET(h)
    return sent["code"], sent["body"]


def post(path, body):
    h, sent = _handler(path, body)
    server.H.do_POST(h)
    return sent["code"], sent["body"]


class ShelfWiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        shelf.init(os.path.join(self._tmp, "shelf.json"))
        server._jobs.clear()
        server._pool.clear()
        server._app = None
        server._app_cmd = None
        server._bx.update(IDLE_BX)
        self._ctr_pid = server._ctr_pid
        server._ctr_pid = lambda name: None      # no docker in here
        self._get_page = server.get_page
        self._app_fresh = server.app_fresh
        self._app_cmd = server.app_cmd

    def tearDown(self):
        server._ctr_pid = self._ctr_pid
        server.get_page = self._get_page
        server.app_fresh = self._app_fresh
        server.app_cmd = self._app_cmd
        server._bx.update(IDLE_BX)
        server._jobs.clear()
        server._pool.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def beat(self, **kw):
        d = {"id": "tv1", "name": "TV", "version": "1", "wait": 0}
        d.update(kw)
        return server.app_heartbeat(d)

    # -- the TV heartbeat ----------------------------------------------------

    def test_paused_midway_leaves_a_pinned_film(self):
        server.job_set("cinemeta:tt1", shelf_kind="movie", runtime_s=6000,
                       shelf_snap={"title": "A Film", "year": 1999,
                                   "poster": "p.jpg", "imdb_id": "tt1"})
        self.beat(state="playing", job="cinemeta:tt1", position_s=3000, duration_s=6000)
        self.beat(state="paused", job="cinemeta:tt1", position_s=3010, duration_s=6000)
        pins = shelf.pins("movie")
        self.assertEqual([p["id"] for p in pins], ["cinemeta:tt1"])
        self.assertEqual(pins[0]["title"], "A Film")
        self.assertEqual(pins[0]["poster"], "p.jpg")
        self.assertTrue(pins[0]["shelf"]["pinned"])
        self.assertEqual(pins[0]["shelf"]["resume_s"], 3005)

    def test_a_state_change_reaches_the_disk(self):
        """The pause is the moment worth a write; a run of "playing" is not."""
        server.job_set("cinemeta:tt1", shelf_kind="movie")
        self.beat(state="playing", job="cinemeta:tt1", position_s=3000, duration_s=6000)
        self.beat(state="paused", job="cinemeta:tt1", position_s=3000, duration_s=6000)
        with open(os.path.join(self._tmp, "shelf.json")) as f:
            self.assertIn("cinemeta:tt1", json.load(f)["titles"])

    def test_ended_marks_a_film_watched_even_with_no_position(self):
        server.job_set("cinemeta:tt2", shelf_kind="movie")
        self.beat(state="playing", job="cinemeta:tt2", position_s=100, duration_s=6000)
        self.beat(state="ended", job="cinemeta:tt2", duration_s=6000)
        v = shelf.view("cinemeta:tt2")
        self.assertTrue(v["watched"])
        self.assertFalse(v["pinned"])

    def test_the_tv_going_home_mid_film_leaves_a_resume_point(self):
        """What the TV app now sends when Home or standby cuts a film short:
        paused where it stands, then idle. It used to send "ended", which
        marked a film 20 minutes in as watched and threw its place away."""
        server.job_set("cinemeta:tt3", shelf_kind="movie", runtime_s=7200)
        self.beat(state="playing", job="cinemeta:tt3", position_s=1200, duration_s=7200)
        self.beat(state="paused", job="cinemeta:tt3", position_s=1200, duration_s=7200)
        self.beat(state="idle")
        v = shelf.view("cinemeta:tt3")
        self.assertFalse(v["watched"])
        self.assertEqual(v["resume_s"], 1200 - shelf.RESUME_BACK_S)

    def test_a_position_below_the_resume_point_is_ignored(self):
        """The TV reports where it is before it has seeked. 200s would be a
        perfectly recordable position for any other job."""
        server.job_set("cinemeta:tt3", shelf_kind="movie", start_s=3000.0)
        self.beat(state="playing", job="cinemeta:tt3", position_s=200, duration_s=6000)
        self.assertIsNone(shelf.view("cinemeta:tt3")["resume_s"])
        self.beat(state="playing", job="cinemeta:tt3", position_s=3100, duration_s=6000)
        self.assertEqual(shelf.view("cinemeta:tt3")["resume_s"], 3095)

    def test_a_job_with_no_offset_records_from_the_first_beat(self):
        server.job_set("cinemeta:tt4", shelf_kind="movie")
        self.beat(state="playing", job="cinemeta:tt4", position_s=200, duration_s=6000)
        self.assertEqual(shelf.view("cinemeta:tt4")["resume_s"], 195)

    # -- the resume offset ---------------------------------------------------

    def test_t_is_parsed_and_rubbish_is_not(self):
        self.assertEqual(server._start_s("t=90.5"), 90.5)
        self.assertEqual(server._start_s("t=-5"), 0.0)      # clamped, not refused
        self.assertIsNone(server._start_s("t=soon"))
        self.assertIsNone(server._start_s("t=nan"))
        self.assertIsNone(server._start_s(""))

    def test_t_lands_on_the_job_and_autoplay_carries_none(self):
        server.app_fresh = lambda: {"id": "tv1"}
        entry = {"pick": {"infoHash": "abc", "fileIdx": 0}, "title": "A Film",
                 "imdb_id": "tt7"}
        st, _ = server.start_play("cinemeta:tt7", lambda: (entry, 100),
                                  worker=lambda *a: None, start_s=42.0)
        self.assertEqual(st, 202)
        self.assertEqual(server.job_get("cinemeta:tt7").get("start_s"), 42.0)
        st, _ = server.start_play("cinemeta:tt8", lambda: (entry, 100),
                                  worker=lambda *a: None, autoplay=True)
        self.assertEqual(st, 202)
        self.assertIsNone(server.job_get("cinemeta:tt8").get("start_s"))
        # And the offset does not survive into a replay of the SAME title:
        # job_set merges, so the key has to be written even when empty.
        server.start_play("cinemeta:tt7", lambda: (entry, 100),
                          worker=lambda *a: None)
        self.assertIsNone(server.job_get("cinemeta:tt7").get("start_s"))

    def test_the_play_command_carries_start_s_only_when_there_is_one(self):
        cmds = []
        app = {"id": "tv1", "name": "TV", "job": "cinemeta:tt7", "acked": 1,
               "state": "playing"}
        server.app_fresh = lambda: app
        server.app_cmd = lambda kind, **f: (cmds.append((kind, f)), 1)[1]
        server.job_set("cinemeta:tt7", start_s=90.5)
        ok, _ = server.launch("http://x/1", "cinemeta:tt7", {}, "A Film", None)
        self.assertTrue(ok)
        self.assertEqual(cmds[-1][1].get("start_s"), 90.5)
        app["job"] = "tv:cinemeta:tt7:1:2"
        server.job_set("tv:cinemeta:tt7:1:2")
        server.launch("http://x/2", "tv:cinemeta:tt7:1:2", {}, "Ep", None)
        self.assertNotIn("start_s", cmds[-1][1])

    # -- decoration ----------------------------------------------------------

    def test_decoration_does_not_touch_the_cached_item(self):
        shelf.set_fav("cinemeta:tt5", True, snap={"title": "A Film"})
        cached = {"id": "cinemeta:tt5", "title": "A Film"}
        server.get_page = lambda *a, **k: ([cached], False, 1, 1, None)
        code, body = get("/api/movies?offset=0")
        self.assertEqual(code, 200)
        self.assertTrue(body["movies"][0]["shelf"]["fav"])
        self.assertNotIn("shelf", cached)

    def test_pinned_rides_the_first_page_only(self):
        server.job_set("cinemeta:tt6", shelf_kind="movie",
                       shelf_snap={"title": "A Film", "year": 1999,
                                   "poster": "p.jpg", "imdb_id": "tt6"})
        self.beat(state="playing", job="cinemeta:tt6", position_s=3000, duration_s=6000)
        server.get_page = lambda *a, **k: ([], False, 0, 0, None)
        _, first = get("/api/movies?offset=0")
        self.assertEqual([i["id"] for i in first["pinned"]], ["cinemeta:tt6"])
        _, later = get("/api/movies?offset=24")
        self.assertNotIn("pinned", later)

    def test_a_pinned_title_the_pool_still_has_comes_back_whole(self):
        rich = {"id": "cinemeta:tt6", "title": "A Film", "poster": "p.jpg",
                "stream": {"url": "http://x/1"}}
        server._pool["k"] = {"cands": [], "served": [rich], "buf": [],
                             "cursor": 0, "at": time.time()}
        server.job_set("cinemeta:tt6", shelf_kind="movie")
        self.beat(state="playing", job="cinemeta:tt6", position_s=3000, duration_s=6000)
        pinned = server.shelf_pins("movie")
        self.assertEqual(pinned[0]["stream"], {"url": "http://x/1"})
        self.assertNotIn("shelf", rich)

    def test_season_episodes_are_decorated_on_copies(self):
        cached = {"at": time.time(),
                  "episodes": [{"season": 1, "episode": 1, "name": "One"}]}
        server._tvseason.clear()
        server._tvseason["%s@%s:%s" % (
            server.gateway.cache_tag(server.contract.ROLE_METADATA),
            "cinemeta:tt9", 1)] = cached
        shelf.set_watched("cinemeta:tt9", True, s=1, e=1)
        code, body = get("/api/tv/cinemeta%3Att9/season/1")
        self.assertEqual(code, 200)
        self.assertTrue(body["episodes"][0]["watched"])
        self.assertEqual(body["episodes"][0]["name"], "One")
        self.assertNotIn("watched", cached["episodes"][0])

    # -- the browser's beat --------------------------------------------------

    def test_browser_beat_records_progress(self):
        server._bx.update(token="tok-1", gen=3, job="cinemeta:tta", dur=6000.0,
                          state="playing", pos=0.0, at=time.time())
        code, _ = post("/api/bx/beat", {"token": "tok-1", "gen": 3,
                                        "state": "playing", "pos": 3000})
        self.assertEqual(code, 200)
        self.assertEqual(shelf.view("cinemeta:tta")["resume_s"], 2995)
        post("/api/bx/beat", {"token": "tok-1", "gen": 3, "state": "ended",
                              "pos": 5999})
        self.assertTrue(shelf.view("cinemeta:tta")["watched"])

    def test_a_stale_browser_beat_records_nothing(self):
        server._bx.update(token="tok-1", gen=3, job="cinemeta:ttb", dur=6000.0,
                          state="playing", pos=0.0, at=time.time())
        code, _ = post("/api/bx/beat", {"token": "tok-1", "gen": 2,
                                        "state": "playing", "pos": 3000})
        self.assertEqual(code, 409)
        self.assertIsNone(shelf.view("cinemeta:ttb")["resume_s"])

    # -- the shelf routes ----------------------------------------------------

    def test_fav_round_trips_through_the_routes(self):
        code, body = post("/api/shelf/fav", {
            "id": "cinemeta:ttc", "on": True,
            "snap": {"kind": "movie", "title": "A Film", "year": 1999,
                     "poster": "p.jpg", "imdb_id": "ttc"}})
        self.assertEqual(code, 200)
        self.assertTrue(body["shelf"]["fav"])
        code, body = get("/api/shelf")
        self.assertEqual([i["id"] for i in body["items"]], ["cinemeta:ttc"])
        self.assertEqual(body["items"][0]["title"], "A Film")
        post("/api/shelf/fav", {"id": "cinemeta:ttc", "on": False})
        _, body = get("/api/shelf")
        self.assertEqual(body["items"], [])

    def test_the_write_routes_need_an_id(self):
        for path in ("/api/shelf/fav", "/api/shelf/watched", "/api/shelf/drop"):
            code, _ = post(path, {"on": True})
            self.assertEqual(code, 400, path)
        code, _ = post("/api/shelf/watched", {"id": "cinemeta:ttd", "on": True, "s": 1})
        self.assertEqual(code, 400)

    def test_watched_marks_one_episode_or_the_whole_title(self):
        post("/api/shelf/watched", {"id": "cinemeta:tte", "on": True, "s": 2, "e": 4})
        self.assertTrue(shelf.episode_view("cinemeta:tte", 2, 4)["watched"])
        code, body = post("/api/shelf/watched", {"id": "cinemeta:ttf", "on": True})
        self.assertEqual(code, 200)
        self.assertTrue(body["shelf"]["watched"])

    def test_drop_then_a_late_heartbeat_does_not_re_record(self):
        server.job_set("cinemeta:ttg", shelf_kind="movie")
        self.beat(state="playing", job="cinemeta:ttg", position_s=3000, duration_s=6000)
        self.assertTrue(shelf.view("cinemeta:ttg")["pinned"])
        code, _ = post("/api/shelf/drop", {"job": "cinemeta:ttg"})
        self.assertEqual(code, 200)
        self.assertFalse(shelf.view("cinemeta:ttg")["pinned"])
        self.beat(state="paused", job="cinemeta:ttg", position_s=3100, duration_s=6000)
        self.assertIsNone(shelf.view("cinemeta:ttg")["resume_s"])
        self.assertFalse(shelf.view("cinemeta:ttg")["pinned"])

    def test_a_new_play_after_a_drop_records_again(self):
        server.app_fresh = lambda: {"id": "tv1"}
        server.job_set("cinemeta:ttg", shelf_kind="movie")
        self.beat(state="playing", job="cinemeta:ttg", position_s=3000, duration_s=6000)
        post("/api/shelf/drop", {"job": "cinemeta:ttg"})
        server.start_play("cinemeta:ttg",
                          lambda: ({"pick": {"infoHash": "abc", "fileIdx": 0},
                                    "title": "A Film"}, 100),
                          worker=lambda *a: None)
        self.beat(state="playing", job="cinemeta:ttg", position_s=3100, duration_s=6000)
        self.assertTrue(shelf.view("cinemeta:ttg")["pinned"])


if __name__ == "__main__":
    unittest.main()
