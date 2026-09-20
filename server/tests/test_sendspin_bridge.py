"""The bridge's decoder lifecycle, against a real subprocess.

The failure these guard against was seen live on 2026-09-16: the reader task
spent most of its time asleep between commits while ffmpeg kept writing, so
the pipe's read buffer filled and asyncio paused it; on teardown nobody drained
it, EOF never arrived, wait() hung, the caller's HTTP timeout fired, and the
next /start's cleanup killed the decoder it had just started. Every test here
uses a subprocess that floods stdout exactly like ffmpeg does; with the PCM
cache that blocked-pipe state is the normal one whenever the lead is full.

Run: python3 -m unittest discover -s server/tests -t server
"""

import asyncio
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _stubs  # noqa: E402

_stubs.install()
import sendspin_bridge as bridge  # noqa: E402

FLOOD = [sys.executable, "-u", "-c",
         "import os\nd = bytes(9600)\nwhile True:\n    os.write(1, d)\n"]
# Three whole chunks then EOF: a track that ends.
SHORT = [sys.executable, "-u", "-c", "import os; os.write(1, bytes(9600 * 3))"]
# Sleeps first, like ffmpeg seeking an MKV over the swarm.
SLOW = [sys.executable, "-u", "-c",
        "import os, time\ntime.sleep(0.6)\nd = bytes(9600)\nwhile True:\n    os.write(1, d)\n"]
BROKEN = [sys.executable, "-u", "-c", "import sys; sys.exit(1)"]


class FakeStream:
    """Enough of aiosendspin's PushStream: an explicit play_start_us pins the
    timeline, later commits continue it, sleep_to_limit_buffer parks the
    pusher like the real one."""

    def __init__(self):
        self.committed = 0
        self.stopped = False
        self.t = None
        self.pinned = []

    def set_live_source(self, live):
        pass

    def prepare_audio(self, chunk, fmt):
        assert chunk and len(chunk) % 4 == 0, "whole stereo frames only"

    async def commit_audio(self, *, play_start_us=None):
        self.committed += 1
        if play_start_us is not None:
            self.pinned.append(play_start_us)
            self.t = play_start_us
        else:
            self.t = (self.t if self.t is not None else self.now_us() + 600_000) + bridge.CHUNK_US
        return self.t

    def now_us(self):
        return time.monotonic_ns() // 1000

    async def sleep_to_limit_buffer(self, max_us):
        await asyncio.sleep(0.005)

    def stop(self):
        self.stopped = True


class FakeRole:
    volume = 40


class FakeGroup:
    def __init__(self):
        self.streams = []
        self.stops = 0

    def start_stream(self):
        s = FakeStream()
        self.streams.append(s)
        return s

    async def stop(self):
        self.stops += 1
        return True


class FakeClient:
    def __init__(self):
        self.is_connected = True
        self.group = FakeGroup()
        self.info = type("Info", (), {"name": "fake"})()

    def roles_by_family(self, fam):
        return [FakeRole()]


class FakeServer:
    def __init__(self, client):
        self.client = client
        self.clock = type("Clock", (), {"now_us": staticmethod(lambda: time.monotonic_ns() // 1000)})()
        self.disconnected = []

    def get_client(self, cid):
        return self.client

    def disconnect_from_client(self, url):
        self.disconnected.append(url)


class FakeRequest:
    def __init__(self, body=None):
        self._body = body or {}
        self.body_exists = bool(body)

    async def json(self):
        return self._body


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeClient()
        bridge.SERVER = FakeServer(self.client)
        bridge.CLIENT_ID = "fake"
        bridge.ACTIVE_URL = "ws://fake:8928/sendspin"
        bridge.STATE["decoder"] = None
        bridge.STATE["pusher"] = None
        bridge.STATE["gen"] = 0
        bridge.PLAYERS.clear()
        self.tmp = tempfile.mkdtemp()
        self._saved = (bridge.CACHE_DIR, bridge.DECODE_LEAD_S, bridge.START_TIMEOUT_S)
        bridge.CACHE_DIR = pathlib.Path(self.tmp)
        bridge.DECODE_LEAD_S = 30.0
        bridge.STATE["delay_us"] = 0
        self.kills = []
        self.spawned = 0
        self.spawn_args = FLOOD

        async def fake_kill(pattern):
            self.kills.append(pattern)

        async def fake_spawn(args):
            self.ffmpeg_args = args
            self.spawned += 1
            return await asyncio.create_subprocess_exec(
                *self.spawn_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

        self._orig = (bridge._kill_container_ffmpeg, bridge._spawn_decoder)
        bridge._kill_container_ffmpeg = fake_kill
        bridge._spawn_decoder = fake_spawn

    async def asyncTearDown(self):
        async with bridge._lock:
            await bridge._stop_pusher()
            await bridge._drop_decoder()
        bridge._kill_container_ffmpeg, bridge._spawn_decoder = self._orig
        bridge.CACHE_DIR, bridge.DECODE_LEAD_S, bridge.START_TIMEOUT_S = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def prepare(self, start_s=0.0):
        return await bridge.handle_prepare(FakeRequest({"src": "http://src", "aidx": 0, "start_s": start_s}))

    async def start(self, gen, start_s=10.0, pos_at_us=None, delay_ms=None):
        body = {"src": "http://src", "aidx": 0, "start_s": start_s, "gen": gen}
        if pos_at_us is not None:
            body["pos_at_us"] = pos_at_us
        if delay_ms is not None:
            body["delay_ms"] = delay_ms
        return await bridge.handle_start(FakeRequest(body))

    def dec_of(self, p):
        return p.dec

    async def wait_cache(self, seconds, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            dec = bridge.STATE["decoder"]
            if dec is not None and dec.end_s >= seconds:
                return dec
            await asyncio.sleep(0.01)
        self.fail("cache never reached %ss: %s" % (seconds, bridge.status_snapshot()))

    async def wait_live(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = bridge.status_snapshot()
            if snap["streaming"]:
                return snap
            await asyncio.sleep(0.01)
        self.fail("stream never went live: %s" % bridge.status_snapshot())

    async def wait_pusher_done(self, p, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not p.task.done():
            await asyncio.sleep(0.01)
        self.assertTrue(p.task.done())

    # -- decoding into the cache ---------------------------------------------

    async def test_prepare_decodes_ahead_and_stops_at_the_lead(self):
        bridge.DECODE_LEAD_S = 0.5
        resp = await self.prepare()
        self.assertEqual(resp.status, 200, resp.data)
        self.assertTrue(resp.data["prepared"])
        dec = await self.wait_cache(0.5)
        self.assertIn("comment=" + dec.marker, self.ffmpeg_args)
        # The downmix keeps ffmpeg's matrix but not its 7.7 dB clip-proof scaling.
        af = self.ffmpeg_args[self.ffmpeg_args.index("-af") + 1]
        self.assertEqual(af, "aresample=rematrix_maxval=2,aformat=channel_layouts=stereo")
        self.assertTrue(os.path.exists(dec.path))
        await asyncio.sleep(0.4)
        # Bounded: no more than the lead plus one read beyond the play head (still at 0).
        self.assertLessEqual(dec.end_bytes, int(0.5 * bridge.BYTES_PER_S) + bridge.DECODE_READ_BYTES)
        self.assertTrue(dec.alive, "ffmpeg is held on its pipe, not killed")
        self.assertEqual(os.path.getsize(dec.path), dec.end_bytes)
        snap = bridge.status_snapshot()
        self.assertFalse(snap["streaming"])
        self.assertFalse(snap["pending"])
        self.assertTrue(snap["ffmpeg_alive"])
        self.assertEqual(snap["cache"]["start_s"], 0.0)
        self.assertIsNone(snap["supply"])

    async def test_prepare_twice_for_the_same_track_keeps_the_decoder(self):
        await self.prepare()
        dec = bridge.STATE["decoder"]
        await self.prepare()
        self.assertIs(bridge.STATE["decoder"], dec)
        self.assertEqual(self.spawned, 1)

    async def test_teardown_completes_with_ffmpeg_blocked_on_a_full_pipe(self):
        bridge.DECODE_LEAD_S = 0.2
        await self.prepare()
        dec = await self.wait_cache(0.2)
        await asyncio.sleep(0.3)  # the lead is full: nobody reads, ffmpeg is blocked writing
        t = time.monotonic()
        async with bridge._lock:
            await bridge._drop_decoder()
        self.assertLess(time.monotonic() - t, bridge.TEARDOWN_TIMEOUT_S, "teardown hung on the unread pipe")
        self.assertIsNotNone(dec.proc.returncode, "decoder client never reaped")
        self.assertTrue(dec.task.done())
        self.assertEqual(self.kills, [dec.marker], "container kill must target this decoder only")
        self.assertFalse(os.path.exists(dec.path), "cache file dropped with its decoder")
        self.assertIsNone(bridge.STATE["decoder"])

    # -- pushing from the cache ------------------------------------------------

    async def test_start_from_cache_is_immediate_and_pins_the_timeline(self):
        await self.prepare()
        await self.wait_cache(15.0)
        pos_at = time.monotonic_ns() // 1000 - 1_000_000  # film time 10.0 was on screen 1 s ago
        t = time.monotonic()
        resp = await self.start(1, start_s=10.0, pos_at_us=pos_at)
        self.assertEqual(resp.status, 200, resp.data)
        self.assertLess(time.monotonic() - t, 0.5, "served from the cache, no decoder seek")
        self.assertAlmostEqual(resp.data["t0_us"], pos_at - 10_000_000, delta=2_000)
        p = bridge.STATE["pusher"]
        stream = p.stream
        # The first chunk was pinned: film time x0 plays at t0 + x0, x0 a send-ahead from now.
        self.assertEqual(len(stream.pinned), 1)
        x0 = (stream.pinned[0] - p.t0_us) / 1e6
        self.assertGreater(x0, 11.0)
        self.assertLess(x0, 12.5)
        snap = await self.wait_live()
        self.assertEqual(snap["t0_us"], resp.data["t0_us"])
        self.assertGreater(snap["supply"]["chunks"], 0)
        self.assertEqual(self.spawned, 1)
        self.assertEqual(self.kills, [])

    async def test_seek_inside_the_cache_reuses_the_decoder_and_a_jump_past_it_does_not(self):
        await self.prepare()
        await self.wait_cache(20.0)
        await self.start(1, start_s=10.0)
        await self.wait_live()
        old_push = bridge.STATE["pusher"]
        dec = bridge.STATE["decoder"]
        resp = await self.start(2, start_s=3.0)  # back inside the cache
        self.assertEqual(resp.status, 200, resp.data)
        self.assertTrue(old_push.task.done(), "the old push is retired, not abandoned")
        self.assertIs(bridge.STATE["decoder"], dec)
        self.assertEqual(self.spawned, 1)
        self.assertEqual(self.kills, [])
        pending = [t for t in asyncio.all_tasks() if not t.done() and t.get_coro().__name__ == "_push_loop"]
        self.assertEqual(len(pending), 1)
        resp = await self.start(3, start_s=5000.0)  # far past anything decoded
        self.assertIn(resp.status, (200, 202), resp.data)
        self.assertIsNot(bridge.STATE["decoder"], dec)
        self.assertEqual(self.spawned, 2)
        self.assertEqual(self.kills, [dec.marker])
        self.assertFalse(os.path.exists(dec.path))
        self.assertEqual(bridge.STATE["decoder"].start_s, 5000.0)
        snap = await self.wait_live()
        self.assertEqual(snap["gen"], 3)

    async def test_stop_is_a_pause_that_keeps_the_cache_and_release_drops_it(self):
        await self.prepare()
        await self.wait_cache(15.0)
        await self.start(1, start_s=10.0)
        await self.wait_live()
        p = bridge.STATE["pusher"]
        dec = bridge.STATE["decoder"]
        resp = await bridge.handle_stop(FakeRequest())
        self.assertEqual(resp.data, {"stopped": True})
        self.assertEqual(self.client.group.stops, 1)
        self.assertTrue(p.task.done())
        self.assertTrue(p.stream.stopped)
        self.assertIs(bridge.STATE["decoder"], dec)
        self.assertTrue(dec.alive)
        snap = bridge.status_snapshot()
        self.assertEqual((snap["streaming"], snap["t0_us"], snap["audio_pos_s"], snap["pending"]),
                         (False, None, None, False))
        self.assertEqual(snap["gen"], 1)
        self.assertIsNotNone(snap["cache"])
        # Resume: straight from the cache again.
        resp = await self.start(2, start_s=12.0)
        self.assertEqual(resp.status, 200, resp.data)
        self.assertEqual(self.spawned, 1)
        resp = await bridge.handle_release(FakeRequest())
        self.assertEqual(resp.data, {"released": True})
        self.assertEqual(bridge.SERVER.disconnected, ["ws://fake:8928/sendspin"])
        self.assertIsNone(bridge.CLIENT_ID)
        self.assertIsNone(bridge.STATE["decoder"])
        self.assertEqual(self.kills, [dec.marker])
        self.assertFalse(os.path.exists(dec.path))

    async def test_start_without_prepare_decodes_from_there_and_goes_pending(self):
        self.spawn_args = SLOW
        bridge.START_TIMEOUT_S = 0.3  # the fixture's seek is 0.6 s; keep the test quick
        t = time.monotonic()
        resp = await self.start(3, start_s=40.0)
        self.assertEqual(resp.status, 202, resp.data)
        self.assertTrue(resp.data["pending"])
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(bridge.STATE["decoder"].start_s, 40.0)
        snap = bridge.status_snapshot()
        self.assertTrue(snap["pending"])
        self.assertFalse(snap["streaming"])
        self.assertIsNone(snap["t0_us"])
        snap = await self.wait_live()
        self.assertFalse(snap["pending"])
        self.assertEqual(snap["supply"]["stalls"], 0, "the seek before the first byte is not a stall")

    async def test_a_start_that_waited_for_the_decoder_still_lands_on_the_timeline(self):
        self.spawn_args = SLOW  # 0.6 s before the first byte, like a fresh ffmpeg seeking
        bridge.START_TIMEOUT_S = 0.2
        pos_at = time.monotonic_ns() // 1000
        resp = await self.start(7, start_s=40.0, pos_at_us=pos_at)
        self.assertEqual(resp.status, 202, resp.data)
        snap = await self.wait_live()
        p = bridge.STATE["pusher"]
        self.assertAlmostEqual(p.t0_us, pos_at - 40_000_000, delta=1_000, msg="t0 is the caller's, untouched")
        self.assertEqual(snap["t0_us"], p.t0_us)
        # The first chunk was pinned no earlier than a send-ahead from when it was committed,
        # i.e. the film seconds that passed while waiting were skipped, not played late.
        pinned = p.stream.pinned[0]
        self.assertGreaterEqual(pinned - p.t0_us, 40_000_000 + 600_000)
        self.assertGreater((pinned - p.t0_us) / 1e6, 40.6 + 0.5, "the 0.6 s decoder wait was skipped")

    async def test_reconnect_keeps_the_cache(self):
        await self.prepare()
        dec = await self.wait_cache(5.0)
        bridge.ACTIVE_URL = "ws://other:8928/sendspin"
        async with bridge._lock:
            pass
        await bridge._disconnect_active(keep_cache=True)
        self.assertIs(bridge.STATE["decoder"], dec)
        self.assertTrue(dec.alive)
        self.assertEqual(self.kills, [])
        await bridge._disconnect_active()
        self.assertIsNone(bridge.STATE["decoder"])
        self.assertEqual(self.kills, [dec.marker])

    async def test_a_live_trim_moves_the_sound_with_no_gap(self):
        await self.prepare()
        await self.wait_cache(20.0)
        await self.start(1, start_s=10.0)
        await self.wait_live()
        p = bridge.STATE["pusher"]
        self.assertEqual(bridge.status_snapshot()["delay_ms"], 0)
        t0_before = p.t0_us
        queue_end_before = p.next_play_us
        resp = await bridge.handle_delay(FakeRequest({"ms": 200}))
        self.assertEqual(resp.data, {"delay_ms": 200})
        deadline = time.monotonic() + 3.0
        while p.delay_us != 200_000 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertEqual(p.delay_us, 200_000, "the push never picked the trim up")
        # The sound moved by exactly the trim, and nothing else did.
        self.assertEqual(p.t0_us, t0_before + 200_000)
        self.assertIs(bridge.STATE["decoder"], self.dec_of(p))
        self.assertEqual(self.spawned, 1, "a trim is not a restart")
        self.assertEqual(self.kills, [])
        # Re-pinned at the end of what was already queued: no gap, no overlap.
        self.assertEqual(len(p.stream.pinned), 2)
        self.assertGreaterEqual(p.stream.pinned[1], queue_end_before)
        self.assertLess(p.stream.pinned[1] - queue_end_before, 200_000)
        snap = bridge.status_snapshot()
        self.assertTrue(snap["streaming"])
        self.assertEqual(snap["delay_ms"], 200)
        self.assertEqual(snap["t0_us"], p.t0_us)
        # And back the other way, from a value that is already set.
        await bridge.handle_delay(FakeRequest({"ms": -50}))
        deadline = time.monotonic() + 3.0
        while p.delay_us != -50_000 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertEqual(p.t0_us, t0_before - 50_000)
        self.assertEqual(len(p.stream.pinned), 3)

    async def test_the_trim_is_clamped_and_survives_into_the_next_start(self):
        await bridge.handle_delay(FakeRequest({"ms": 99_000}))
        self.assertEqual(bridge.STATE["delay_us"], bridge.DELAY_MAX_US)
        resp = await bridge.handle_delay(FakeRequest({"ms": -99_000}))
        self.assertEqual(resp.data["delay_ms"], bridge.DELAY_MIN_US // 1000)
        await bridge.handle_delay(FakeRequest({"ms": 120}))
        await self.prepare()
        await self.wait_cache(15.0)
        pos_at = time.monotonic_ns() // 1000
        resp = await self.start(1, start_s=10.0, pos_at_us=pos_at)
        self.assertEqual(resp.status, 200, resp.data)
        # t0 carries the trim from the first sample, so a reader of the
        # timeline sees where the sound really is.
        self.assertAlmostEqual(resp.data["t0_us"], pos_at - 10_000_000 + 120_000, delta=2_000)
        self.assertEqual(bridge.status_snapshot()["delay_ms"], 120)
        # A start may also carry it, for a bridge restarted mid-film.
        await bridge.handle_stop(FakeRequest())
        resp = await self.start(2, start_s=10.0, pos_at_us=pos_at, delay_ms=-300)
        self.assertAlmostEqual(resp.data["t0_us"], pos_at - 10_000_000 - 300_000, delta=2_000)
        self.assertEqual(bridge.status_snapshot()["delay_ms"], -300)

    async def test_track_end_stops_the_push_and_marks_the_cache_complete(self):
        self.spawn_args = SHORT
        await self.prepare()
        dec = bridge.STATE["decoder"]
        for _ in range(100):
            if dec.complete:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(dec.complete)
        self.assertEqual(dec.end_bytes, 9600 * 3)
        self.assertEqual(self.kills, [dec.marker], "cleanup runs once, on EOF")
        resp = await self.start(4, start_s=0.0, pos_at_us=time.monotonic_ns() // 1000 - 800_000)
        # 0.8 s already shown of a 0.15 s track: nothing left to play.
        self.assertEqual(resp.status, 503, resp.data)
        # And the player is not left holding the stream that start created.
        # Refusing while the group still says PLAYING is how a film that ran
        # out left the Sendspin client playing in Music Assistant.
        self.assertTrue(self.client.group.streams[-1].stopped)
        self.assertEqual(self.client.group.stops, 1)
        resp = await self.start(5, start_s=0.0, pos_at_us=time.monotonic_ns() // 1000 + 5_000_000)
        self.assertEqual(resp.status, 200, resp.data)
        p = bridge.STATE["pusher"]
        await self.wait_pusher_done(p)
        self.assertEqual(p.chunks, 3)
        # The push ended by itself, so nobody else will end the stream: the
        # player is told here or not at all.
        self.assertTrue(p.stream.stopped, "the track running out ends the stream")
        snap = bridge.status_snapshot()
        self.assertFalse(snap["streaming"])
        self.assertFalse(snap["pending"])
        self.assertFalse(snap["ffmpeg_alive"])
        self.assertTrue(snap["cache"]["complete"])
        await bridge.handle_stop(FakeRequest())
        self.assertEqual(self.kills, [dec.marker])

    async def test_a_decoder_that_fails_is_reported_not_waited_on(self):
        self.spawn_args = BROKEN
        await self.prepare()
        dec = bridge.STATE["decoder"]
        for _ in range(100):
            if dec.error:
                break
            await asyncio.sleep(0.02)
        self.assertIn("rc=1", dec.error)
        resp = await self.start(6, start_s=0.0)
        self.assertEqual(resp.status, 503)
        self.assertIn("rc=1", resp.data["error"])
        snap = bridge.status_snapshot()
        self.assertFalse(snap["pending"])
        self.assertFalse(snap["ffmpeg_alive"])
        self.assertEqual(snap["cache"]["error"], dec.error)

    async def test_a_disconnected_player_hides_the_timeline(self):
        await self.prepare()
        await self.wait_cache(15.0)
        await self.start(5)
        await self.wait_live()
        self.client.is_connected = False
        snap = bridge.status_snapshot()
        self.assertFalse(snap["connected"])
        self.assertFalse(snap["streaming"])
        self.assertIsNone(snap["t0_us"])
        self.assertTrue(snap["ffmpeg_alive"])

    async def test_start_refused_when_not_connected(self):
        self.client.is_connected = False
        resp = await self.start(6)
        self.assertEqual(resp.status, 503)
        self.assertIsNone(bridge.STATE["pusher"])

    async def test_players_stay_listed_until_zeroconf_removes_them(self):
        bridge.PLAYERS["ws://p:8928/sendspin"] = {"id": "ws://p:8928/sendspin", "name": "loftpi",
                                                 "url": "ws://p:8928/sendspin",
                                                 "seen_at": time.monotonic() - 3600,
                                                 "_service_name": "loftpi._sendspin._tcp.local."}
        resp = await bridge.handle_players(FakeRequest())
        self.assertEqual([p["name"] for p in resp.data["players"]], ["loftpi"])
        self.assertEqual(resp.data["default"], "ws://p:8928/sendspin", "the only player is the default")
        bridge._on_service_state_change(None, bridge.SENDSPIN_SERVICE_TYPE,
                                        "loftpi._sendspin._tcp.local.", "Removed")
        resp = await bridge.handle_players(FakeRequest())
        self.assertEqual(resp.data["players"], [])
        self.assertIsNone(resp.data["default"])


if __name__ == "__main__":
    unittest.main()
