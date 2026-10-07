"""Resuming a film whose audio is converted.

Rules under test:
- a resume converts from the resume point (-ss before -i), an ordinary play
  from 0:00;
- the file's real start is measured from the gap between its first video and
  first audio timestamps, and falls back to the resume point if unreadable;
- that start reaches the job as base_s, and the play command carries it only
  for a converted pick; a new play never inherits the last one's;
- all of that only for an app whose heartbeat says it reads base_s: an older
  one gets the from-the-top conversion it knows how to wait for;
- POST /api/restart/<job>?t= is a play of the same job from t, for a film
  or an episode, with an episode keeping its autoplay setting.

Run: python3 -m unittest discover -s server/tests -t server
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server  # noqa: E402,F401
import config  # noqa: E402
import disk  # noqa: E402
import jobs  # noqa: E402
import torrents  # noqa: E402
import nowplaying  # noqa: E402
import routes  # noqa: E402
import sendspin  # noqa: E402
import transcode  # noqa: E402
import tvlink  # noqa: E402


def post(path):
    h = routes.H.__new__(routes.H)
    raw = b"{}"
    h.headers = {"Host": "localhost", "Content-Length": str(len(raw))}
    h.path = path
    h.rfile = io.BytesIO(raw)
    h.close_connection = False
    sent = {}
    h._send = lambda code, b, ctype="application/json": sent.update(code=code, body=b)
    routes.H.do_POST(h)
    return sent["code"], sent["body"]


class FilmStart(unittest.TestCase):
    def test_the_picture_starts_at_the_keyframe_before_the_resume_point(self):
        # Measured with ffmpeg 7.1 on a 10s-GOP test file resumed at 25s: the
        # video copy starts on the 20s keyframe, the decoded audio at 25s.
        self.assertAlmostEqual(
            transcode.film_start_from("video,1.400000\naudio,6.373000\n", 25), 20.027, places=3)

    def test_an_unreadable_file_falls_back_to_the_resume_point(self):
        self.assertEqual(transcode.film_start_from("", 420), 420.0)
        self.assertEqual(transcode.film_start_from("video,1.4\n", 420), 420.0)
        self.assertEqual(transcode.film_start_from("video,N/A\naudio,2.0\n", 420), 420.0)

    def test_never_before_the_start_of_the_film(self):
        self.assertEqual(transcode.film_start_from("video,0.0\naudio,9.0\n", 3), 0.0)


class BeginSeeksTheInput(unittest.TestCase):
    def _cmd(self, start_s):
        seen = {}
        proc = mock.Mock()
        proc.poll.return_value = None

        def fake_start(key, cmd, name=None):
            seen["cmd"] = cmd
            return proc

        with mock.patch.object(transcode, "tc_cleanup"), \
                mock.patch.object(transcode, "_kill_ctr"), \
                mock.patch.object(transcode, "transcode_start", fake_start), \
                mock.patch.object(transcode.os.path, "exists", return_value=True), \
                mock.patch.object(transcode.os.path, "getsize", return_value=1 << 40), \
                mock.patch.object(transcode.os, "remove"), \
                mock.patch.object(transcode.os, "makedirs"), \
                mock.patch.object(transcode.threading, "Thread"):
            transcode.transcode_begin("a" * 40, 0, "http://src", 1_000_000, "m",
                                      start_s=start_s)
        return seen["cmd"]

    def test_a_resume_seeks_before_opening_the_input(self):
        cmd = self._cmd(420.5)
        i = cmd.index("-i")
        self.assertEqual(cmd[i - 2:i], ["-ss", "420.500"])

    def test_an_ordinary_play_converts_from_the_top(self):
        self.assertNotIn("-ss", self._cmd(0))
        self.assertNotIn("-ss", self._cmd(None))


class BaseReachesTheTv(unittest.TestCase):
    def setUp(self):
        jobs._jobs.clear()
        # a film reaching its player clears every other torrent through docker
        p = mock.patch.object(torrents, "keep_only", lambda h: None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        jobs._jobs.clear()

    def _run(self, start_s, can=True):
        jobs.job_set("cinemeta:tt1", start_s=start_s, base_s=None)
        prep = {"internal": "http://src", "acodec": "aac", "adur": 9000, "alangs": ["eng"],
                "acodecs": ["aac"], "aidx": 0, "got": 1, "rate": 1.0}
        begun = {}

        def fake_begin(*a, **kw):
            begun.update(kw)
            return "x.ts"

        launched = {}

        def fake_launch(url, mid, pick, title, gen):
            launched.update(jobs.job_get(mid), url=url)
            return True, ""

        with mock.patch.object(jobs, "prepare_candidate", return_value=prep), \
                mock.patch.dict(sendspin._hifi, {"on": False}), \
                mock.patch.object(config, "AUDIO_FIX", True), \
                mock.patch.object(disk, "cache_gb", return_value=30.0), \
                mock.patch.object(transcode, "transcode_stop_all"), \
                mock.patch.object(transcode, "transcode_begin", fake_begin), \
                mock.patch.object(transcode, "film_start", return_value=414.2) as fs, \
                mock.patch.object(transcode, "audio_url", return_value="http://audio"), \
                mock.patch.object(tvlink, "launch", fake_launch), \
                mock.patch.object(tvlink, "app_can", lambda cap: can and cap == "base_s"), \
                mock.patch.object(nowplaying, "_now_save"):
            jobs.run_play_job("cinemeta:tt1", [{"infoHash": "a" * 40, "gb": 5, "tag": "4k"}], 150)
        return begun, launched, fs

    def test_a_resume_converts_from_t_and_records_where_the_file_starts(self):
        begun, launched, fs = self._run(420.0)
        self.assertEqual(begun["start_s"], 420.0)
        self.assertEqual(launched["base_s"], 414.2)
        self.assertEqual(launched["url"], "http://audio")
        self.assertTrue(fs.called)

    def test_an_app_that_does_not_read_base_s_converts_from_the_top(self):
        begun, launched, fs = self._run(420.0, can=False)
        self.assertEqual(begun["start_s"], 0)
        self.assertIsNone(launched.get("base_s"))
        self.assertEqual(launched["start_s"], 420.0)   # its own wait-and-seek, as before

    def test_an_ordinary_play_has_no_base(self):
        begun, launched, fs = self._run(None)
        self.assertEqual(begun["start_s"], 0)
        self.assertIsNone(launched.get("base_s"))
        self.assertFalse(fs.called)

    def test_a_new_play_does_not_inherit_the_last_base(self):
        tvlink_fresh = tvlink.app_fresh
        tvlink.app_fresh = lambda: {"id": "tv1"}
        try:
            jobs.job_set("cinemeta:tt1", base_s=414.2)
            entry = {"pick": {"infoHash": "abc", "fileIdx": 0}, "title": "A Film"}
            jobs.start_play("cinemeta:tt1", lambda: (entry, 100), worker=lambda *a: None)
            self.assertIsNone(jobs.job_get("cinemeta:tt1").get("base_s"))
        finally:
            tvlink.app_fresh = tvlink_fresh


class PlayCommand(unittest.TestCase):
    def setUp(self):
        jobs._jobs.clear()
        self.cmds = []
        self._fresh, self._cmd = tvlink.app_fresh, tvlink.app_cmd
        tvlink.app_fresh = lambda: {"id": "tv1", "name": "TV", "job": "cinemeta:tt1",
                                    "acked": 1, "state": "playing"}
        tvlink.app_cmd = lambda kind, **f: (self.cmds.append(f), 1)[1]

    def tearDown(self):
        tvlink.app_fresh, tvlink.app_cmd = self._fresh, self._cmd
        jobs._jobs.clear()

    def test_carries_base_s_for_a_converted_pick(self):
        jobs.job_set("cinemeta:tt1", start_s=420.0, base_s=414.2)
        tvlink.launch("http://audio", "cinemeta:tt1", {"transcoded": True}, "A Film", None)
        self.assertEqual(self.cmds[-1]["base_s"], 414.2)
        self.assertEqual(self.cmds[-1]["start_s"], 420.0)

    def test_leaves_it_off_when_the_pick_plays_directly(self):
        # The conversion failed and the film fell back to the source: the TV
        # opens it at 0:00 and seeks to start_s itself.
        jobs.job_set("cinemeta:tt1", start_s=420.0, base_s=414.2)
        tvlink.launch("http://src", "cinemeta:tt1", {"transcoded": False}, "A Film", None)
        self.assertNotIn("base_s", self.cmds[-1])
        self.assertEqual(self.cmds[-1]["start_s"], 420.0)


class AppCaps(unittest.TestCase):
    def setUp(self):
        self._app = tvlink._app

    def tearDown(self):
        tvlink._app = self._app

    def beat(self, **kw):
        d = {"id": "tv1", "name": "TV", "version": "1", "wait": 0, "state": "idle"}
        d.update(kw)
        tvlink.app_heartbeat(d)

    def test_the_heartbeat_says_what_the_app_reads(self):
        self.beat(caps=["base_s"])
        self.assertTrue(tvlink.app_can("base_s"))
        self.assertFalse(tvlink.app_can("something_else"))

    def test_an_older_app_says_nothing_and_can_nothing(self):
        self.beat()
        self.assertFalse(tvlink.app_can("base_s"))


class RestartRoute(unittest.TestCase):
    def setUp(self):
        jobs._jobs.clear()

    def tearDown(self):
        jobs._jobs.clear()

    def _restart(self, path):
        calls = []

        def fake_start_play(jobid, resolve, autoplay=False, start_s=None, **kw):
            calls.append({"job": jobid, "autoplay": autoplay, "start_s": start_s})
            return 202, {"ok": True, "job": jobid}

        with mock.patch.object(jobs, "start_play", fake_start_play):
            code, body = post(path)
        return code, body, calls

    def test_a_film_plays_again_from_t(self):
        code, body, calls = self._restart("/api/restart/cinemeta%3Att1?t=95")
        self.assertEqual(code, 202)
        self.assertEqual(calls, [{"job": "cinemeta:tt1", "autoplay": False, "start_s": 95.0}])

    def test_an_episode_keeps_its_autoplay(self):
        jobs.job_set("tv:cinemeta:tt7:1:2", autoplay=True)
        code, _, calls = self._restart("/api/restart/tv%3Acinemeta%3Att7%3A1%3A2?t=30")
        self.assertEqual(code, 202)
        self.assertEqual(calls, [{"job": "tv:cinemeta:tt7:1:2", "autoplay": True, "start_s": 30.0}])

    def test_t_is_required(self):
        code, _, calls = self._restart("/api/restart/cinemeta%3Att1")
        self.assertEqual(code, 400)
        self.assertEqual(calls, [])

    def test_a_restart_already_under_way_is_joined(self):
        jobs.job_set("cinemeta:tt1", stage="buffering")
        code, body, calls = self._restart("/api/restart/cinemeta%3Att1?t=95")
        self.assertEqual(code, 202)
        self.assertEqual(body["msg"], "already starting")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
