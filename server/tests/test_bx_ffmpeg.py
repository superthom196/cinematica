"""The browser packager against a REAL ffmpeg, end to end.

Everything else that covers this path builds its fMP4 boxes by hand. That
is the right way to test the box patcher -- and it is exactly why three
production bugs sat in this path with a full green suite over them:

- _bx["timescales"] was a field nothing ever wrote, so every segment
  request 503'd forever. The hand-built tests filled it in themselves.
- the route added the segment's grid position to bytes that were already
  counting up from the run's start, so the time was counted twice. The
  hand-built segments all carried tfdt 0, where counting twice and
  counting once give the same answer for the first segment of a run.
- the playlist assumed segments are exactly `seg` long, which a copied
  video stream cannot promise, because it can only be cut on a keyframe.
  Synthetic segments have no keyframes to be cut on.

None of those survive contact with a file ffmpeg actually wrote, so this
file makes one: a short clip with a keyframe interval deliberately chosen
NOT to divide the segment length, packaged by the real segment_cmd argv,
served over a real socket by the real server, and read back with
test_tfdt's independent box reader.

ffmpeg and ffprobe are found on PATH, or named by CINEMATICA_TEST_FFMPEG
and CINEMATICA_TEST_FFPROBE. With neither available the whole file skips,
so a checkout with no ffmpeg still runs the rest of the suite -- see
BROWSER-TESTING.md for how CI installs them.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import http.client

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import server           # noqa: E402
import browser_play     # noqa: E402

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
import test_tfdt as tfdt   # noqa: E402 -- its box readers, not its fixtures


TOKEN = "c" * 32

FPS = 25
SRC_SECONDS = 60

# The keyframe interval is the knob every bug in this path turns on, so it
# is per-class rather than fixed, and the two classes at the bottom of this
# file pick the two values that behave differently:
#
#   63 frames == 2.52s, against a 4s grid. Coprime-ish: after the first,
#   no keyframe lands on a grid line, so segment boundaries drift and a
#   seeked run starts somewhere other than k*seg.
#
#   50 frames == 2.00s, which DIVIDES the grid. Every grid point is a
#   keyframe, so a seek lands exactly on one -- the boundary that an
#   exclusive probe interval quietly got wrong.
#
# Only one of these is "adversarial" for any given bug, which is the whole
# reason both are here.
GOP_COPRIME = 63
GOP_ON_GRID = 50


def _tool(env_name, exe):
    return os.environ.get(env_name) or shutil.which(exe)


FFMPEG = _tool("CINEMATICA_TEST_FFMPEG", "ffmpeg")
FFPROBE = _tool("CINEMATICA_TEST_FFPROBE", "ffprobe")
HAVE_TOOLS = bool(FFMPEG and FFPROBE)


def _extinf_durations(playlist_path):
    """The real duration of every segment ffmpeg wrote, from its own
    playlist -- ffmpeg's accounting of what it actually cut, not ours."""
    out = []
    with open(playlist_path) as f:
        for line in f:
            m = re.match(r"^#EXTINF:([0-9.]+)", line.strip())
            if m:
                out.append(float(m.group(1)))
    return out


class _Live:
    """A real server on a real loopback port, as in test_bx_serve."""

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
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request("GET", path, headers=headers or {})
            r = c.getresponse()
            return r, r.read()
        finally:
            c.close()


class _Harness:
    """Encodes one source per class and drives the real packager against it.

    GOP_FRAMES is what a subclass varies; SEG is the grid the packager is
    told to use. One encode per class -- it is the only slow part, and no
    test modifies the source.
    """

    GOP_FRAMES = GOP_COPRIME
    SEG = 4.0

    @classmethod
    def setUpClass(cls):
        cls.GOP_SECONDS = cls.GOP_FRAMES / FPS
        cls.tmp = tempfile.mkdtemp(prefix="bx-ffmpeg-test-")
        cls.src = os.path.join(cls.tmp, "src.mp4")
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=%d" % FPS,
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
             "-t", str(SRC_SECONDS),
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-g", str(cls.GOP_FRAMES), "-keyint_min", str(cls.GOP_FRAMES),
             "-sc_threshold", "0",
             "-c:a", "aac", "-b:a", "128k", "-ac", "2",
             "-movflags", "+faststart", cls.src],
            check=True, capture_output=True, timeout=300)

    @property
    def n_segs(self):
        return browser_play.grid(SRC_SECONDS, self.GOP_SECONDS)[1]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._orig_tc_host = server.TC_HOST
        self._orig_bx = dict(server._bx)
        self.run_dir = tempfile.mkdtemp(prefix="bx-ffmpeg-run-")
        server.TC_HOST = self.run_dir
        self.sess_dir = os.path.join(self.run_dir, server.BX_DIR + TOKEN)
        os.makedirs(self.sess_dir, exist_ok=True)

    def tearDown(self):
        server.TC_HOST = self._orig_tc_host
        with server._lock:
            server._bx.clear()
            server._bx.update(self._orig_bx)
        shutil.rmtree(self.run_dir, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------

    def _package(self, k0, seg=None, aidx=0):
        """Run the REAL segment_cmd argv, minus the docker prefix the
        server prepends, and return (playlist_path, real_durations)."""
        seg = self.SEG if seg is None else seg
        playlist = os.path.join(self.sess_dir, "ff.m3u8")
        argv = [FFMPEG] + browser_play.segment_cmd(
            self.src, self.sess_dir, playlist, k0, seg,
            {"acodec": "copy"}, aidx)
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        return playlist, _extinf_durations(playlist)

    def _probe_keyframes(self, at, window=30.0):
        # start/end, both absolute -- the same shape probe_keyframes() uses.
        start = max(0.0, at - window)
        argv = [FFPROBE] + browser_play.keyframe_probe_cmd(self.src, start, at)
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return browser_play.parse_keyframe_times(r.stdout)

    def _raw_tfdts(self, k):
        with open(os.path.join(self.sess_dir, "s%06d.m4s" % k), "rb") as f:
            return {tid: base for tid, version, base in tfdt._read_tfdts(f.read())}

    def _install_session(self, k0, run_anchor, seg=None, timescales=None):
        """_bx exactly as bx_spawn leaves it -- timescales None, because
        that is what production has: nothing writes it at spawn time."""
        seg = self.SEG if seg is None else seg
        with server._lock:
            server._bx.update(
                token=TOKEN, job=None, gen=0, at=0.0, state="playing",
                pos=0.0, dur=float(SRC_SECONDS), title=None,
                dir=self.sess_dir, seg=seg, anchor=k0, frontier=None,
                seek_gen=0, pending_anchor=None, timescales=timescales,
                run_anchor=run_anchor,
                n_segs=self.n_segs,
                proc_key="bx:" + TOKEN, src=self.src,
                plan={"acodec": "copy", "aidx": 0})


class _PackagerTests:
    """The checks that need a GOP which does NOT divide the grid."""

    # ---- what ffmpeg actually writes ---------------------------------------

    def test_init_mp4_yields_real_track_timescales(self):
        self._package(k0=0)
        with open(os.path.join(self.sess_dir, "init.mp4"), "rb") as f:
            ts = browser_play.track_timescales(f.read())
        # Two tracks, each with a positive timescale, parsed out of a moov
        # this repo did not build.
        self.assertEqual(len(ts), 2, ts)
        for track_id, timescale in ts.items():
            self.assertGreater(timescale, 0)
        # Video and audio really do differ here, which is the whole reason
        # deltas_for() works per track rather than applying one shift.
        self.assertEqual(len(set(ts.values())), 2, ts)

    def test_segments_already_count_up_within_a_run(self):
        # The premise the old serve-time arithmetic got wrong. Segment 1 of
        # an unseeked run does NOT start its own clock at zero -- it starts
        # at one segment's worth of ticks, because ffmpeg counts
        # continuously for as long as the run lasts. Adding the grid
        # position on top of this is what produced 12s for the 6s mark.
        _, durations = self._package(k0=0)
        with open(os.path.join(self.sess_dir, "init.mp4"), "rb") as f:
            ts = browser_play.track_timescales(f.read())
        first = self._raw_tfdts(0)
        second = self._raw_tfdts(1)
        for track_id, timescale in ts.items():
            self.assertLess(first[track_id] / timescale, 0.1)
            self.assertAlmostEqual(second[track_id] / timescale,
                                   durations[0], delta=0.05)

    def test_real_segments_are_not_the_length_the_grid_promises(self):
        # -c:v copy can only cut on a keyframe, and the keyframes here are
        # 2.52s apart against a 4s grid. If this ever stops being true the
        # source above stopped being adversarial and the rest of this file
        # got weaker without failing.
        _, durations = self._package(k0=0)
        self.assertTrue(any(abs(d - self.SEG) > 0.1 for d in durations[:-1]),
                        "every segment came out exactly %.1fs: %r" % (self.SEG, durations))

    # ---- the anchor prediction, against what ffmpeg really did -------------

    def test_anchor_time_predicts_where_a_seeked_run_really_starts(self):
        k0 = 3
        grid_point = k0 * self.SEG                      # 12.0
        predicted = browser_play.anchor_time(self._probe_keyframes(grid_point),
                                             k0, self.SEG)
        _, durations = self._package(k0=k0)
        # ffmpeg copied from its chosen keyframe to the end of the file, so
        # what it produced is exactly the tail after that keyframe. That
        # makes the real start measurable without trusting the prediction.
        actual = SRC_SECONDS - sum(durations)
        self.assertAlmostEqual(predicted, actual, delta=0.05,
                               msg="predicted %r, ffmpeg really started at %r "
                                   "(durations %r)" % (predicted, actual, durations))
        # And it is genuinely before the grid point -- otherwise this test
        # would pass just as well against the old k*seg assumption.
        self.assertLess(predicted, grid_point - 0.1)

    def test_anchor_probe_window_is_not_cut_short_by_its_own_seek(self):
        # BUG the probe asked for "START%+DURATION", and ffprobe counts that
        # duration from where its seek actually landed -- the keyframe at or
        # before START -- so the window finished up to one GOP early, at the
        # exact end where the answer lives. This seek is deep enough that the
        # window does not clamp to 0, which is the only reason the first
        # version of this file did not catch it: at k0=3 the window starts at
        # max(0, 12-30) == 0 and there is no seek to be cut short by.
        k0 = 10
        grid_point = k0 * self.SEG                      # 40.0
        kfs = self._probe_keyframes(grid_point)    # window 30 -> starts at 10.0
        predicted = browser_play.anchor_time(kfs, k0, self.SEG)
        _, durations = self._package(k0=k0)
        actual = SRC_SECONDS - sum(durations)
        self.assertAlmostEqual(predicted, actual, delta=0.05,
                               msg="predicted %r, ffmpeg really started at %r"
                                   % (predicted, actual))
        # The keyframe immediately before the grid point must be in the
        # window at all -- that is the thing the old interval dropped.
        self.assertTrue(any(t > grid_point - self.GOP_SECONDS - 0.01 and t <= grid_point
                            for t in kfs),
                        "no keyframe within one GOP before %.2fs in %r"
                        % (grid_point, kfs))

    # ---- the end of the film ------------------------------------------------

    def test_a_seeked_run_writes_past_the_last_playlist_slot(self):
        # The premise of the fix below, stated against real output: a run
        # that starts before its grid point has more film left than the grid
        # budgeted slots for, so it numbers files past the end of the
        # playlist. If ffmpeg ever stops doing this the tail handling is
        # dead weight and this says so.
        k0 = 10
        self._package(k0=k0)
        n_segs = self.n_segs
        written = sorted(fn for fn in os.listdir(self.sess_dir)
                         if fn.endswith(".m4s"))
        overflow = [fn for fn in written if int(fn[1:7]) >= n_segs]
        self.assertTrue(overflow,
                        "expected files past slot %d, got %r" % (n_segs - 1, written))

    def test_last_slot_carries_the_end_of_the_film(self):
        # BUG those overflow files were unreachable: the playlist never named
        # them and the route 404'd them, so the film ended early and silently
        # -- 2.04s short, on the clip this file builds.
        k0 = 10
        anchor = browser_play.anchor_time(self._probe_keyframes(k0 * self.SEG), k0, self.SEG)
        _, durations = self._package(k0=k0)
        self._install_session(k0=k0, run_anchor=anchor)
        n_segs = self.n_segs
        with open(os.path.join(self.sess_dir, "init.mp4"), "rb") as f:
            ts = browser_play.track_timescales(f.read())

        with _Live() as live:
            r, body = live.get("/hls/%s/s%06d.m4s" % (TOKEN, n_segs - 1))
            self.assertEqual(r.status, 200)

        # One fragment per file from the last slot to the end of the run.
        files_from_last_slot = len([fn for fn in os.listdir(self.sess_dir)
                                    if fn.endswith(".m4s")
                                    and int(fn[1:7]) >= n_segs - 1])
        self.assertGreater(files_from_last_slot, 1, "not the tail case")
        per_track = {}
        for track_id, version, base in tfdt._read_tfdts(body):
            per_track.setdefault(track_id, []).append(base)
        for track_id, bases in per_track.items():
            self.assertEqual(len(bases), files_from_last_slot,
                             "track %d: %d fragments for %d files"
                             % (track_id, len(bases), files_from_last_slot))
            # The last fragment starts where the final file really starts:
            # the whole film minus that file's own duration. Serving only
            # the slot's own file would have ended the film one file early.
            starts = [b / ts[track_id] for b in bases]
            self.assertEqual(starts, sorted(starts))
            self.assertAlmostEqual(starts[-1], SRC_SECONDS - durations[-1],
                                   delta=0.05)

    def test_concatenated_last_slot_is_one_segment_not_two_files(self):
        # Appending whole files would put a styp and a pair of sidx boxes in
        # the middle of a media segment. What must come back is one segment
        # header followed by several fragments.
        k0 = 10
        anchor = browser_play.anchor_time(self._probe_keyframes(k0 * self.SEG), k0, self.SEG)
        self._package(k0=k0)
        self._install_session(k0=k0, run_anchor=anchor)
        n_segs = self.n_segs
        with _Live() as live:
            r, body = live.get("/hls/%s/s%06d.m4s" % (TOKEN, n_segs - 1))
            self.assertEqual(r.status, 200)
        types = [bt.decode("latin1") for bt, c, e in tfdt._iter_boxes(body, 0, len(body))]
        self.assertEqual(types.count("styp"), 1, types)
        self.assertGreater(types.count("moof"), 1, types)
        self.assertEqual(types.count("moof"), types.count("mdat"), types)
        # Every sidx belongs to the leading fragment, before the first moof.
        self.assertTrue(all(i < types.index("moof")
                            for i, t in enumerate(types) if t == "sidx"), types)

    # ---- the served timeline ------------------------------------------------

    def test_served_segments_carry_an_absolute_gapless_timeline(self):
        k0 = 3
        anchor = browser_play.anchor_time(self._probe_keyframes(k0 * self.SEG), k0, self.SEG)
        _, durations = self._package(k0=k0)
        self._install_session(k0=k0, run_anchor=anchor)
        with open(os.path.join(self.sess_dir, "init.mp4"), "rb") as f:
            ts = browser_play.track_timescales(f.read())

        # Four segments is enough to catch a per-segment error that grows.
        ks = [k0 + i for i in range(4)]
        served = {}
        with _Live() as live:
            for k in ks:
                r, body = live.get("/hls/%s/s%06d.m4s" % (TOKEN, k))
                # A 503 here is the timescales bug: nothing populated the
                # field at spawn time, and _install_session left it None on
                # purpose, so the route has to read init.mp4 itself.
                self.assertEqual(r.status, 200, "segment %d -> %d" % (k, r.status))
                served[k] = {tid: base for tid, version, base
                             in tfdt._read_tfdts(body)}

        for track_id, timescale in ts.items():
            starts = [served[k][track_id] / timescale for k in ks]
            # 1. The run begins at its real anchor, not at k0*seg.
            self.assertAlmostEqual(starts[0], anchor, delta=0.05)
            # 2. Each segment begins where the previous one really ended --
            #    the real duration, not the nominal one. This is what breaks
            #    if segments are stamped onto the grid individually.
            for i, d in enumerate(durations[:len(ks) - 1]):
                self.assertAlmostEqual(starts[i + 1] - starts[i], d, delta=0.05,
                                       msg="track %d segment %d" % (track_id, ks[i]))
            # 3. Strictly increasing: no segment is stamped at or before its
            #    predecessor, which is what an overlap would look like.
            self.assertEqual(starts, sorted(starts))
            self.assertEqual(len(set(starts)), len(starts))

    def test_elapsed_time_is_not_added_twice_end_to_end(self):
        # The regression, stated in film seconds rather than ticks: the
        # segment holding the N-second mark must be served stamped N.
        self._package(k0=0)
        self._install_session(k0=0, run_anchor=0.0)
        with open(os.path.join(self.sess_dir, "init.mp4"), "rb") as f:
            ts = browser_play.track_timescales(f.read())
        raw = self._raw_tfdts(2)
        with _Live() as live:
            r, body = live.get("/hls/%s/s000002.m4s" % TOKEN)
            self.assertEqual(r.status, 200)
        served = {tid: base for tid, version, base in tfdt._read_tfdts(body)}
        for track_id, timescale in ts.items():
            # An unseeked run's anchor is zero, so the served time is the
            # time the bytes already carried -- unchanged, not doubled.
            self.assertEqual(served[track_id], raw[track_id])
            self.assertNotEqual(served[track_id],
                                raw[track_id] + round(2 * self.SEG * timescale))


@unittest.skipUnless(HAVE_TOOLS, "needs a real ffmpeg and ffprobe on PATH")
class BxFfmpegTest(_Harness, _PackagerTests, unittest.TestCase):
    """The coprime GOP: keyframes never line up with the grid."""
    GOP_FRAMES = GOP_COPRIME


@unittest.skipUnless(HAVE_TOOLS, "needs a real ffmpeg and ffprobe on PATH")
class BxFfmpegOnGridKeyframeTest(_Harness, unittest.TestCase):
    """The GOP that DIVIDES the grid, so a seek lands exactly on a keyframe.

    BUG this is the case the probe interval got wrong the second time.
    "START%END" is exclusive of END, so a keyframe sitting exactly on the
    seek point was never reported and the run was anchored a whole GOP too
    early: seeking to 40s on a 2s GOP predicted 38.0 while ffmpeg started at
    40.0, shifting every stamp 2s back and ending the film's timeline at 58s
    instead of 60s. A coprime GOP cannot express this -- no keyframe is ever
    ON a grid point there -- which is exactly why this class exists.
    """
    GOP_FRAMES = GOP_ON_GRID

    def test_the_grid_points_really_are_keyframes_here(self):
        # If this stops holding, everything below is testing the coprime
        # case again and the regression has nowhere left to be caught.
        grid_point = 10 * self.SEG                     # 40.0
        self.assertAlmostEqual(self.GOP_SECONDS, 2.0, places=6)
        kfs = self._probe_keyframes(grid_point)
        self.assertTrue(any(abs(t - grid_point) < 0.001 for t in kfs),
                        "no keyframe at %.2fs in %r" % (grid_point, kfs))

    def test_anchor_is_the_keyframe_on_the_seek_point_not_the_one_before(self):
        k0 = 10
        grid_point = k0 * self.SEG                     # 40.0
        predicted = browser_play.anchor_time(self._probe_keyframes(grid_point),
                                             k0, self.SEG)
        _, durations = self._package(k0=k0)
        actual = SRC_SECONDS - sum(durations)
        self.assertAlmostEqual(actual, grid_point, delta=0.05,
                               msg="ffmpeg started at %r, not the grid point" % actual)
        self.assertAlmostEqual(predicted, actual, delta=0.05,
                               msg="predicted %r, ffmpeg started at %r"
                                   % (predicted, actual))
        # Name the wrong answer: one GOP early is what an exclusive
        # interval produced, and it is close enough to look plausible.
        self.assertNotAlmostEqual(predicted, grid_point - self.GOP_SECONDS,
                                  delta=0.05)

    def test_served_timeline_reaches_the_end_of_the_film(self):
        # The visible symptom: stamps a GOP early mean the last segment
        # claims to start a GOP early too, and the film's timeline finishes
        # short of its real duration.
        k0 = 10
        anchor = browser_play.anchor_time(self._probe_keyframes(k0 * self.SEG),
                                          k0, self.SEG)
        _, durations = self._package(k0=k0)
        self._install_session(k0=k0, run_anchor=anchor)
        with open(os.path.join(self.sess_dir, "init.mp4"), "rb") as f:
            ts = browser_play.track_timescales(f.read())
        with _Live() as live:
            r, body = live.get("/hls/%s/s%06d.m4s" % (TOKEN, self.n_segs - 1))
            self.assertEqual(r.status, 200)
        per_track = {}
        for track_id, version, base in tfdt._read_tfdts(body):
            per_track.setdefault(track_id, []).append(base)
        for track_id, bases in per_track.items():
            last_start = max(bases) / ts[track_id]
            # The final fragment starts one segment before the end of the
            # film, so the timeline it hands the player runs out at the
            # film's real duration -- not two seconds short of it.
            self.assertAlmostEqual(last_start + durations[-1], SRC_SECONDS,
                                   delta=0.05,
                                   msg="track %d timeline ends at %.2f"
                                       % (track_id, last_start + durations[-1]))


if __name__ == "__main__":
    unittest.main()
