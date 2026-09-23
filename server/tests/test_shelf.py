"""Favourites / watched / resume behaviour for shelf.py -- one test per row
of the shelf contract (pinning bands, watched transitions, drop/mute,
series next-episode logic, fade, save throttling, corrupt-file recovery).

Run: python3 -m pytest tests/test_shelf.py -q
     python3 -m unittest discover -s server/tests -t server
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import shelf  # noqa: E402


NOW = 1_700_000_000.0
DAY = 86400.0


class ShelfTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, "shelf.json")
        shelf.init(self._path)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # -- film progress bands ------------------------------------------------

    def test_credits_stop_marks_watched_not_pinned(self):
        changed = shelf.note_progress(
            "cinemeta:tt1", 5500, 6000, "playing", now=NOW
        )
        self.assertTrue(changed)
        v = shelf.view("cinemeta:tt1", now=NOW)
        self.assertTrue(v["watched"])
        self.assertFalse(v["pinned"])
        self.assertIsNone(v["resume_s"])

    def test_ended_state_marks_watched(self):
        shelf.note_progress("cinemeta:tt2", 100, 6000, "ended", now=NOW)
        v = shelf.view("cinemeta:tt2", now=NOW)
        self.assertTrue(v["watched"])
        self.assertFalse(v["pinned"])

    def test_early_bail_not_pinned_resume_kept(self):
        # 500/6000 = 0.083, below PIN_FROM but above RESUME_MIN_S.
        shelf.note_progress("cinemeta:tt3", 500, 6000, "paused", now=NOW)
        v = shelf.view("cinemeta:tt3", now=NOW)
        self.assertFalse(v["pinned"])
        self.assertFalse(v["watched"])
        self.assertEqual(v["resume_s"], 495)

    def test_mid_stop_pinned_with_resume(self):
        # 3000/6000 = 0.5, within [PIN_FROM, DONE_AT).
        shelf.note_progress("cinemeta:tt4", 3000, 6000, "paused", now=NOW)
        v = shelf.view("cinemeta:tt4", now=NOW)
        self.assertTrue(v["pinned"])
        self.assertAlmostEqual(v["progress"], 0.5)
        self.assertEqual(v["resume_s"], 2995)

    # -- drop / mute ----------------------------------------------------

    def test_drop_unpins_and_mutes_job_until_begin(self):
        job = "cinemeta:tt5"
        shelf.note_progress(job, 3000, 6000, "paused", now=NOW)
        shelf.drop(job=job, now=NOW + 1)
        v = shelf.view("cinemeta:tt5", now=NOW + 1)
        self.assertFalse(v["pinned"])
        self.assertFalse(v["watched"])
        self.assertIsNone(v["resume_s"])

        # Muted: further progress on the same job is ignored.
        changed = shelf.note_progress(job, 3100, 6000, "paused", now=NOW + 2)
        self.assertFalse(changed)
        v2 = shelf.view("cinemeta:tt5", now=NOW + 2)
        self.assertIsNone(v2["resume_s"])

        # begin() un-mutes it, so progress resumes recording.
        shelf.begin(job)
        changed2 = shelf.note_progress(job, 3200, 6000, "paused", now=NOW + 3)
        self.assertTrue(changed2)
        v3 = shelf.view("cinemeta:tt5", now=NOW + 3)
        self.assertTrue(v3["pinned"])

    # -- series ------------------------------------------------------------

    def test_series_one_episode_watched_not_pinned(self):
        shelf.note_progress(
            "tv:cinemeta:tt6:1:1", 3000, 3000, "ended", kind="tv", now=NOW
        )
        v = shelf.view("cinemeta:tt6", now=NOW)
        self.assertFalse(v["pinned"])

    def test_series_two_watched_has_next_pinned_with_next(self):
        shelf.note_progress(
            "tv:cinemeta:tt7:1:1", 3000, 3000, "ended", kind="tv", now=NOW
        )
        shelf.note_progress(
            "tv:cinemeta:tt7:1:2",
            3000,
            3000,
            "ended",
            kind="tv",
            has_next=True,
            now=NOW,
        )
        v = shelf.view("cinemeta:tt7", now=NOW)
        self.assertTrue(v["pinned"])
        self.assertEqual(v["next"], {"s": 1, "e": 3, "resume_s": None})

    def test_series_mid_episode_pinned_with_next_resume_s(self):
        shelf.note_progress(
            "tv:cinemeta:tt8:1:3", 3000, 6000, "paused", kind="tv", now=NOW
        )
        v = shelf.view("cinemeta:tt8", now=NOW)
        self.assertTrue(v["pinned"])
        self.assertEqual(v["next"]["s"], 1)
        self.assertEqual(v["next"]["e"], 3)
        self.assertEqual(v["next"]["resume_s"], 2995)
        self.assertAlmostEqual(v["progress"], 0.5)

    # -- fade ----------------------------------------------------------

    def test_fade_after_31_days_unpins_but_keeps_resume(self):
        shelf.note_progress("cinemeta:tt9", 3000, 6000, "paused", now=NOW)
        later = NOW + 31 * DAY
        v = shelf.view("cinemeta:tt9", now=later)
        self.assertFalse(v["pinned"])
        self.assertEqual(v["resume_s"], 2995)

    # -- duration edge cases ---------------------------------------------

    def test_provisional_duration_not_watched(self):
        # dur_s 600 is < 0.8 * runtime_s (7200), so runtime_s is trusted;
        # 590/7200 is nowhere near DONE_AT.
        shelf.note_progress(
            "cinemeta:tt10", 590, 600, "playing", runtime_s=7200, now=NOW
        )
        v = shelf.view("cinemeta:tt10", now=NOW)
        self.assertFalse(v["watched"])

    def test_short_position_no_resume_point(self):
        changed = shelf.note_progress("cinemeta:tt11", 30, 6000, "paused", now=NOW)
        v = shelf.view("cinemeta:tt11", now=NOW)
        self.assertIsNone(v["resume_s"])
        # Nothing pre-existed and nothing was worth recording.
        self.assertFalse(changed)

    # -- parse_job -----------------------------------------------------

    def test_tv_job_colon_in_title_id_parses(self):
        tid, s, e = shelf.parse_job("tv:cinemeta:tt0903747:2:4")
        self.assertEqual((tid, s, e), ("cinemeta:tt0903747", 2, 4))
        self.assertEqual(shelf.parse_job("cinemeta:tt0903747"), ("cinemeta:tt0903747", None, None))
        self.assertEqual(shelf.parse_job("tv:garbage"), (None, None, None))
        self.assertEqual(shelf.parse_job(None), (None, None, None))

    # -- persistence -----------------------------------------------------

    def test_save_init_round_trip(self):
        shelf.note_progress("cinemeta:tt12", 3000, 6000, "paused", now=NOW)
        shelf.save(force=True)
        shelf.init(self._path)
        v = shelf.view("cinemeta:tt12", now=NOW)
        self.assertTrue(v["pinned"])

    def test_corrupt_file_gives_empty_state(self):
        with open(self._path, "w") as f:
            f.write("{not valid json")
        shelf.init(self._path)  # must not raise
        v = shelf.view("anything", now=NOW)
        self.assertEqual(
            v,
            {
                "fav": False,
                "watched": False,
                "pinned": False,
                "progress": None,
                "resume_s": None,
                "next": None,
            },
        )

    def test_save_throttle(self):
        shelf.note_progress("cinemeta:tt13", 3000, 6000, "paused", now=NOW)
        shelf.save(force=True)
        mtime_after_first = os.path.getmtime(self._path)

        shelf.note_progress("cinemeta:tt13", 3050, 6000, "paused", now=NOW + 1)
        shelf.save()  # within FLUSH_S of the first write -> no-op
        self.assertEqual(os.path.getmtime(self._path), mtime_after_first)

        shelf.save(force=True)  # forced -> writes now
        self.assertGreaterEqual(os.path.getmtime(self._path), mtime_after_first)
        shelf.init(self._path)
        v = shelf.view("cinemeta:tt13", now=NOW + 1)
        self.assertEqual(v["resume_s"], 3045)

    # -- favourites / set_watched / decorate ------------------------------

    def test_favourites_order(self):
        shelf.set_fav("cinemeta:tt14", True, snap={"title": "First"}, now=NOW)
        shelf.set_fav("cinemeta:tt15", True, snap={"title": "Second"}, now=NOW + 10)
        favs = shelf.favourites(now=NOW + 10)
        self.assertEqual([f["id"] for f in favs], ["cinemeta:tt15", "cinemeta:tt14"])

    def test_favourite_never_played_keeps_its_kind(self):
        shelf.set_fav("cinemeta:tt17", True, snap={"title": "A Series", "kind": "tv"}, now=NOW)
        self.assertEqual(shelf.favourites(now=NOW)[0]["kind"], "tv")

    def test_watchlist_drops_a_title_watched_after_saving(self):
        shelf.set_fav("cinemeta:tt20", True, snap={"title": "Saved"}, now=NOW)
        shelf.set_watched("cinemeta:tt20", True, now=NOW + 5)
        self.assertEqual(shelf.favourites(now=NOW + 5), [])
        # Un-marking watched (the fell-asleep undo) puts it back.
        shelf.set_watched("cinemeta:tt20", False, now=NOW + 6)
        self.assertEqual([f["id"] for f in shelf.favourites(now=NOW + 6)], ["cinemeta:tt20"])

    def test_watchlist_keeps_a_rewatch_saved_after_watching(self):
        shelf.set_watched("cinemeta:tt21", True, now=NOW)
        shelf.set_fav("cinemeta:tt21", True, snap={"title": "Again"}, now=NOW + 5)
        self.assertEqual([f["id"] for f in shelf.favourites(now=NOW + 5)], ["cinemeta:tt21"])
        shelf.set_watched("cinemeta:tt21", True, now=NOW + 9)
        self.assertEqual(shelf.favourites(now=NOW + 9), [])

    def test_watchlist_drops_a_series_finished_by_its_episodes(self):
        tid = "cinemeta:tt22"
        shelf.set_fav(tid, True, snap={"title": "Short Run", "kind": "tv"}, now=NOW)
        shelf.set_watched(tid, True, s=1, e=1, now=NOW + 1)
        # Not finished yet: stays.
        self.assertEqual([f["id"] for f in shelf.favourites(now=NOW + 1)], [tid])
        shelf.set_watched(tid, True, now=NOW + 2)
        self.assertEqual(shelf.favourites(now=NOW + 2), [])

    def test_set_watched_undo(self):
        shelf.set_watched("cinemeta:tt16", True, now=NOW)
        self.assertTrue(shelf.view("cinemeta:tt16", now=NOW)["watched"])
        shelf.set_watched("cinemeta:tt16", False, now=NOW)
        self.assertFalse(shelf.view("cinemeta:tt16", now=NOW)["watched"])

    def test_decorate_unknown_id_default(self):
        item = shelf.decorate({"id": "cinemeta:nope"}, now=NOW)
        self.assertEqual(item["shelf"]["fav"], False)
        self.assertEqual(item["shelf"]["watched"], False)
        self.assertEqual(item["shelf"]["pinned"], False)
        no_id_item = shelf.decorate({}, now=NOW)
        self.assertEqual(no_id_item["shelf"]["pinned"], False)


if __name__ == "__main__":
    unittest.main()
