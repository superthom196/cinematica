"""browser_play's segment grid, VOD playlist text, and the tfdt reader that
checks a produced segment actually starts where the grid says it should.

Rules under test:
- grid() clamps the segment length into [4, 12] seconds from the GOP;
- vod_playlist() covers the whole duration exactly (EXTINF sums to the
  real duration, no zero-length trailing segment on an exact multiple),
  and is None for a falsy/non-positive duration;
- seg_start()/seg_index() round-trip on the fixed grid;
- anchor_time() picks the last keyframe at or before a run's grid point,
  and falls back to the grid point itself when the probe found nothing;
- tfdt_of() reads a hand-built moof/traf/tfdt fixture and never raises on
  a truncated file;
- segment_cmd() always copies video and never names a video encoder, never
  emits -copyts, and threads k0/seg/aidx/plan through to the right flags.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import browser_play  # noqa: E402


def _box(box_type, body):
    """Pack one ISO-BMFF box: 4-byte size, 4-byte type, then the body."""
    return struct.pack(">I4s", 8 + len(body), box_type) + body


def _tfdt_body(version, base):
    if version == 0:
        # version(1) + flags(3), then a 32-bit baseMediaDecodeTime.
        return b"\x00\x00\x00\x00" + struct.pack(">I", base)
    # version(1) + flags(3), then a 64-bit baseMediaDecodeTime.
    return b"\x01\x00\x00\x00" + struct.pack(">Q", base)


def _moof_with_tfdt(version, base):
    tfdt = _box(b"tfdt", _tfdt_body(version, base))
    traf = _box(b"traf", tfdt)
    return _box(b"moof", traf)


def _write_temp(data):
    fd, path = tempfile.mkstemp(prefix="browser_play_test_")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


class GridTest(unittest.TestCase):
    def test_gop_clamped_up_to_minimum(self):
        # 2.0 * 1.5 = 3.0, below the 4.0 floor.
        seg, _ = browser_play.grid(100.0, 2.0)
        self.assertEqual(seg, 4.0)

    def test_huge_gop_clamped_to_maximum(self):
        seg, _ = browser_play.grid(100.0, 1000.0)
        self.assertEqual(seg, 12.0)

    def test_missing_gop_defaults_to_six(self):
        seg, _ = browser_play.grid(100.0, None)
        self.assertEqual(seg, 6.0)

    def test_zero_or_negative_gop_defaults_to_six(self):
        seg, _ = browser_play.grid(100.0, 0)
        self.assertEqual(seg, 6.0)
        seg, _ = browser_play.grid(100.0, -5.0)
        self.assertEqual(seg, 6.0)

    def test_n_segs_matches_ceil_of_duration_over_seg(self):
        seg, n = browser_play.grid(61.0, None)  # seg = 6.0
        self.assertEqual(n, 11)  # ceil(61/6) = 11


class VodPlaylistTest(unittest.TestCase):
    def _extinfs(self, text):
        return [
            float(line.split(":", 1)[1].rstrip(","))
            for line in text.splitlines()
            if line.startswith("#EXTINF:")
        ]

    def test_full_shape_and_duration_coverage(self):
        duration = 7385.2
        seg = 6.0
        text = browser_play.vod_playlist(duration, seg)
        self.assertIn("#EXT-X-VERSION:7", text)
        self.assertIn("#EXT-X-TARGETDURATION:6", text)
        self.assertIn('#EXT-X-MAP:URI="init.mp4"', text)
        self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", text)
        self.assertIn("#EXT-X-ENDLIST", text)

        extinfs = self._extinfs(text)
        seg_lines = [l for l in text.splitlines() if l.startswith("s0")]
        self.assertEqual(len(seg_lines), len(extinfs))

        import math
        expected_n = math.ceil(duration / seg)
        self.assertEqual(len(extinfs), expected_n)
        self.assertAlmostEqual(sum(extinfs), duration, delta=0.001)

    def test_exact_multiple_has_no_zero_length_trailing_segment(self):
        text = browser_play.vod_playlist(12.0, 6.0)
        extinfs = self._extinfs(text)
        self.assertEqual(len(extinfs), 2)
        self.assertTrue(all(v > 0 for v in extinfs))
        self.assertAlmostEqual(sum(extinfs), 12.0, delta=0.001)

    def test_duration_shorter_than_one_segment_has_one_extinf(self):
        text = browser_play.vod_playlist(3.0, 6.0)
        extinfs = self._extinfs(text)
        self.assertEqual(len(extinfs), 1)
        self.assertAlmostEqual(extinfs[0], 3.0, delta=0.001)

    def test_none_duration_returns_none(self):
        self.assertIsNone(browser_play.vod_playlist(None, 6.0))

    def test_zero_duration_returns_none(self):
        self.assertIsNone(browser_play.vod_playlist(0, 6.0))


class SegGridRoundTripTest(unittest.TestCase):
    def test_seg_index_of_seg_start_round_trips(self):
        seg = 6.0
        for k in range(0, 500):
            t = browser_play.seg_start(k, seg)
            self.assertEqual(browser_play.seg_index(t, seg), k)


class AnchorTimeTest(unittest.TestCase):
    """anchor_time() answers "where does this run's clock actually start",
    which is the number every segment of that run is shifted by."""

    def test_picks_the_last_keyframe_at_or_before_the_grid_point(self):
        # Keyframes every 3.5s; the run is anchored at segment 10 of a 6s
        # grid, so 60.0s -- and the last keyframe before that is 59.5s.
        kfs = [round(i * 3.5, 3) for i in range(30)]
        self.assertAlmostEqual(browser_play.anchor_time(kfs, 10, 6.0), 59.5, places=3)

    def test_a_keyframe_exactly_on_the_grid_point_is_the_anchor(self):
        self.assertEqual(browser_play.anchor_time([54.0, 60.0, 66.0], 10, 6.0), 60.0)

    def test_keyframes_after_the_grid_point_are_ignored(self):
        self.assertEqual(browser_play.anchor_time([61.0, 70.0, 12.5], 10, 6.0), 12.5)

    def test_unsorted_input_is_fine(self):
        self.assertEqual(browser_play.anchor_time([70.0, 57.6, 12.5, 61.0], 10, 6.0), 57.6)

    def test_falls_back_to_the_grid_point_with_no_usable_keyframes(self):
        # Probe failed, or found only keyframes past the point: the old
        # assumption (the grid position) is the safe answer, wrong by at
        # most one GOP rather than arbitrarily.
        self.assertEqual(browser_play.anchor_time([], 10, 6.0), 60.0)
        self.assertEqual(browser_play.anchor_time(None, 10, 6.0), 60.0)
        self.assertEqual(browser_play.anchor_time([61.0, 62.0], 10, 6.0), 60.0)

    def test_segment_zero_is_always_the_start_of_the_film(self):
        self.assertEqual(browser_play.anchor_time([], 0, 6.0), 0.0)
        self.assertEqual(browser_play.anchor_time([0.0, 2.4], 0, 6.0), 0.0)

    def test_a_keyframe_a_hair_past_the_grid_point_still_counts(self):
        # csv float output can put the keyframe ffmpeg seeks to a rounding
        # error on the wrong side of the line.
        self.assertEqual(browser_play.anchor_time([60.0005], 10, 6.0), 60.0005)


class KeyframeProbeCmdTest(unittest.TestCase):
    def test_interval_end_is_absolute_not_a_duration(self):
        argv = browser_play.keyframe_probe_cmd("/src.mp4", 10.0, 40.0)
        interval = argv[argv.index("-read_intervals") + 1]
        start, end = interval.split("%")
        self.assertEqual(float(start), 10.0)
        # NOT "10%+30". ffprobe measures a "+" duration from wherever its
        # seek actually landed -- the keyframe at or before the start -- so
        # the window finishes early at precisely the end where the answer
        # is. An absolute end does not depend on where the seek landed.
        self.assertNotIn("+", interval)

    def test_interval_reaches_past_its_end_so_the_endpoint_is_included(self):
        # ffprobe's interval is exclusive of the end timestamp, and a
        # keyframe sitting exactly ON the seek point is the common case
        # whenever the GOP divides the grid. Asking for a little more is
        # what makes it visible; anchor_time() drops the excess.
        argv = browser_play.keyframe_probe_cmd("/src.mp4", 10.0, 40.0)
        _, end = argv[argv.index("-read_intervals") + 1].split("%")
        self.assertGreater(float(end), 40.0)
        # ...but not so far that it reaches the NEXT grid point and starts
        # reading material no run anchored here will ever use.
        self.assertLess(float(end), 41.0)

    def test_probes_only_keyframes_of_the_first_video_stream(self):
        argv = browser_play.keyframe_probe_cmd("/src.mp4", 0.0, 12.0)
        self.assertIn("-skip_frame", argv)
        self.assertEqual(argv[argv.index("-skip_frame") + 1], "nokey")
        self.assertEqual(argv[argv.index("-select_streams") + 1], "v:0")
        self.assertEqual(argv[-1], "/src.mp4")


class FragmentsOnlyTest(unittest.TestCase):
    """fragments_only() is what lets several segment files be served as one
    HLS segment without leaving a styp and a pair of sidx boxes stranded in
    the middle of it."""

    def _segment(self, base):
        styp = _box(b"styp", b"msdh" + b"\x00" * 4 + b"msdh")
        moof = _box(b"moof", _box(b"traf", _box(b"tfdt", _tfdt_body(1, base))))
        mdat = _box(b"mdat", b"payload")
        return styp + moof + mdat, moof + mdat

    def test_drops_everything_before_the_first_moof(self):
        whole, fragment = self._segment(1000)
        self.assertEqual(browser_play.fragments_only(whole), fragment)

    def test_keeps_every_fragment_when_there_are_several(self):
        a_whole, a_frag = self._segment(0)
        b_whole, b_frag = self._segment(9000)
        joined = a_whole + b_frag                # one header, two fragments
        self.assertEqual(browser_play.fragments_only(joined), a_frag + b_frag)

    def test_a_file_with_no_fragment_contributes_nothing(self):
        # Better to append nothing than to append bytes that corrupt the
        # segment they are being added to.
        self.assertEqual(browser_play.fragments_only(_box(b"styp", b"msdh")), b"")
        self.assertEqual(browser_play.fragments_only(b""), b"")

    def test_a_truncated_box_contributes_nothing(self):
        self.assertEqual(browser_play.fragments_only(b"\x00\x00\x00\x40styp"), b"")


class TfdtOfTest(unittest.TestCase):
    def test_version0_tfdt(self):
        data = _moof_with_tfdt(0, 90000)  # 90000 ticks
        path = _write_temp(data)
        try:
            secs = browser_play.tfdt_of(path, timescale=90000)
            self.assertAlmostEqual(secs, 1.0, delta=1e-9)
        finally:
            os.unlink(path)

    def test_version1_tfdt(self):
        # A 64-bit base value that would overflow a 32-bit field, to prove
        # the version-1 branch is actually the one being exercised.
        base = (1 << 32) + 45000
        data = _moof_with_tfdt(1, base)
        path = _write_temp(data)
        try:
            secs = browser_play.tfdt_of(path, timescale=90000)
            self.assertAlmostEqual(secs, base / 90000.0, delta=1e-6)
        finally:
            os.unlink(path)

    def test_truncated_file_returns_none(self):
        data = _moof_with_tfdt(0, 90000)
        path = _write_temp(data[:-3])  # cut off mid-tfdt-body
        try:
            self.assertIsNone(browser_play.tfdt_of(path, timescale=90000))
        finally:
            os.unlink(path)

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            browser_play.tfdt_of("/nonexistent/path/nope.mp4", timescale=1),
        )

    def test_no_moof_returns_none(self):
        path = _write_temp(_box(b"free", b"\x00" * 4))
        try:
            self.assertIsNone(browser_play.tfdt_of(path, timescale=90000))
        finally:
            os.unlink(path)

    def test_missing_timescale_and_no_moov_returns_none(self):
        # A bare fragment with no moov and no explicit timescale can't be
        # converted to seconds, so this must come back None, not raise.
        data = _moof_with_tfdt(0, 90000)
        path = _write_temp(data)
        try:
            self.assertIsNone(browser_play.tfdt_of(path))
        finally:
            os.unlink(path)


class SegmentCmdTest(unittest.TestCase):
    """segment_cmd() is bx_spawn's argv construction, pulled out into pure
    code so these assertions can run without ffmpeg or docker.
    """

    def _cmd(self, plan, aidx=0, k0=0, seg=6.0):
        return browser_play.segment_cmd(
            "/src/movie.mkv", "/transcode/bx_tok", "/transcode/bx_tok/ff.m3u8",
            k0, seg, plan, aidx)

    def test_never_emits_a_video_encoder_this_guards_against_reencoding(self):
        # The standing guarantee this whole refactor exists to protect:
        # there is deliberately no video transcoding in this version, and
        # "fixing" a browser-compatibility complaint by adding a video
        # encoder here must fail this test, loudly, by name.
        banned = ("libx264", "libx265", "h264", "hevc",
                  "libvpx", "libaom", "libsvtav1")
        for acodec in ("copy", "aac"):
            cmd = self._cmd({"acodec": acodec})
            for arg in cmd:
                for enc in banned:
                    self.assertNotIn(
                        enc, arg,
                        "video encoder %r found in argv (acodec=%s) -- "
                        "this version must NEVER transcode video, only "
                        "copy it; see segment_cmd's docstring" % (enc, acodec))
            self.assertIn("-c:v", cmd)
            self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
            # Exactly one -c:v, so nothing later in the argv could smuggle
            # in a second, overriding value.
            self.assertEqual(cmd.count("-c:v"), 1)

    def test_copyts_never_appears(self):
        # -copyts would bake the anchor offset into init.mp4's moov edit
        # list, breaking the single shared init this design depends on --
        # see segment_cmd's docstring for the full reasoning.
        cmd = self._cmd({"acodec": "copy"})
        self.assertNotIn(
            "-copyts", cmd,
            "-copyts must never appear -- it would make init.mp4 differ "
            "per anchor, breaking the shared init.mp4 this design needs")

    def test_vtag_present_when_plan_says_hvc1(self):
        cmd = self._cmd({"acodec": "copy", "vtag": "hvc1"})
        self.assertIn("-tag:v", cmd)
        self.assertEqual(cmd[cmd.index("-tag:v") + 1], "hvc1")

    def test_vtag_absent_when_plan_has_no_vtag(self):
        cmd = self._cmd({"acodec": "copy"})
        self.assertNotIn("-tag:v", cmd)
        cmd = self._cmd({"acodec": "copy", "vtag": None})
        self.assertNotIn("-tag:v", cmd)

    def test_audio_copy(self):
        cmd = self._cmd({"acodec": "copy"})
        self.assertIn("-c:a", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_audio_aac_transcode(self):
        cmd = self._cmd({"acodec": "aac"})
        i = cmd.index("-c:a")
        self.assertEqual(
            cmd[i:i + 8],
            ["-c:a", "aac", "-b:a", "256k", "-ac", "2", "-ar", "48000"])

    def test_ss_and_start_number_track_k0(self):
        for k0 in (0, 1, 5, 100):
            cmd = self._cmd({"acodec": "copy"}, k0=k0, seg=6.0)
            self.assertEqual(cmd[cmd.index("-ss") + 1], str(k0 * 6.0))
            self.assertEqual(cmd[cmd.index("-start_number") + 1], str(k0))

    def test_map_uses_passed_aidx_not_hardcoded_zero(self):
        cmd = self._cmd({"acodec": "copy"}, aidx=3)
        self.assertIn("0:a:3", cmd)
        self.assertNotIn("0:a:0", cmd)

    def test_segment_filename_under_out_dir_and_playlist_is_last(self):
        cmd = browser_play.segment_cmd(
            "/src/movie.mkv", "/transcode/bx_tok", "/transcode/bx_tok/ff.m3u8",
            0, 6.0, {"acodec": "copy"}, 0)
        i = cmd.index("-hls_segment_filename")
        self.assertEqual(cmd[i + 1], "/transcode/bx_tok/s%06d.m4s")
        self.assertEqual(cmd[-1], "/transcode/bx_tok/ff.m3u8")


if __name__ == "__main__":
    unittest.main()
