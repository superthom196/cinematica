"""Autoplay-next as the TV app sees it: the heartbeat that reports an episode
ended answers with the next one ("next"), so the app can count down to it and
cancel it, and a next episode that never gets going retires its own job rather
than leaving the countdown polling a job that will never move."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402
import catalogue  # noqa: E402
import jobs  # noqa: E402
import tvlink  # noqa: E402


class AutoplayNextTest(unittest.TestCase):
    def setUp(self):
        jobs._jobs.clear()
        tvlink._app = None
        tvlink._app_cmd = None
        self._saved = {(m, k): getattr(m, k) for m, k in
                       ((catalogue, "next_episode"), (catalogue, "tv_season"),
                        (jobs, "_autoplay_start"), (jobs, "start_play"))}
        self.started = []
        catalogue.next_episode = lambda tid, s, e: (s, e + 1)
        catalogue.tv_season = lambda tid, s: {"episodes": [
            {"episode": 1, "name": "Pilot"}, {"episode": 2, "name": "The Second"}]}
        jobs._autoplay_start = lambda nid, resolve: self.started.append(nid)

    def tearDown(self):
        for (m, k), v in self._saved.items():
            setattr(m, k, v)
        jobs._jobs.clear()
        tvlink._app = None
        tvlink._app_cmd = None

    def beat(self, **kw):
        d = {"id": "tv1", "name": "TV", "version": "1", "wait": 0}
        d.update(kw)
        return tvlink.app_heartbeat(d)

    def test_the_ended_beat_names_the_next_episode(self):
        jobs.job_set("tv:show:1:1", autoplay=True)
        self.assertNotIn("next", self.beat(state="playing", job="tv:show:1:1", position_s=10))
        reply = self.beat(state="ended", job="tv:show:1:1")
        self.assertEqual(reply["next"], {"job": "tv:show:1:2", "s": 1, "e": 2, "name": "The Second"})
        self.assertEqual(self.started, ["tv:show:1:2"])
        # Registered before the thread runs, so the app's first poll finds it moving.
        self.assertEqual(jobs.job_get("tv:show:1:2")["stage"], "starting")
        # Only the edge: a repeat "ended" does not start it again.
        self.assertNotIn("next", self.beat(state="ended", job="tv:show:1:1"))
        self.assertEqual(self.started, ["tv:show:1:2"])

    def test_the_answer_worked_out_during_the_episode_is_used(self):
        def no_lookup(*a):
            raise AssertionError("the heartbeat asked the provider")
        catalogue.next_episode = no_lookup
        jobs.job_set("tv:show:1:1", autoplay=True,
                       next_ep={"s": 2, "e": 1, "name": "New Season"})
        self.beat(state="playing", job="tv:show:1:1", position_s=10)
        reply = self.beat(state="ended", job="tv:show:1:1")
        self.assertEqual(reply["next"], {"job": "tv:show:2:1", "s": 2, "e": 1, "name": "New Season"})

    def test_a_stored_end_of_show_starts_nothing(self):
        jobs.job_set("tv:show:1:9", autoplay=True, next_ep=False)
        self.beat(state="playing", job="tv:show:1:9", position_s=10)
        self.assertNotIn("next", self.beat(state="ended", job="tv:show:1:9"))
        self.assertEqual(self.started, [])

    def test_remembering_stores_the_next_episode_on_the_job(self):
        jobs.job_set("tv:show:1:1", autoplay=True)
        jobs._remember_next_episode("tv:show:1:1")
        self.assertEqual(jobs.job_get("tv:show:1:1")["next_ep"], {"s": 1, "e": 2, "name": "The Second"})

    def test_no_autoplay_no_next(self):
        jobs.job_set("tv:show:1:1", autoplay=False)
        self.beat(state="playing", job="tv:show:1:1", position_s=10)
        self.assertNotIn("next", self.beat(state="ended", job="tv:show:1:1"))
        self.assertEqual(self.started, [])

    def test_a_next_episode_with_no_stream_retires_its_job(self):
        jobs._autoplay_start = self._saved[(jobs, "_autoplay_start")]
        jobs.start_play = lambda nid, resolve, autoplay=False: (409, {"ok": False, "msg": "no stream"})
        jobs.job_set("tv:show:1:2", stage="starting")
        jobs._autoplay_start("tv:show:1:2", None)
        j = jobs.job_get("tv:show:1:2")
        self.assertEqual(j["stage"], "error")
        self.assertEqual(j["msg"], "no stream")

    def test_a_cancelled_job_keeps_its_own_message(self):
        jobs._autoplay_start = self._saved[(jobs, "_autoplay_start")]
        jobs.start_play = lambda nid, resolve, autoplay=False: (409, {"ok": False, "msg": "Cancelled"})
        jobs.job_set("tv:show:1:2", stage="error", msg="Cancelled by you")
        jobs._autoplay_start("tv:show:1:2", None)
        self.assertEqual(jobs.job_get("tv:show:1:2")["msg"], "Cancelled by you")


if __name__ == "__main__":
    unittest.main()
