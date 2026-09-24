"""server.py's side of hifi audio: the heartbeat state machine that decides
when the bridge is asked to start, stop or release, and what the TV is told.

Rules under test:
- audio restarts only for a viewer seek (seek_seq), pause/buffering recovery,
  a source change, or a stream that actually died -- never on a drift reading;
- a timeline the bridge no longer has is retired at once, never reused;
- a failed start backs off, and the TV is told why.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import queue
import sys
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
os.environ["SENDSPIN"] = "1"
import server  # noqa: E402


def drain():
    out = []
    while True:
        try:
            out.append(server._ss_q.get_nowait())
        except queue.Empty:
            return out


class HifiSyncTest(unittest.TestCase):
    def setUp(self):
        server.SENDSPIN_ENABLED = True
        server._app = None
        server._app_cmd = None
        server._hifi.update(on=False, gen=100, t0_us=None, clock_offset_us=0, streaming=False,
                            connected=False, src="http://127.0.0.1:11470/abc/0", aidx=0,
                            last_restart=0.0, err_s=None, player_url="ws://p:8928/sendspin",
                            pending_since=0.0, seek_seq=None, delay_ms=0, last_error=None,
                            fail_count=0, supply=None, cache=None, job="1",
                            applied_delay_ms=0, delay_at=0.0, tv_delay_ms=None,
                            volume=None)
        drain()

    def beat(self, state, pos=None, seek_seq=0, hifi=True):
        d = {"id": "tv", "name": "tv", "version": "t", "state": state, "job": "1",
             "position_s": pos, "wait": 0, "hifi": hifi, "hifi_player": "ws://p:8928/sendspin",
             "seek_seq": seek_seq}
        return server.app_heartbeat(d)

    def go_live(self, pos):
        """A playing beat, the bridge answering with a t0 for that gen."""
        server._hifi["last_restart"] = 0.0
        self.beat("playing", pos)
        gen = server._hifi["gen"]
        self.assertEqual([a[0] for a in drain()], ["start"])
        t0 = time.monotonic_ns() // 1000 - int(pos * 1e6)
        server._hifi_apply_status({"connected": True, "streaming": True, "pending": False,
                                   "gen": gen, "t0_us": t0, "ffmpeg_alive": True,
                                   "clock_offset_us": 0})
        self.assertTrue(server._hifi["streaming"])
        return gen

    def test_first_playing_beat_starts_once_and_reports_starting(self):
        r = self.beat("playing", 12.0)
        actions = drain()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0][:2], ("start", server._hifi["src"]))
        self.assertEqual(actions[0][3], 12.0, "audio starts at the reported position, no lead")
        self.assertAlmostEqual(actions[0][5], time.monotonic_ns() // 1000, delta=2_000_000,
                               msg="and carries the moment that position was true")
        self.assertEqual(r["hifi_status"], {"state": "starting", "msg": None})
        self.assertNotIn("sync", r)
        r = self.beat("playing", 13.0)
        self.assertEqual(drain(), [], "a pending start is not restarted")
        self.assertEqual(r["hifi_status"]["state"], "starting")

    def test_live_beats_carry_a_verdict_and_drift_never_restarts(self):
        gen = self.go_live(20.0)
        r = self.beat("playing", 21.0)
        self.assertEqual(r["hifi_status"]["state"], "live")
        self.assertEqual(r["sync"]["gen"], gen)
        self.assertAlmostEqual(r["sync"]["err_s"], 1.0, delta=0.05)
        # Picture 40 s away from the audio: the TV's problem, not a restart.
        r = self.beat("playing", 60.0)
        self.assertAlmostEqual(r["sync"]["err_s"], 40.0, delta=0.05)
        self.assertEqual(drain(), [])
        self.assertEqual(server._hifi["gen"], gen)

    def test_position_jumps_without_seek_seq_are_not_viewer_seeks(self):
        gen = self.go_live(20.0)
        server._hifi["last_restart"] = time.time()
        self.beat("playing", 20.5)               # same seek_seq as every beat so far
        self.beat("playing", 200.0)              # the TV's own correction landed far away
        self.assertEqual(drain(), [])
        self.assertEqual(server._hifi["gen"], gen)

    def test_viewer_seek_restarts_at_once_at_the_new_position(self):
        gen = self.go_live(20.0)
        server._hifi["last_restart"] = time.time()  # inside the min gap
        self.beat("playing", 20.5)
        self.assertEqual(drain(), [])
        r = self.beat("playing", 95.0, seek_seq=1)
        actions = drain()
        self.assertEqual([a[0] for a in actions], ["start"])
        self.assertEqual(actions[0][3], 95.0)
        self.assertGreater(server._hifi["gen"], gen)
        self.assertEqual(actions[0][4], server._hifi["gen"])
        self.assertIsNone(server._hifi["t0_us"])
        self.assertNotIn("sync", r)
        self.assertEqual(r["hifi_status"]["state"], "starting")

    def test_pause_stops_and_resume_restarts(self):
        gen = self.go_live(30.0)
        r = self.beat("paused", 31.0)
        self.assertEqual([a[0] for a in drain()], ["stop"])
        self.assertIsNone(server._hifi["t0_us"])
        self.assertEqual(r["hifi_status"]["state"], "stopped")
        self.beat("paused", 31.0)
        self.assertEqual(drain(), [], "one stop per pause")
        server._hifi["last_restart"] = 0.0
        self.beat("playing", 31.0)
        actions = drain()
        self.assertEqual([a[0] for a in actions], ["start"])
        self.assertEqual(actions[0][3], 31.0)
        self.assertGreater(server._hifi["gen"], gen)

    def test_bridge_restart_mid_film_retires_the_timeline(self):
        gen = self.go_live(40.0)
        # A fresh bridge knows nothing of our gen and has no stream.
        server._hifi_apply_status({"connected": False, "streaming": False, "pending": False,
                                   "gen": 0, "t0_us": None, "ffmpeg_alive": False})
        self.assertFalse(server._hifi["streaming"])
        self.assertIsNone(server._hifi["t0_us"])
        self.assertGreater(server._hifi["gen"], gen)
        self.assertEqual(server._hifi["last_error"], "audio bridge restarted")
        server._hifi["last_restart"] = 0.0
        r = self.beat("playing", 41.0)
        self.assertEqual([a[0] for a in drain()], ["start"])
        self.assertNotIn("sync", r)

    def test_a_stale_status_for_an_old_gen_is_ignored_while_a_start_is_pending(self):
        self.beat("playing", 12.0)
        gen = server._hifi["gen"]
        drain()
        # The bridge still describes the previous stream (old gen) with a t0.
        server._hifi_apply_status({"connected": True, "streaming": True, "pending": False,
                                   "gen": gen - 1, "t0_us": 12345, "ffmpeg_alive": True})
        self.assertIsNone(server._hifi["t0_us"], "an old timeline must never be adopted")
        self.assertGreater(server._hifi["pending_since"], 0)
        self.assertEqual(server._hifi["gen"], gen)

    def test_a_slipped_timeline_is_followed_under_the_same_gen(self):
        gen = self.go_live(50.0)
        t0 = server._hifi["t0_us"]
        server._hifi_apply_status({"connected": True, "streaming": True, "pending": False,
                                   "gen": gen, "t0_us": t0 + 300_000, "ffmpeg_alive": True})
        self.assertEqual(server._hifi["t0_us"], t0 + 300_000)
        self.assertEqual(server._hifi["gen"], gen)
        self.assertEqual(drain(), [])

    def test_stream_that_dies_is_restarted_not_reused(self):
        gen = self.go_live(50.0)
        server._hifi_apply_status({"connected": True, "streaming": False, "pending": False,
                                   "gen": gen, "t0_us": None, "ffmpeg_alive": False})
        self.assertIsNone(server._hifi["t0_us"])
        self.assertEqual(server._hifi["last_error"], "decoder ended")
        server._hifi["last_restart"] = 0.0
        self.beat("playing", 51.0)
        self.assertEqual([a[0] for a in drain()], ["start"])

    def test_failed_starts_back_off_and_are_surfaced(self):
        self.beat("playing", 12.0)
        drain()
        with server._lock:
            server._hifi_fail("not connected")
        self.assertEqual(server._hifi_restart_gap_s(), 6.0)
        r = self.beat("playing", 13.0)
        self.assertEqual(drain(), [], "3 s after the failure is inside the doubled gap")
        self.assertEqual(r["hifi_status"], {"state": "failed", "msg": "not connected"})
        for _ in range(5):
            with server._lock:
                server._hifi_fail("not connected")
        self.assertEqual(server._hifi_restart_gap_s(), server.HIFI_RESTART_MAX_GAP_S)
        server._hifi["last_restart"] = time.time() - server.HIFI_RESTART_MAX_GAP_S - 1
        self.beat("playing", 14.0)
        self.assertEqual([a[0] for a in drain()], ["start"])
        # A viewer seek is worth trying at once regardless of the back-off.
        with server._lock:
            server._hifi_fail("not connected")
        server._hifi["last_restart"] = time.time()
        self.beat("playing", 300.0, seek_seq=1)
        self.assertEqual([a[0] for a in drain()], ["start"])
        self.assertEqual(server._hifi["fail_count"], 0)

    def test_pending_start_times_out_and_retries(self):
        self.beat("playing", 12.0)
        drain()
        server._hifi["pending_since"] = time.time() - server.HIFI_START_TIMEOUT_S - 1
        server._hifi["last_restart"] = time.time() - server.HIFI_START_TIMEOUT_S - 1
        r = self.beat("playing", 60.0)
        actions = drain()
        self.assertEqual([a[0] for a in actions], ["start"])
        self.assertEqual(actions[0][3], 60.0)
        self.assertEqual(server._hifi["fail_count"], 1)
        self.assertEqual(r["hifi_status"]["state"], "starting")

    def test_end_of_film_releases_and_stop_retires_the_timeline_immediately(self):
        self.go_live(70.0)
        self.beat("ended", 71.0)
        self.assertEqual([a[0] for a in drain()], ["release"])
        self.assertIsNone(server._hifi["t0_us"])
        self.beat("idle")
        self.assertEqual(drain(), [], "one release per film")
        self.go_live(1.0)
        server._hifi_release()
        self.assertIsNone(server._hifi["t0_us"])
        self.assertFalse(server._hifi["streaming"])
        self.assertEqual([a[0] for a in drain()], ["release"])
        # A playing beat already in flight when the film was stopped restarts nothing.
        r = self.beat("playing", 2.0)
        self.assertEqual(drain(), [])
        self.assertEqual(r["hifi_status"]["state"], "failed")

    def test_a_track_that_plays_out_releases_the_player_instead_of_restarting(self):
        """Seen on 2026-09-17, at the end of Fight Club: the audio ran out
        before the picture did, the stopped stream read as a failure, and the
        restart put the player back into PLAYING for a stream with nothing in
        it. Music Assistant kept showing it playing until the bridge was
        restarted; stopping the film mid-way always released it cleanly."""
        gen = self.go_live(8340.0)
        cache = {"src": server._hifi["src"], "aidx": 0, "start_s": 0.0,
                 "end_s": 8348.4, "complete": True, "error": None}
        server._hifi_apply_status({"connected": True, "streaming": False, "pending": False,
                                   "gen": gen, "t0_us": None, "ffmpeg_alive": False,
                                   "clock_offset_us": 0, "cache": cache})
        self.assertEqual(drain(), [], "the end of a track is not a failure to retry")
        self.assertIsNone(server._hifi["last_error"], "nothing went wrong")
        r = self.beat("playing", 8348.25)
        self.assertEqual([a[0] for a in drain()], ["release"])
        self.assertTrue(server._hifi["done"])
        self.assertFalse(server._hifi["connected"])
        self.assertEqual(r["hifi_status"], {"state": "stopped", "msg": None})
        # The film's last frames keep arriving: the player stays let go, and
        # the overlay says stopped rather than blaming a failure.
        r = self.beat("playing", 8349.5)
        self.assertEqual(drain(), [], "one release per film")
        self.assertEqual(r["hifi_status"]["state"], "stopped")
        self.beat("ended", 8349.9)
        self.assertEqual(drain(), [], "and the end of the film has nothing left to release")
        # A viewer who rewinds out of the credits gets the audio back.
        self.beat("playing", 7000.0, seek_seq=1)
        self.assertEqual([a[0] for a in drain()], ["start"])
        self.assertFalse(server._hifi["done"])

    def test_the_previous_film_on_screen_does_not_start_the_next_films_audio(self):
        # run_play_job has committed to the next film: src/job moved on, timeline retired.
        server._hifi["src"] = "http://127.0.0.1:11470/next/0"
        server._hifi["job"] = "2"
        r = self.beat("playing", 725.0)  # job "1" still on screen
        self.assertEqual(drain(), [])
        self.assertEqual(r["hifi_status"]["state"], "starting")
        d = {"id": "tv", "name": "tv", "version": "t", "state": "playing", "job": "2",
             "position_s": 1.7, "wait": 0, "hifi": True, "seek_seq": 0}
        server.app_heartbeat(d)
        actions = drain()
        self.assertEqual([a[0] for a in actions], ["start"])
        self.assertEqual(actions[0][1], "http://127.0.0.1:11470/next/0")
        self.assertEqual(actions[0][3], 1.7)

    def test_idle_while_the_next_film_prebuffers_keeps_the_bridge(self):
        # run_play_job committed and queued prepare; the worker connected. The TV is idle
        # because the play command has not reached it yet.
        server._hifi["job"] = "2"
        server._hifi["src"] = "http://127.0.0.1:11470/next/0"
        server._hifi["connected"] = True
        d = {"id": "tv", "name": "tv", "version": "t", "state": "idle", "wait": 0, "hifi": True}
        server.app_heartbeat(d)
        self.assertEqual(drain(), [], "no release while a prepared film is on its way")
        self.assertTrue(server._hifi["connected"])
        # Once the film is over (or nothing is prepared), idle releases as before.
        server._hifi["job"] = None
        server.app_heartbeat(d)
        self.assertEqual([a[0] for a in drain()], ["release"])

    def test_the_trim_goes_to_the_bridge_and_the_picture_stays_put(self):
        gen = self.go_live(20.0)
        r = self.beat("playing", 21.0)
        self.assertAlmostEqual(r["sync"]["err_s"], 1.0, delta=0.05)
        # The TV's Settings value rides the heartbeat; it must reach the bridge,
        # which moves the sound. Nothing restarts.
        # Same position throughout: any change in err_s is the trim's doing.
        d = {"id": "tv", "name": "tv", "version": "t", "state": "playing", "job": "1",
             "position_s": 21.0, "wait": 0, "hifi": True, "seek_seq": 0, "hifi_delay_ms": 250}
        r = server.app_heartbeat(d)
        actions = drain()
        self.assertEqual(actions, [("delay", 250)])
        self.assertEqual(server._hifi["gen"], gen, "a trim is not a restart")
        self.assertEqual(server._hifi["delay_ms"], 250)
        # Until the bridge confirms, err is unchanged: the picture does not
        # chase a trim the sound has not taken on yet.
        self.assertAlmostEqual(r["sync"]["err_s"], 1.0, delta=0.05)
        # The bridge applies it by moving t0, and reports both. err stays put.
        t0 = server._hifi["t0_us"]
        server._hifi_apply_status({"connected": True, "streaming": True, "pending": False,
                                   "gen": gen, "t0_us": t0 + 250_000, "ffmpeg_alive": True,
                                   "delay_ms": 250})
        self.assertEqual(server._hifi["applied_delay_ms"], 250)
        r = self.beat("playing", 21.0)
        self.assertAlmostEqual(r["sync"]["err_s"], 1.0, delta=0.05)
        # Repeating the same value is not resent.
        server.app_heartbeat(d)
        self.assertEqual(drain(), [])
        # And a trim set from elsewhere is not undone by the TV repeating the
        # value it has stored: only a change at the TV is an instruction.
        with server._lock:
            server._hifi_set_delay(75)
        self.assertEqual(drain(), [("delay", 75)])
        server.app_heartbeat(d)
        self.assertEqual(drain(), [])
        self.assertEqual(server._hifi["delay_ms"], 75)
        server.app_heartbeat(dict(d, hifi_delay_ms=300))
        self.assertEqual(drain(), [("delay", 300)])

    def test_a_start_carries_the_trim_so_it_survives_a_restart(self):
        with server._lock:
            server._hifi_set_delay(-125)
        self.assertEqual([a[0] for a in drain()], ["delay"])
        server._hifi["last_restart"] = 0.0
        self.beat("playing", 30.0)
        self.assertEqual([a[0] for a in drain()], ["start"])
        self.assertEqual(server._hifi["delay_ms"], -125)

    def test_the_players_volume_rides_on_hifi_status_until_release(self):
        r = self.beat("playing", 12.0)
        self.assertNotIn("volume", r["hifi_status"], "no level is shown before the bridge says one")
        gen = self.go_live(20.0)
        server._hifi_apply_status({"connected": True, "streaming": True, "pending": False,
                                   "gen": gen, "t0_us": server._hifi["t0_us"],
                                   "ffmpeg_alive": True, "clock_offset_us": 0, "volume": 40})
        r = self.beat("playing", 21.0)
        self.assertEqual(r["hifi_status"]["volume"], 40)
        server._hifi_release()
        self.assertIsNone(server._hifi["volume"], "the player goes back to Music Assistant")

    def test_hifi_off_leaves_the_bridge_alone(self):
        r = self.beat("playing", 5.0, hifi=False)
        self.assertEqual(drain(), [])
        self.assertNotIn("hifi_status", r)
        self.assertNotIn("sync", r)

    def test_no_source_is_reported_as_a_failure(self):
        server._hifi["src"] = None
        r = self.beat("playing", 5.0)
        self.assertEqual(drain(), [])
        self.assertEqual(r["hifi_status"], {"state": "failed", "msg": "no audio source for this film"})


if __name__ == "__main__":
    unittest.main()
