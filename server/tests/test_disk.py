"""The disk's say in the cache: how big it may get, what a pick may weigh,
and what goes when the drive runs out of room mid-film.

Rules under test:
- the cache gets what is free plus what it already holds, less the 8 GB
  left for the OS -- most of a small drive, not a share of it;
- CACHE_GB still caps it from above;
- the largest pick follows the cache, so a 32 GB drive refuses a 20 GB film;
- an AAC release must fit twice (its AC3 copy), an AC3 one only once;
- a pack whose real file turns out too big for the disk is dropped as soon as
  its size is known, before anything more is fetched;
- the moment a film reaches its player, every other torrent goes -- the
  previous episode and the dropped candidates, open in Stremio or not --
  except one a newer play is still buffering;
- out of room, everything but the torrent in use goes -- the previous
  episode included, even though Stremio still holds it open and a film plays.

Run: python3 -m unittest discover -s server/tests -t server
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server  # noqa: E402,F401
import config  # noqa: E402
import disk  # noqa: E402
import jobs  # noqa: E402
import netprofile  # noqa: E402
import nowplaying  # noqa: E402
import streams  # noqa: E402
import torrents  # noqa: E402
from tests.test_cache import FakeBox  # noqa: E402

G = disk.GB


def cand(gb, display="Film 2160p x265 DDP5.1"):
    return {"codec": "HEVC", "is4k": True, "seeders": 100, "gb": gb,
            "infoHash": "a" * 40, "pack": False, "display": display}


class DiskBase(unittest.TestCase):
    drive = (2000 * G, 1500 * G, 0)

    def setUp(self):
        self.patches = [
            mock.patch.object(disk, "_measure", lambda: self.drive),
            mock.patch.dict(disk._m, {"at": 0.0, "v": None}),
            mock.patch.object(config, "CACHE_GB", 30.0),
            mock.patch.object(config, "DISK_RESERVE_GB", 8.0),
            mock.patch.object(config, "MAX_GB_4K", 25.0),
            mock.patch.object(config, "SUSTAIN_MBPS", 60.0),
            mock.patch.object(netprofile, "_net", {"samples": []}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def on(self, total, free, held=0):
        self.drive = (total * G, free * G, held * G)
        disk._m.update(at=0.0, v=None)


class Budget(DiskBase):
    def test_a_small_drive_gives_all_but_the_reserve(self):
        self.on(32, 24)              # an OS of 8 GB already on it
        self.assertEqual(disk.cache_gb(), 16.0)
        self.assertEqual(netprofile.max_gb(), 16.0)

    def test_what_the_cache_holds_is_room_it_can_reuse(self):
        self.on(32, 10, held=14)
        self.assertEqual(disk.cache_gb(), 16.0)

    def test_cache_gb_still_caps_a_big_drive(self):
        self.on(2000, 1500)
        self.assertEqual(disk.cache_gb(), 30.0)
        self.assertEqual(netprofile.max_gb(), 25.0)

    def test_a_full_drive_is_a_tiny_limit_not_no_limit(self):
        self.on(32, 7)
        self.assertEqual(disk.cache_gb(), 0.1)
        self.assertEqual(netprofile.max_gb(), 0.1)

    def test_over(self):
        self.on(32, 5, held=14)      # room for 11, holding 14
        self.assertTrue(disk.over())
        self.on(32, 7, held=1)       # inside the reserve
        self.assertTrue(disk.over())
        self.on(32, 12, held=5)
        self.assertFalse(disk.over())


class Picking(DiskBase):
    def test_small_drive_refuses_a_20gb_film(self):
        self.on(32, 24)
        self.assertEqual(streams.score(cand(20), runtime_min=150), -1)
        self.assertGreater(streams.score(cand(12), runtime_min=150), 0)

    def test_aac_must_fit_twice(self):
        self.on(32, 24)
        aac = "Film 2160p x265 AAC5.1"
        self.assertEqual(streams.score(cand(9, aac), runtime_min=150), -1)
        self.assertNotEqual(streams.score(cand(7, aac), runtime_min=150), -1)
        self.assertGreater(streams.score(cand(9), runtime_min=150), 0)

    def test_pack_too_big_for_the_disk_stops_at_the_tail(self):
        self.on(32, 24)
        pick = dict(cand(None), pack=True)
        with mock.patch.object(jobs, "fetch_tail", lambda *a: (True, int(21 * G))), \
             mock.patch.object(jobs, "job_set", lambda *a, **k: None), \
             mock.patch.object(jobs.urllib.request, "urlopen",
                               side_effect=AssertionError("fetched past the tail")):
            self.assertEqual(jobs.probe_and_buffer("tv:x", pick, 50, 1, 1), (False, 0, 0.0))
        self.assertTrue(pick["too_big"])


class AC3Copy(DiskBase):
    """run_play_job: a probed AAC/DTS file needs its AC3 copy to fit too."""

    def play(self, picks, keep_only=lambda h: None):
        import sendspin, transcode, tvlink
        prep = {"internal": "http://src", "acodec": "dts", "adur": 9000, "alangs": ["eng"],
                "acodecs": ["dts"], "aidx": 0, "got": 1, "rate": 1.0}
        begun, launched = [], []
        with mock.patch.object(jobs, "prepare_candidate", return_value=prep), \
                mock.patch.object(torrents, "keep_only", keep_only), \
                mock.patch.dict(sendspin._hifi, {"on": False}), \
                mock.patch.object(config, "AUDIO_FIX", True), \
                mock.patch.object(transcode, "transcode_stop_all"), \
                mock.patch.object(transcode, "transcode_begin",
                                  lambda *a, **k: begun.append(a[0]) or "x.ts"), \
                mock.patch.object(transcode, "audio_url", return_value="http://audio"), \
                mock.patch.object(tvlink, "launch",
                                  lambda url, mid, pick, title, gen: launched.append(pick["gb"]) or (True, "")), \
                mock.patch.object(nowplaying, "_now_save"), \
                mock.patch.dict(jobs._jobs, clear=True), \
                mock.patch.dict(nowplaying._now, clear=True), \
                redirect_stdout(io.StringIO()):
            jobs.run_play_job("cinemeta:tt1", picks, 150)
        return begun, launched

    def test_no_room_for_the_copy_moves_to_the_next_candidate(self):
        self.on(32, 24)
        begun, launched = self.play([dict(cand(12), infoHash="1" * 40),
                                     dict(cand(6), infoHash="2" * 40)])
        self.assertEqual(launched, [6])
        self.assertEqual(len(begun), 1)

    def test_reaching_the_player_clears_the_rest(self):
        self.on(2000, 1500)
        kept = []

        class Now:
            def __init__(self, target, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with mock.patch.object(jobs.threading, "Thread", Now):
            self.play([dict(cand(6), infoHash="2" * 40)], keep_only=kept.append)
        self.assertEqual(kept, ["2" * 40])

    def test_the_last_candidate_plays_unconverted(self):
        self.on(32, 24)
        begun, launched = self.play([cand(12)])
        self.assertEqual(launched, [12])
        self.assertEqual(begun, [])


class Pressure(DiskBase):
    PREV, NOW, OTHER = "d" * 40, "e" * 40, "f" * 40

    def setUp(self):
        super().setUp()
        self.patches += [mock.patch.dict(jobs._jobs, clear=True),
                         mock.patch.dict(nowplaying._now, clear=True),
                         mock.patch.object(netprofile, "stremio_set", lambda **k: True)]
        for p in self.patches[-3:]:
            p.start()

    def run_pressure(self, box, playing=True):
        out = io.StringIO()
        import subprocess, urllib.request, tvlink
        with mock.patch.object(subprocess, "run", box.run), \
             mock.patch.object(urllib.request, "urlopen", box.urlopen), \
             mock.patch.object(tvlink, "playing_now", lambda: playing), \
             redirect_stdout(out):
            torrents.disk_pressure()
        return out.getvalue()

    def keep_only(self, box, h):
        out = io.StringIO()
        import subprocess, urllib.request, tvlink
        with mock.patch.object(subprocess, "run", box.run), \
             mock.patch.object(urllib.request, "urlopen", box.urlopen), \
             mock.patch.object(tvlink, "playing_now", lambda: True), \
             redirect_stdout(out):
            torrents.keep_only(h)
        return out.getvalue()

    def test_a_new_film_playing_clears_every_other_torrent(self):
        self.on(2000, 1500)          # plenty of room: this is not about the disk
        jobs._jobs["tv:x:1:1"] = {"stage": "playing", "at": 100.0, "pick": {"infoHash": self.PREV}}
        jobs._jobs["tv:x:1:2"] = {"stage": "playing", "at": 200.0, "pick": {"infoHash": self.NOW}}
        box = FakeBox([self.PREV, self.NOW, self.OTHER], active=[self.PREV, self.NOW, self.OTHER])
        log = self.keep_only(box, self.NOW.upper())
        self.assertEqual(sorted(box.deleted), sorted([self.PREV, self.OTHER]))
        self.assertEqual(sorted(box.released), sorted([self.PREV, self.OTHER]))
        self.assertIn("new film playing, cleared 2 torrent(s)", log)

    def test_a_newer_play_still_buffering_keeps_its_torrent(self):
        import time
        jobs._jobs["cinemeta:tt2"] = {"stage": "buffering", "at": time.time(),
                                     "pick": {"infoHash": self.OTHER}}
        box = FakeBox([self.PREV, self.NOW, self.OTHER])
        self.keep_only(box, self.NOW)
        self.assertEqual(box.deleted, [self.PREV])

    def test_a_direct_source_clears_every_torrent(self):
        box = FakeBox([self.PREV, self.NOW])
        self.keep_only(box, None)
        self.assertEqual(sorted(box.deleted), sorted([self.PREV, self.NOW]))

    def test_binge_keeps_only_the_episode_playing(self):
        jobs._jobs["tv:x:1:1"] = {"stage": "playing", "at": 100.0, "pick": {"infoHash": self.PREV}}
        jobs._jobs["tv:x:1:2"] = {"stage": "playing", "at": 200.0, "pick": {"infoHash": self.NOW}}
        self.on(32, 1, held=15)
        box = FakeBox([self.PREV, self.NOW, self.OTHER], active=[self.PREV, self.NOW])
        log = self.run_pressure(box)
        self.assertEqual(sorted(box.deleted), sorted([self.PREV, self.OTHER]))
        self.assertEqual(box.released, [self.PREV])
        self.assertIn("freed disk: 2 torrent(s)", log)

    def test_after_a_restart_nowplaying_names_the_film(self):
        nowplaying._now["hash"] = self.NOW
        self.on(32, 1, held=15)
        box = FakeBox([self.PREV, self.NOW], active=[self.PREV, self.NOW])
        self.run_pressure(box)
        self.assertEqual(box.deleted, [self.PREV])

    def test_nothing_known_while_playing_keeps_open_engines(self):
        self.on(32, 1, held=15)
        box = FakeBox([self.PREV, self.OTHER], active=[self.PREV])
        self.run_pressure(box)
        self.assertEqual(box.deleted, [self.OTHER])

    def test_room_to_spare_touches_nothing(self):
        self.on(32, 20, held=5)
        box = FakeBox([self.PREV])
        self.run_pressure(box)
        self.assertEqual(box.deleted, [])


if __name__ == "__main__":
    unittest.main()
