"""The on-demand browser HLS packager's serving routes: /hls/<token>/....

No ffmpeg, no network, no docker. A fake session is built by hand: a temp
directory stands in for TC_HOST, an init.mp4 and a run of s%06d.m4s files
are written straight to disk with struct.pack (the same box builders
test_tfdt.py uses, imported from there rather than duplicated), and _bx is
populated the way bx_begin/bx_spawn would have left it. The real HTTP
server is driven over a real socket, the way test_proxy.py drives /src/ --
because the thing under test includes header handling (Range, keep-alive,
Content-Range) that an in-process call to do_GET would not exercise.

Rules under test:
- index.m3u8, init.mp4 and a complete segment are all served with the
  right content-type, body and headers;
- Range: bytes=100-199 on a segment yields 206 with an exact Content-Range
  and exactly 100 bytes;
- a segment with no successor file, while ffmpeg is still (fake-)alive,
  never comes back 200 -- the muxer has not closed it yet;
- an unknown token (wrong shape) and a token that no longer matches the
  live _bx session both 404 -- the token IS the route, and stale-tab
  isolation depends on that being absolute;
- a segment index at or past n_segs 404s;
- the tfdt actually stamped into a served segment is the RUN's anchor plus
  whatever that segment's own bytes had already counted since the run
  started, per track, at that track's own timescale -- read back
  independently of the server's own code via test_tfdt's box reader -- so
  the elapsed time is never counted twice and real (keyframe-driven)
  segment lengths are not forced onto the nominal grid;
- the track timescales are read from init.mp4 by the route itself, and a
  segment is 503 rather than 200 or 500 while that file cannot be read.

Run: python3 -m unittest discover -s server/tests -t server
"""

import http.client
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import server           # noqa: E402
import browser_play     # noqa: E402

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
import test_tfdt as tfdt   # noqa: E402 -- reuse its ISO-BMFF box builders


TOKEN = "a" * 32     # 32 lowercase hex chars, matches browser_play.RE_TOKEN
OTHER_TOKEN = "b" * 32
BAD_TOKEN = "not-a-token"

VIDEO_TRACK = 1
AUDIO_TRACK = 2
VIDEO_TS = 12288
AUDIO_TS = 44100


def _segment_bytes(k, internal_s=0.0):
    """One fake segment file: a two-track moof plus an mdat body long
    enough for the Range test to slice a meaningful chunk out of, and
    distinct per segment.

    internal_s is the time this segment's own bytes claim to start at, in
    seconds, converted to each track's own ticks. That is NOT the segment's
    position in the film: ffmpeg zeroes its clock at the keyframe the run
    seeked to, so the FIRST segment of a run carries 0 and each one after
    it carries the time elapsed since that keyframe -- which is exactly the
    distinction the run-anchor shift exists to get right, and exactly what
    a fixture that stamps 0 into every segment cannot express. _run_bytes()
    below builds a whole run's worth correctly.
    """
    moof = tfdt._moof(
        tfdt._traf(VIDEO_TRACK, 1, round(internal_s * VIDEO_TS)),
        tfdt._traf(AUDIO_TRACK, 1, round(internal_s * AUDIO_TS)))
    payload = ("SEG-%06d-" % k).encode() * 20   # comfortably over 200 bytes
    mdat = tfdt._box(b"mdat", payload)
    return moof + mdat


def _init_bytes():
    return tfdt._moov(tfdt._trak(VIDEO_TRACK, VIDEO_TS), tfdt._trak(AUDIO_TRACK, AUDIO_TS))


class _FakeProc:
    """Stands in for the Popen transcode_alive() inspects: poll() is None
    for as long as the fake ffmpeg is "running"."""

    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


class _Live:
    """A real Cinematica server on a real loopback port, the way
    test_proxy.py's _Live drives /src/ -- the routes under test do their
    own header and range handling, which an in-process do_GET call would
    not exercise the same way a real socket does.
    """

    def __enter__(self):
        self.app = server.Server(("127.0.0.1", 0), server.H)
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.app.server_address[1]
        return self

    def __exit__(self, *exc):
        self.app.shutdown()
        self.app.server_close()
        self.thread.join(timeout=5)
        return False

    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("GET", path, headers=headers or {})
            r = c.getresponse()
            body = r.read()
            return r, body
        finally:
            c.close()


class BxServeTest(unittest.TestCase):
    def setUp(self):
        self._orig_tc_host = server.TC_HOST
        self._orig_bx = dict(server._bx)
        self._orig_seg_wait = server.BX_SEG_WAIT
        # The long-poll in the segment route waits up to BX_SEG_WAIT before
        # giving up -- shrink it so the "never comes back 200" test does not
        # take 45 real seconds.
        server.BX_SEG_WAIT = 0.3
        self.tmp = tempfile.mkdtemp(prefix="bx-serve-test-")
        server.TC_HOST = self.tmp

    def tearDown(self):
        server.TC_HOST = self._orig_tc_host
        with server._lock:
            server._bx.clear()
            server._bx.update(self._orig_bx)
        server.BX_SEG_WAIT = self._orig_seg_wait
        with server._tc_lock:
            server._transcodes.pop("bx:" + TOKEN, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session_dir(self, token=TOKEN):
        return os.path.join(self.tmp, server.BX_DIR + token)

    def _make_session(self, n_complete=3, seg=6.0, n_segs=20, anchor=0,
                      token=TOKEN, dur=None, run_anchor=None, timescales=None):
        """Write init.mp4 plus n_complete+1 segment files, starting at
        segment `anchor` -- the run's own first segment -- so that segments
        anchor..anchor+n_complete-1 each have a successor on disk and are
        complete, and the last one written has none and is the one still
        (notionally) being written. Populates _bx to match.

        Each segment's bytes are stamped the way the run that wrote them
        would have stamped them: zero for the run's first segment, then one
        segment length per segment after it.

        run_anchor defaults to the grid position of `anchor`, which is what
        bx_spawn records when the probe finds a keyframe sitting exactly on
        the grid (and always, for anchor 0). Pass it explicitly to model the
        ordinary case, where the keyframe is somewhat before that.

        timescales defaults to None -- NOT to the parsed init.mp4 -- so that
        the default session is the one production actually starts from, with
        the route left to read init.mp4 itself.
        """
        sess_dir = self._session_dir(token)
        os.makedirs(sess_dir, exist_ok=True)
        init_bytes = _init_bytes()
        with open(os.path.join(sess_dir, "init.mp4"), "wb") as f:
            f.write(init_bytes)
        for i in range(n_complete + 1):
            k = anchor + i
            with open(os.path.join(sess_dir, "s%06d.m4s" % k), "wb") as f:
                f.write(_segment_bytes(k, internal_s=i * seg))
        with server._lock:
            server._bx.update(
                token=token, job=None, gen=0, at=time.time(), state="playing",
                pos=0.0, dur=dur if dur is not None else n_segs * seg, title=None,
                dir=sess_dir, seg=seg, anchor=anchor,
                frontier=anchor + n_complete - 1,
                seek_gen=0, pending_anchor=None, timescales=timescales,
                run_anchor=(anchor * seg if run_anchor is None else run_anchor),
                n_segs=n_segs, proc_key="bx:" + token,
                src="http://internal/fake-src", plan={"acodec": "copy", "aidx": 0},
            )
        return sess_dir, browser_play.track_timescales(init_bytes)

    # ---- playlist -------------------------------------------------------

    def test_index_playlist_content_type_and_body(self):
        self._make_session(seg=6.0, n_segs=10, dur=60.0)
        with _Live() as live:
            r, body = live.get("/hls/%s/index.m3u8" % TOKEN)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.getheader("Content-Type"), "application/vnd.apple.mpegurl")
            self.assertEqual(r.getheader("Cache-Control"), "no-store")
            text = body.decode("utf-8")
            self.assertTrue(text.startswith("#EXTM3U"))
            self.assertIn("#EXT-X-ENDLIST", text)
            self.assertIn('#EXT-X-MAP:URI="init.mp4"', text)

    # ---- init.mp4 ---------------------------------------------------------

    def test_init_mp4_served(self):
        sess_dir, _ = self._make_session()
        with open(os.path.join(sess_dir, "init.mp4"), "rb") as f:
            expected = f.read()
        with _Live() as live:
            r, body = live.get("/hls/%s/init.mp4" % TOKEN)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.getheader("Content-Type"), "video/mp4")
            self.assertEqual(body, expected)

    # ---- a complete segment -----------------------------------------------

    def test_complete_segment_served_200_with_right_length(self):
        sess_dir, _ = self._make_session(n_complete=3)
        with open(os.path.join(sess_dir, "s%06d.m4s" % 1), "rb") as f:
            raw_len = len(f.read())
        with _Live() as live:
            r, body = live.get("/hls/%s/s000001.m4s" % TOKEN)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.getheader("Content-Type"), "video/mp4")
            self.assertEqual(r.getheader("Accept-Ranges"), "bytes")
            self.assertEqual(int(r.getheader("Content-Length")), raw_len)
            self.assertEqual(len(body), raw_len)
            # Segments are many and small -- unlike /audio/, the connection
            # must be left open rather than closed after each one.
            self.assertIsNone(r.getheader("Connection"))

    def test_range_request_on_a_segment(self):
        self._make_session(n_complete=3)
        with _Live() as live:
            r, body = live.get("/hls/%s/s000001.m4s" % TOKEN,
                               headers={"Range": "bytes=100-199"})
            self.assertEqual(r.status, 206)
            total = int(r.getheader("Content-Range").split("/")[-1])
            self.assertEqual(r.getheader("Content-Range"), "bytes 100-199/%d" % total)
            self.assertEqual(len(body), 100)
            self.assertEqual(int(r.getheader("Content-Length")), 100)

    def _served_tfdts(self, k):
        with _Live() as live:
            r, body = live.get("/hls/%s/s%06d.m4s" % (TOKEN, k))
            self.assertEqual(r.status, 200)
        return {track_id: base for track_id, version, base in tfdt._read_tfdts(body)}

    def test_segment_tfdt_is_the_run_anchor_plus_its_own_elapsed_time(self):
        # A run anchored at segment 10 on a 6s grid, whose keyframe was
        # 1.8s before the grid point -- the ordinary case, since a copied
        # video can only be cut on a keyframe and one rarely lands exactly
        # on a grid line.
        seg, k0, run_anchor = 6.0, 10, 58.2
        self._make_session(n_complete=3, seg=seg, anchor=k0, run_anchor=run_anchor)
        k = k0 + 1
        got = self._served_tfdts(k)
        # Segment 11 is the second of the run, so its own bytes count 6s
        # since the run's keyframe: 58.2 + 6.0.
        self.assertEqual(got[VIDEO_TRACK], round((run_anchor + seg) * VIDEO_TS))
        self.assertEqual(got[AUDIO_TRACK], round((run_anchor + seg) * AUDIO_TS))

    def test_a_runs_elapsed_time_is_never_counted_twice(self):
        # BUG the route used to add seg_start(k, seg) -- the segment's grid
        # position -- to bytes that were ALREADY counting up from the run's
        # start, so the elapsed time went in twice and the error grew with
        # k. On the unseeked run below, segment 1 holds the 6s mark and was
        # served stamped 12s. Asserting the right answer is not enough here:
        # the old answer has to be named, because the fixture this test file
        # used to carry (tfdt 0 in every segment) made the two agree.
        seg = 6.0
        self._make_session(n_complete=3, seg=seg, anchor=0, run_anchor=0.0)
        k = 1
        got = self._served_tfdts(k)
        self.assertEqual(got[VIDEO_TRACK], round(k * seg * VIDEO_TS))
        double_counted = round(2 * k * seg * VIDEO_TS)
        self.assertNotEqual(got[VIDEO_TRACK], double_counted)

    def test_real_segment_lengths_survive_the_shift(self):
        # Video is copied, so ffmpeg cuts at keyframes and a segment is
        # rarely exactly `seg` long: here 5.4s then 6.4s against a 6s grid.
        # Shifting the whole run by one constant preserves those real
        # boundaries, which is what keeps playback gapless. Stamping each
        # segment at its grid position instead -- k*seg -- would have
        # silently moved the third segment 0.2s from where its audio and
        # video actually are.
        seg, real = 6.0, [0.0, 5.4, 11.8]
        sess_dir, _ = self._make_session(n_complete=2, seg=seg, anchor=0,
                                         run_anchor=0.0)
        for k, internal in enumerate(real):
            with open(os.path.join(sess_dir, "s%06d.m4s" % k), "wb") as f:
                f.write(_segment_bytes(k, internal_s=internal))
        for k, internal in enumerate(real[:-1]):   # the last has no successor
            got = self._served_tfdts(k)
            self.assertEqual(got[VIDEO_TRACK], round(internal * VIDEO_TS))
            self.assertEqual(got[AUDIO_TRACK], round(internal * AUDIO_TS))

    # ---- timescales come from init.mp4, not from a field nothing writes ----

    def test_segment_served_when_bx_has_no_timescales_cached(self):
        # BUG _bx["timescales"] was set to None by bx_begin/bx_restart/
        # bx_stop_all and written by nothing else, so this guard was always
        # true in production and every segment request came back 503. Only
        # the tests ever populated it -- by hand -- which is why nothing
        # caught it. _make_session leaves it None on purpose now.
        self._make_session(n_complete=3)
        with server._lock:
            self.assertIsNone(server._bx["timescales"])
        got = self._served_tfdts(1)
        self.assertIn(VIDEO_TRACK, got)
        # ...and it is cached on _bx afterwards, read from the real file.
        with server._lock:
            self.assertEqual(server._bx["timescales"],
                             {VIDEO_TRACK: VIDEO_TS, AUDIO_TRACK: AUDIO_TS})

    def test_segment_is_503_not_500_while_init_mp4_is_unreadable(self):
        # No init.mp4 means no timescale, and a guessed timescale stamps a
        # plausible-looking wrong time -- so this must read as "not ready"
        # and be retried, never be served and never 500.
        sess_dir, _ = self._make_session(n_complete=3)
        os.remove(os.path.join(sess_dir, "init.mp4"))
        with _Live() as live:
            r, body = live.get("/hls/%s/s000001.m4s" % TOKEN)
            self.assertEqual(r.status, 503)
            self.assertEqual(r.getheader("Retry-After"), "2")

    # ---- incomplete segment -------------------------------------------------

    def test_segment_with_no_successor_and_live_process_never_returns_200(self):
        # Only segment k itself on disk, no successor -- and anchored so k
        # sits inside the "near the frontier" window, so the route just
        # long-polls (BX_SEG_WAIT, shrunk in setUp) instead of treating this
        # as a seek and trying to restart a (nonexistent, dockerless) ffmpeg.
        k = 3
        # n_complete=0 writes exactly one file, segment `anchor` itself, and
        # nothing after it -- so k is on disk with no successor, which is
        # the state this is about.
        self._make_session(n_complete=0, anchor=k, n_segs=20)
        with server._tc_lock:
            server._transcodes["bx:" + TOKEN] = {
                "proc": _FakeProc(alive=True), "name": server.BX_DIR + TOKEN,
                "at": time.time(), "log": None,
            }
        with _Live() as live:
            r, body = live.get("/hls/%s/s%06d.m4s" % (TOKEN, k))
            self.assertNotEqual(r.status, 200)
            self.assertEqual(r.status, 503)
            self.assertEqual(r.getheader("Retry-After"), "2")

    # ---- token isolation and bounds ----------------------------------------

    def test_malformed_token_is_404(self):
        self._make_session()
        with _Live() as live:
            r, body = live.get("/hls/%s/index.m3u8" % BAD_TOKEN)
            self.assertEqual(r.status, 404)

    def test_token_not_matching_live_session_is_404(self):
        self._make_session(token=TOKEN)
        with _Live() as live:
            # OTHER_TOKEN is a well-formed token, but it is not the live
            # session's -- exactly the stale-tab case the route exists to
            # refuse before it ever touches the filesystem.
            r, body = live.get("/hls/%s/index.m3u8" % OTHER_TOKEN)
            self.assertEqual(r.status, 404)

    def test_segment_index_at_or_past_n_segs_is_404(self):
        self._make_session(n_complete=3, n_segs=5)
        with _Live() as live:
            r, body = live.get("/hls/%s/s%06d.m4s" % (TOKEN, 5))
            self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
