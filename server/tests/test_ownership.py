"""Arbitration between the TV and a browser tab for the one player: which
device gets to claim it, and when a stale claim must not go on blocking a
device that actually wants it.

Rules under test (see claim_owner()'s docstring for the full table):
- TV supersedes TV, same as it always has;
- TV is refused by a live browser session, and vice versa for a browser
  request against an active TV job or a TV already on screen;
- a browser reclaims its own session (a different film) but not somebody
  else's;
- browser_playing() survives a pause -- that is the case that stops
  cache_watch() emptying the cache under a film still being watched -- but
  not a heartbeat that has gone stale, and not a session that has ended.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server  # noqa: E402


IDLE_BX = dict(server._bx)   # the real idle shape, captured before any test
# touches it -- NOT a hand-copied literal. A literal here is a second
# definition of _bx's shape that nothing keeps in step with the first: when
# run_anchor was added to the server, every test that rebound server._bx to
# such a literal handed the segment route a dict with that key missing, and
# three tests started failing with a 500 that had nothing to do with what
# they were testing.


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        server._jobs = {}
        server._bx = dict(IDLE_BX)
        server._play_gen = 0
        server._cancel_gen = 0
        # Real tv_playback_state() can shell out to adb once ADB_ENABLED is
        # on; these tests are about the arbitration logic, not the TV's
        # actual state, so it is pinned to "idle" unless a test says
        # otherwise.
        self._orig_tvstate = server.tv_playback_state
        self._orig_app_fresh = server.app_fresh
        server.tv_playback_state = lambda *a, **kw: None
        server.app_fresh = lambda: None

    def tearDown(self):
        server._jobs = {}
        server._bx = dict(IDLE_BX)
        server._play_gen = 0
        server._cancel_gen = 0
        server.tv_playback_state = self._orig_tvstate
        server.app_fresh = self._orig_app_fresh

    def add_job(self, mid, owner="tv", stage="starting", otoken=None):
        kw = dict(stage=stage, owner=owner)
        if otoken is not None:
            kw["otoken"] = otoken
        server.job_set(mid, **kw)

    def live_browser(self, token="tok-1", state="playing", age=0.0):
        server._bx.update(token=token, state=state, at=time.time() - age)

    # -- the arbitration table --------------------------------------------

    def test_tv_accepted_with_nothing_playing(self):
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (True, None))

    def test_tv_accepted_over_a_tv_job(self):
        # Today's behaviour: a newer TV play supersedes an older one. This
        # is the one row that must never start refusing, or every "send to
        # TV" double-tap regresses to "Another device is playing".
        self.add_job("1", owner="tv")
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (True, None))

    def test_tv_refused_by_live_browser_session(self):
        self.live_browser()
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_tv_accepted_over_a_tv_job_still_preparing(self):
        # The supersede guard must hold at every JOB_ACTIVE stage, not just
        # the freshly-created "starting" one.
        self.add_job("1", owner="tv", stage="buffering")
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (True, None))

    def test_tv_refused_during_browser_preparation_window_buffering(self):
        # _bx["at"] is only stamped by /api/bx/beat, which the page cannot
        # send until the media is ready and the player is attached -- so
        # browser_playing() is False for the 30-120s a candidate spends
        # buffering, probing and converting. A TV request landing in that
        # window must not be able to silently supersede a film someone is
        # already sitting and waiting for. This is the regression guard for
        # that hole.
        self.add_job("1", owner="browser", stage="buffering")
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_tv_refused_during_browser_preparation_window_encoding(self):
        self.add_job("1", owner="browser", stage="encoding")
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_tv_accepted_after_browser_job_failed(self):
        # A finished-or-failed browser job must not go on locking the TV
        # out -- active_job() no longer returns it once it has errored, and
        # with no live _bx session either, "nothing" holds the player.
        self.add_job("1", owner="browser", stage="error")
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (True, None))

    def test_browser_accepted_with_nothing_playing(self):
        ok, msg = server.claim_owner("browser", token="tok-1")
        self.assertEqual((ok, msg), (True, None))

    def test_browser_refused_by_active_tv_job(self):
        self.add_job("1", owner="tv")
        ok, msg = server.claim_owner("browser", token="tok-1")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_browser_refused_by_tv_playback_state(self):
        # No job entry at all -- the film is already on screen, which is
        # exactly the state /api/cancel's own comment describes -- so only
        # tv_playback_state() can catch it.
        server.tv_playback_state = lambda *a, **kw: 3
        ok, msg = server.claim_owner("browser", token="tok-1")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_browser_refused_by_paused_tv_playback_state(self):
        server.tv_playback_state = lambda *a, **kw: 2
        ok, msg = server.claim_owner("browser", token="tok-1")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_browser_reclaims_same_token(self):
        # "Play a different film" from the same tab.
        self.live_browser(token="tok-1")
        ok, msg = server.claim_owner("browser", token="tok-1")
        self.assertEqual((ok, msg), (True, None))

    def test_browser_refused_by_live_session_different_token(self):
        self.live_browser(token="tok-1")
        ok, msg = server.claim_owner("browser", token="tok-2")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_browser_refused_during_another_browsers_preparation_window(self):
        # The mirror of the TV rows above, and the hole they left open: a
        # browser job that is still preparing has no _bx session yet, so
        # browser_playing() reads False for the whole 30-120s a candidate
        # spends buffering -- and a SECOND browser landing in that window was
        # accepted, superseding the tab already sitting in front of the
        # progress bar. The token is what tells the two apart.
        self.add_job("1", owner="browser", stage="buffering", otoken="tok-1")
        ok, msg = server.claim_owner("browser", token="tok-2")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_browser_refused_during_its_own_preparation_at_every_stage(self):
        for stage in ("starting", "buffering", "encoding", "launching"):
            with self.subTest(stage=stage):
                server._jobs = {}
                self.add_job("1", owner="browser", stage=stage, otoken="tok-1")
                ok, msg = server.claim_owner("browser", token="tok-2")
                self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_browser_reclaims_its_own_preparing_job_by_token(self):
        # The same attempt asking again is not a second device. Without this
        # the guard above would also refuse the tab that owns the job.
        self.add_job("1", owner="browser", stage="buffering", otoken="tok-1")
        ok, msg = server.claim_owner("browser", token="tok-1")
        self.assertEqual((ok, msg), (True, None))

    def test_browser_accepted_once_the_earlier_job_is_released(self):
        # What the page does before starting a fresh attempt: POST
        # /api/bx/stop ends the old job, active_job() stops returning it,
        # and the new token is free to claim the player. Without this the
        # retry button would be answered "Another device is playing" by its
        # own replacement request.
        self.add_job("1", owner="browser", stage="buffering", otoken="tok-1")
        server.job_set("1", stage="error", ok=False, msg="Playback abandoned")
        ok, msg = server.claim_owner("browser", token="tok-2")
        self.assertEqual((ok, msg), (True, None))

    def test_browser_preparation_refusal_does_not_touch_the_job(self):
        # claim_owner is read-only: losing the race must not mark the job
        # that won it as failed.
        self.add_job("1", owner="browser", stage="buffering", otoken="tok-1")
        server.claim_owner("browser", token="tok-2")
        amid, aj = server.active_job()
        self.assertEqual(amid, "1")
        self.assertEqual(aj.get("stage"), "buffering")
        self.assertEqual(aj.get("otoken"), "tok-1")

    def test_browser_claim_survives_a_refused_tv_claim(self):
        self.live_browser(token="tok-1")
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (False, "Another device is playing"))
        # claim_owner is read-only: a refusal must not have touched the
        # session it refused to displace.
        self.assertTrue(server.browser_playing())
        self.assertEqual(server._bx["token"], "tok-1")

    # -- browser_playing() ---------------------------------------------------

    def test_browser_playing_false_once_heartbeat_is_stale(self):
        self.live_browser(age=server.BX_IDLE + 1)
        self.assertFalse(server.browser_playing())

    def test_browser_playing_true_when_heartbeat_recent(self):
        self.live_browser(age=1.0)
        self.assertTrue(server.browser_playing())

    def test_browser_playing_true_while_paused(self):
        # The case that stops cache_watch() from tearing down the cache
        # under a film the viewer has merely paused, not abandoned.
        self.live_browser(state="paused")
        self.assertTrue(server.browser_playing())

    def test_browser_playing_false_once_ended(self):
        self.live_browser(state="ended")
        self.assertFalse(server.browser_playing())

    def test_browser_playing_implies_playing_now_the_cache_watch_guard(self):
        # cache_watch() only has playing_now() to go on, so this is the
        # property that actually keeps it from emptying the cache under a
        # browser viewer -- the most valuable assertion in this file.
        self.live_browser()
        self.assertTrue(server.browser_playing())
        self.assertTrue(server.playing_now())


class CancelRouteTest(unittest.TestCase):
    """POST /api/cancel, through do_POST itself. The route bumps _cancel_gen,
    and without a `global` for it in do_POST every cancel that had something
    to cancel died with UnboundLocalError -- a 500, and a job that went on
    buffering to seize the TV."""

    setUp = OwnershipTest.setUp
    tearDown = OwnershipTest.tearDown
    add_job = OwnershipTest.add_job

    def post(self, path, body=None):
        import io, json
        h = server.H.__new__(server.H)
        raw = json.dumps(body or {}).encode()
        h.headers = {"Host": "localhost", "Content-Length": str(len(raw))}
        h.path, h.rfile, h.close_connection = path, io.BytesIO(raw), False
        sent = {}
        h._send = lambda code, b, ctype="application/json": sent.update(code=code, body=b)
        server.H.do_POST(h)
        return sent["code"], sent["body"]

    def test_cancel_stands_down_a_buffering_job(self):
        self.add_job("m1", stage="buffering")
        gen = server._play_gen
        code, body = self.post("/api/cancel")
        self.assertEqual(code, 200)
        self.assertEqual(body.get("msg"), "cancelled")
        self.assertEqual(server.job_get("m1")["stage"], "error")
        self.assertTrue(server.superseded(gen))
        self.assertEqual(server._cancel_gen, 1)

    def test_cancel_reaches_a_play_still_resolving(self):
        server._play_inflight = 1
        try:
            code, body = self.post("/api/cancel")
        finally:
            server._play_inflight = 0
        self.assertEqual(code, 200)
        self.assertEqual(body.get("msg"), "cancelled")
        self.assertEqual(server._cancel_gen, 1)


if __name__ == "__main__":
    unittest.main()
