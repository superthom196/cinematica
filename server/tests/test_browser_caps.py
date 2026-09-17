"""browser_play's codec matcher: turning an ffprobe stream description and a
browser's isTypeSupported()/canPlayType() answers into a serving decision.

Rules under test:
- rfc6381_video/rfc6381_audio build the RFC 6381 strings the spec examples
  name, and never raise on a profile name we don't recognise;
- decide() picks direct/remux/audio/skip correctly across the MP4/MKV,
  H.264/HEVC/AV1 combinations real-world sources turn up in, and a
  level past what the browser has confirmed is refused even when the codec
  and profile are otherwise fine.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import browser_play  # noqa: E402


def make_caps(true_strings):
    """A caps dict as the page would send it, with only the named probe
    strings flipped True -- everything else behaves as False because
    decide() reads it with dict.get(), which is falsy on a missing key.
    """
    return {
        "mse": True, "nativeHls": True,
        "types": {s: True for s in true_strings},
    }


def h264(profile, level):
    return {"codec_name": "h264", "profile": profile, "level": level}


def hevc(profile, level):
    return {"codec_name": "hevc", "profile": profile, "level": level}


def av1(level, ten_bit=False):
    pix_fmt = "yuv420p10le" if ten_bit else "yuv420p"
    return {"codec_name": "av1", "level": level, "pix_fmt": pix_fmt}


def aac():
    return {"codec_name": "aac", "channels": 2}


def eac3():
    return {"codec_name": "eac3", "channels": 6}


class Rfc6381VideoTest(unittest.TestCase):
    def test_h264_high_40(self):
        self.assertEqual(
            browser_play.rfc6381_video(h264("High", 40)), ["avc1.640028"],
        )

    def test_hevc_main10_150_both_forms(self):
        self.assertEqual(
            browser_play.rfc6381_video(hevc("Main 10", 150)),
            ["hvc1.2.4.L150.B0", "hev1.2.4.L150.B0"],
        )

    def test_hevc_main_93(self):
        self.assertEqual(
            browser_play.rfc6381_video(hevc("Main", 93)),
            ["hvc1.1.6.L93.B0", "hev1.1.6.L93.B0"],
        )

    def test_av1_8bit(self):
        self.assertEqual(
            browser_play.rfc6381_video(av1(8)), ["av01.0.08M.08"],
        )

    def test_av1_10bit(self):
        self.assertEqual(
            browser_play.rfc6381_video(av1(13, ten_bit=True)),
            ["av01.0.13M.10"],
        )

    def test_vp9_8bit(self):
        stream = {"codec_name": "vp9", "level": 41, "pix_fmt": "yuv420p"}
        self.assertEqual(
            browser_play.rfc6381_video(stream), ["vp09.00.41.08"],
        )

    def test_vp9_10bit(self):
        stream = {"codec_name": "vp9", "level": 51, "pix_fmt": "yuv420p10le"}
        self.assertEqual(
            browser_play.rfc6381_video(stream), ["vp09.00.51.10"],
        )

    def test_unknown_profile_returns_empty(self):
        # A profile name ffprobe reports that this table has never seen
        # must not raise -- a black screen from a wrong guess is worse
        # than a refusal, so the caller gets [] and treats it as
        # unsupported rather than getting an exception.
        self.assertEqual(browser_play.rfc6381_video(h264("Weird", 40)), [])

    def test_unknown_codec_returns_empty(self):
        self.assertEqual(
            browser_play.rfc6381_video({"codec_name": "mpeg2video"}), [],
        )


class Rfc6381AudioTest(unittest.TestCase):
    def test_table(self):
        cases = [
            ("aac", ["mp4a.40.2"]),
            ("ac3", ["ac-3", "mp4a.a5"]),
            ("eac3", ["ec-3", "mp4a.a6"]),
            ("mp3", ["mp4a.40.34"]),
            ("opus", ["opus"]),
            ("flac", ["flac"]),
            ("dts", ["dtsc", "dtse"]),
            ("truehd", ["mlpa"]),
        ]
        for codec_name, expected in cases:
            with self.subTest(codec_name=codec_name):
                stream = {"codec_name": codec_name, "channels": 2}
                self.assertEqual(browser_play.rfc6381_audio(stream), expected)

    def test_unknown_audio_codec_returns_empty(self):
        self.assertEqual(
            browser_play.rfc6381_audio({"codec_name": "vorbis"}), [],
        )


# The full set of a "generous" browser's true probes, used for the direct
# mp4 case: every candidate string decide() could possibly look at,
# including the video+audio pair string it probes dynamically per source.
_FULL_CAPS_TYPES = list(browser_play.CODEC_PROBES) + [
    'video/mp4; codecs="avc1.640028,mp4a.40.2"',
]


class DecideTest(unittest.TestCase):
    def test_mp4_h264_high_aac_full_caps_is_direct(self):
        caps = make_caps(_FULL_CAPS_TYPES)
        probe = {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": 100.0,
            "video": h264("High", 40),
            "audio": [aac()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "direct")
        self.assertIsNone(result["vtag"])
        self.assertEqual(result["aidx"], 0)

    def test_mkv_h264_aac_is_remux(self):
        # Same codecs as the direct case, but a Matroska container is
        # never eligible for "direct" -- the mp4-family check alone
        # forces a remux, even with every codec string true.
        caps = make_caps(_FULL_CAPS_TYPES)
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": h264("High", 40),
            "audio": [aac()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "remux")
        self.assertEqual(result["acodec"], "copy")

    def test_mkv_hevc_main10_eac3_safari_shaped_is_remux(self):
        # Safari-shaped: it answers true for hvc1 and for ec-3.
        caps = make_caps([
            'video/mp4; codecs="hvc1.2.4.L150.B0"',
            'video/mp4; codecs="ec-3"',
        ])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": hevc("Main 10", 150),
            "audio": [eac3()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "remux")
        self.assertEqual(result["vtag"], "hvc1")

    def test_same_source_chrome_shaped_is_audio_only(self):
        # Chrome-shaped: hvc1 true, but no ec-3 -- same video, no audio.
        caps = make_caps(['video/mp4; codecs="hvc1.2.4.L150.B0"'])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": hevc("Main 10", 150),
            "audio": [eac3()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "audio")
        self.assertEqual(result["acodec"], "aac")
        self.assertEqual(result["vtag"], "hvc1")

    def test_mkv_hevc_firefox_shaped_no_hvc1_is_skip(self):
        # Firefox-shaped: no hvc1/hev1 string is true at all.
        caps = make_caps(['video/mp4; codecs="ec-3"'])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": hevc("Main 10", 150),
            "audio": [eac3()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("hevc", result["reason"])

    def test_mkv_av1_no_av01_is_skip(self):
        caps = make_caps(['video/mp4; codecs="mp4a.40.2"'])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": av1(8),
            "audio": [aac()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("av1", result["reason"])

    def test_level_headroom_153_over_150_is_skip_not_remux(self):
        # The browser has confirmed HEVC Main10 only up to L150 -- the
        # exact L153 string the source needs is never true. A source at
        # a level the browser hasn't confirmed must not be waved through
        # as a remux just because it recognises the codec family.
        caps = make_caps([
            'video/mp4; codecs="hvc1.2.4.L150.B0"',
            'video/mp4; codecs="ec-3"',
        ])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": hevc("Main 10", 153),
            "audio": [eac3()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("tops out", result["reason"])
        # HEVC levels are level*30 (general_level_idc), so 150 -> "5.0"
        # and 153 -> "5.1" -- matching the L150/L153 already in the tags.
        self.assertIn("L5.0", result["reason"])
        self.assertIn("L5.1", result["reason"])

    def test_vtag_is_hvc1_only_for_hevc(self):
        caps = make_caps(_FULL_CAPS_TYPES + [
            'video/mp4; codecs="hvc1.2.4.L150.B0,ec-3"',
        ])
        hevc_probe = {
            "format_name": "matroska,webm", "duration": 1.0,
            "video": hevc("Main 10", 150), "audio": [eac3()], "aidx": 0,
        }
        h264_probe = {
            "format_name": "matroska,webm", "duration": 1.0,
            "video": h264("High", 40), "audio": [aac()], "aidx": 0,
        }
        self.assertEqual(
            browser_play.decide(caps, hevc_probe)["vtag"], "hvc1",
        )
        self.assertIsNone(browser_play.decide(caps, h264_probe)["vtag"])

    def test_skip_reason_names_the_codec(self):
        caps = make_caps([])
        probe = {
            "format_name": "matroska,webm", "duration": 1.0,
            "video": h264("High", 40), "audio": [aac()], "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("h264", result["reason"])

    def test_bad_audio_index_is_treated_as_no_audio(self):
        # Video decodes fine; the chosen audio index is out of range, so
        # this degrades to video-only rather than raising IndexError.
        caps = make_caps(['video/mp4; codecs="avc1.640028"'])
        probe = {
            "format_name": "matroska,webm", "duration": 1.0,
            "video": h264("High", 40), "audio": [aac()], "aidx": 5,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "audio")
        self.assertEqual(result["acodec"], "aac")

    def test_h264_high_l41_is_remux_not_skip_regression(self):
        # Regression guard: H.264 High @ L4.1 is close to the most common
        # video format in existence, but CODEC_PROBES never probes exactly
        # "avc1.640029" -- only "avc1.640033" (High 5.1). An exact-string
        # match would wrongly skip this. A browser that has confirmed High
        # at L5.1 has necessarily proven it can decode High at L4.1 too.
        caps = make_caps([
            'video/mp4; codecs="avc1.640033"',
            'video/mp4; codecs="mp4a.40.2"',
        ])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": h264("High", 41),
            "audio": [aac()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "remux")
        self.assertEqual(result["acodec"], "copy")

    def test_hevc_main10_l123_is_not_skipped_against_l150_cap(self):
        caps = make_caps([
            'video/mp4; codecs="hvc1.2.4.L150.B0"',
            'video/mp4; codecs="ec-3"',
        ])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": hevc("Main 10", 123),
            "audio": [eac3()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertNotEqual(result["mode"], "skip")

    def test_hevc_main10_source_against_main_only_caps_is_skip(self):
        # A browser confirming only HEVC Main has not proven it can
        # decode Main 10 -- that direction of the tier order must stay
        # strict, since 10-bit decode is a genuinely separate capability.
        caps = make_caps(['video/mp4; codecs="hvc1.1.6.L150.B0"'])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": hevc("Main 10", 120),
            "audio": [eac3()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "skip")

    def test_h264_main_source_against_high_only_caps_is_supported(self):
        # A higher confirmed tier covers a lower source tier.
        caps = make_caps([
            'video/mp4; codecs="avc1.640028"',
            'video/mp4; codecs="mp4a.40.2"',
        ])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": h264("Main", 30),
            "audio": [aac()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertNotEqual(result["mode"], "skip")

    def test_hev1_form_cap_confirms_hvc1_shaped_source(self):
        caps = make_caps(['video/mp4; codecs="hev1.2.4.L150.B0"'])
        ok, cap_level = browser_play.video_supported(
            caps["types"], hevc("Main 10", 150),
        )
        self.assertTrue(ok)
        self.assertEqual(cap_level, 150)

    def test_hvc1_form_cap_confirms_hev1_shaped_expectation(self):
        # The source itself has no "form" (ffprobe never reports one) --
        # this proves the OTHER sample-entry spelling in caps also counts,
        # so either form the browser answered true for confirms the family.
        caps = make_caps(['video/mp4; codecs="hvc1.2.4.L150.B0"'])
        ok, cap_level = browser_play.video_supported(
            caps["types"], hevc("Main 10", 150),
        )
        self.assertTrue(ok)
        self.assertEqual(cap_level, 150)

    def test_unknown_family_is_skip_with_no_cap_level(self):
        caps = make_caps(_FULL_CAPS_TYPES)
        probe = {
            "format_name": "matroska,webm", "duration": 1.0,
            "video": {"codec_name": "mpeg2video"}, "audio": [aac()], "aidx": 0,
        }
        ok, cap_level = browser_play.video_supported(
            caps["types"], probe["video"],
        )
        self.assertFalse(ok)
        self.assertIsNone(cap_level)

        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("cannot decode", result["reason"])
        self.assertNotIn("tops out", result["reason"])

    def test_mp4_h264_high_l41_unprobed_pair_is_direct_regression(self):
        # Regression guard: CODEC_PROBES never probes exactly
        # "avc1.640029,mp4a.40.2" (only a few exact levels are sampled),
        # so for an ordinary H.264 High L4.1 MP4 the pair key is simply
        # absent from caps["types"] -- never probed, not refused. That
        # must not be read as a veto, or this file gets remuxed through
        # ffmpeg on every play for no reason.
        caps = make_caps([
            'video/mp4; codecs="avc1.640033"',
            'video/mp4; codecs="mp4a.40.2"',
            "video/mp4",
        ])
        probe = {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": 100.0,
            "video": h264("High", 41),
            "audio": [aac()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "direct")

    def test_mp4_pair_explicitly_false_vetoes_direct(self):
        # Same source, but this time the page DID probe the exact pair
        # and the platform explicitly refused it -- that is the one case
        # the veto exists for.
        caps = make_caps([
            'video/mp4; codecs="avc1.640033"',
            'video/mp4; codecs="mp4a.40.2"',
            "video/mp4",
        ])
        caps["types"]['video/mp4; codecs="avc1.640029,mp4a.40.2"'] = False
        probe = {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": 100.0,
            "video": h264("High", 41),
            "audio": [aac()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "remux")
        self.assertEqual(result["acodec"], "copy")

    def test_mkv_still_forces_remux_despite_unprobed_pair(self):
        # The pair veto only ever matters inside the mp4-family branch --
        # a Matroska source still can't be "direct" no matter what the
        # (irrelevant) pair lookup would say.
        caps = make_caps([
            'video/mp4; codecs="avc1.640033"',
            'video/mp4; codecs="mp4a.40.2"',
        ])
        probe = {
            "format_name": "matroska,webm",
            "duration": 100.0,
            "video": h264("High", 41),
            "audio": [aac()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "remux")

    def test_mp4_audio_failure_not_masked_by_pair_veto_logic(self):
        # Video decodes fine and the container is mp4, but the audio
        # (E-AC-3) is refused -- "direct" must never paper over that.
        caps = make_caps([
            'video/mp4; codecs="avc1.640033"',
            "video/mp4",
        ])
        caps["types"]['video/mp4; codecs="ec-3"'] = False
        probe = {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": 100.0,
            "video": h264("High", 41),
            "audio": [eac3()],
            "aidx": 0,
        }
        result = browser_play.decide(caps, probe)
        self.assertEqual(result["mode"], "audio")
        self.assertEqual(result["acodec"], "aac")


class VideoSupportedTest(unittest.TestCase):
    def test_family_mismatch_is_unsupported(self):
        caps = make_caps(['video/mp4; codecs="avc1.640033"'])
        ok, cap_level = browser_play.video_supported(
            caps["types"], hevc("Main 10", 150),
        )
        self.assertFalse(ok)
        self.assertIsNone(cap_level)

    def test_level_overrun_reports_the_confirmed_cap(self):
        caps = make_caps(['video/mp4; codecs="hvc1.2.4.L150.B0"'])
        ok, cap_level = browser_play.video_supported(
            caps["types"], hevc("Main 10", 153),
        )
        self.assertFalse(ok)
        self.assertEqual(cap_level, 150)

    def test_exact_probed_level_is_supported(self):
        caps = make_caps(['video/mp4; codecs="avc1.640028"'])
        ok, cap_level = browser_play.video_supported(
            caps["types"], h264("High", 40),
        )
        self.assertTrue(ok)
        self.assertEqual(cap_level, 40)


class PairStringTest(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(
            browser_play.pair_string("avc1.640028", "mp4a.40.2"),
            'video/mp4; codecs="avc1.640028,mp4a.40.2"',
        )


if __name__ == "__main__":
    unittest.main()
