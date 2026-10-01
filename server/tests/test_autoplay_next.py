"""Autoplay-next as the TV app sees it: the heartbeat that reports an episode
ended answers with the next one ("next"), so the app can count down to it and
cancel it, and a next episode that never gets going retires its own job rather
than leaving the countdown polling a job that will never move."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


class AutoplayNextTest(unittest.TestCase):
    def setUp(self):
        server._jobs.clear()
        server._app = None
        server._app_cmd = None
        self._saved = {k: getattr(server, k) for k in
                       ("next_episode", "tv_season", "_autoplay_start", "start_play")}
        self.started = []
        server.next_episode = lambda tid, s, e: (s, e + 1)
        server.tv_season = lambda tid, s: {"episodes": [
            {"episode": 1, "name": "Pilot"}, {"episode": 2, "name": "The Second"}]}
        server._autoplay_start = lambda nid, resolve: self.started.append(nid)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(server, k, v)
        server._jobs.clear()
        server._app = None
        server._app_cmd = None

    def beat(self, **kw):
        d = {"id": "tv1", "name": "TV", "version": "1", "wait": 0}
        d.update(kw)
        return server.app_heartbeat(d)

    def test_the_ended_beat_names_the_next_episode(self):
        server.job_set("tv:show:1:1", autoplay=True)
        self.assertNotIn("next", self.beat(state="playing", job="tv:show:1:1", position_s=10))
        reply = self.beat(state="ended", job="tv:show:1:1")
        self.assertEqual(reply["next"], {"job": "tv:show:1:2", "s": 1, "e": 2, "name": "The Second"})
        self.assertEqual(self.started, ["tv:show:1:2"])
        # Registered before the thread runs, so the app's first poll finds it moving.
        self.assertEqual(server.job_get("tv:show:1:2")["stage"], "starting")
        # Only the edge: a repeat "ended" does not start it again.
        self.assertNotIn("next", self.beat(state="ended", job="tv:show:1:1"))
        self.assertEqual(self.started, ["tv:show:1:2"])

    def test_no_autoplay_no_next(self):
        server.job_set("tv:show:1:1", autoplay=False)
        self.beat(state="playing", job="tv:show:1:1", position_s=10)
        self.assertNotIn("next", self.beat(state="ended", job="tv:show:1:1"))
        self.assertEqual(self.started, [])

    def test_a_next_episode_with_no_stream_retires_its_job(self):
        server._autoplay_start = self._saved["_autoplay_start"]
        server.start_play = lambda nid, resolve, autoplay=False: (409, {"ok": False, "msg": "no stream"})
        server.job_set("tv:show:1:2", stage="starting")
        server._autoplay_start("tv:show:1:2", None)
        j = server.job_get("tv:show:1:2")
        self.assertEqual(j["stage"], "error")
        self.assertEqual(j["msg"], "no stream")

    def test_a_cancelled_job_keeps_its_own_message(self):
        server._autoplay_start = self._saved["_autoplay_start"]
        server.start_play = lambda nid, resolve, autoplay=False: (409, {"ok": False, "msg": "Cancelled"})
        server.job_set("tv:show:1:2", stage="error", msg="Cancelled by you")
        server._autoplay_start("tv:show:1:2", None)
        self.assertEqual(server.job_get("tv:show:1:2")["msg"], "Cancelled by you")


if __name__ == "__main__":
    unittest.main()
