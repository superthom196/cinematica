"""probe_full/probe_media/probe_gop: the ffprobe wrappers that decide whether a
panel can decode a stream and, separately, how finely it can be segmented.

probe_media used to run its own ffprobe call; it is now a thin adapter over
probe_full, and the regression this guards against is the adapter quietly
dropping or reshuffling a field the audio-only contract depended on.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server  # noqa: E402
from providers import contract  # noqa: E402


class FakeResult:
    """Enough of subprocess.CompletedProcess for json.loads(r.stdout)."""

    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


# A realistic ffprobe -of json payload: one HEVC Main 10 video stream and two
# audio tracks (English AC3 5.1, French AAC stereo), inside a Matroska file.
FULL_PAYLOAD = """
{
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "hevc",
            "profile": "Main 10",
            "level": 150,
            "pix_fmt": "yuv420p10le",
            "width": 3840,
            "height": 2160
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "ac3",
            "channels": 6,
            "tags": {"language": "eng"}
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "tags": {"language": "fre"}
        }
    ],
    "format": {
        "format_name": "matroska,webm",
        "duration": "7261.500000"
    }
}
"""

NO_VIDEO_PAYLOAD = """
{
    "streams": [
        {
            "index": 0,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "tags": {"language": "eng"}
        }
    ],
    "format": {"format_name": "mp4", "duration": "120.0"}
}
"""


class ProbeFullTest(unittest.TestCase):

    def setUp(self):
        self._real_run = server.subprocess.run
        self.last_argv = None

    def tearDown(self):
        server.subprocess.run = self._real_run

    def fake_run(self, stdout):
        # Records argv so tests can assert on the command shape, and hands
        # back a canned FakeResult regardless of what was asked for.
        def run(argv, **kwargs):
            self.last_argv = argv
            return FakeResult(stdout)
        server.subprocess.run = run

    def raising_run(self):
        def run(argv, **kwargs):
            self.last_argv = argv
            raise OSError("docker exec failed")
        server.subprocess.run = run

    def test_probe_full_realistic_payload(self):
        self.fake_run(FULL_PAYLOAD)
        result = server.probe_full("http://example/internal")
        self.assertEqual(result["format_name"], "matroska,webm")
        self.assertEqual(result["duration"], 7261.5)
        video = result["video"]
        self.assertIsNotNone(video)
        self.assertEqual(video["codec_name"], "hevc")
        self.assertEqual(video["profile"], "Main 10")
        self.assertEqual(video["level"], 150)
        self.assertEqual(video["pix_fmt"], "yuv420p10le")
        self.assertEqual(video["width"], 3840)
        self.assertEqual(video["height"], 2160)
        self.assertEqual(len(result["audio"]), 2)
        self.assertEqual(result["audio"][0]["codec_name"], "ac3")
        self.assertEqual(result["audio"][0]["channels"], 6)
        self.assertEqual(result["audio"][1]["codec_name"], "aac")
        self.assertEqual(result["audio"][1]["channels"], 2)
        self.assertEqual(result["langs"], ["eng", "fre"])

    def test_probe_full_does_not_select_streams_a(self):
        self.fake_run(FULL_PAYLOAD)
        server.probe_full("http://example/internal")
        self.assertNotIn("-select_streams", self.last_argv)
        # Belt and braces: even if -select_streams appeared for some other
        # reason, it must never be paired with "a" the way probe_media was.
        joined = " ".join(self.last_argv)
        self.assertNotIn("-select_streams a", joined)

    def test_probe_full_malformed_json(self):
        self.fake_run("not json{{{")
        result = server.probe_full("http://example/internal")
        self.assertEqual(result, {"format_name": "", "duration": None,
                                   "video": None, "audio": [], "langs": []})

    def test_probe_full_empty_stdout(self):
        self.fake_run("")
        result = server.probe_full("http://example/internal")
        self.assertEqual(result, {"format_name": "", "duration": None,
                                   "video": None, "audio": [], "langs": []})

    def test_probe_full_subprocess_raises(self):
        self.raising_run()
        result = server.probe_full("http://example/internal")
        self.assertEqual(result, {"format_name": "", "duration": None,
                                   "video": None, "audio": [], "langs": []})

    def test_probe_full_no_video_stream(self):
        self.fake_run(NO_VIDEO_PAYLOAD)
        result = server.probe_full("http://example/internal")
        self.assertIsNone(result["video"])
        self.assertEqual(len(result["audio"]), 1)


class ProbeMediaAdapterTest(unittest.TestCase):
    """The regression guard: probe_media's old four-tuple contract must
    survive being rebuilt on top of probe_full's dict."""

    def setUp(self):
        self._real_run = server.subprocess.run

    def tearDown(self):
        server.subprocess.run = self._real_run

    def test_probe_media_contract_preserved(self):
        def run(argv, **kwargs):
            return FakeResult(FULL_PAYLOAD)
        server.subprocess.run = run
        codec, dur, langs, codecs = server.probe_media("http://example/internal")
        self.assertEqual((codec, dur, langs, codecs),
                          ("ac3", 7261.5, ["eng", "fre"], ["ac3", "aac"]))


class PrepareCandidateInternalUrlTest(unittest.TestCase):
    """prepare_candidate() used to hand-build the url it hands to probe_media()
    as f"{STREMIO_IN}/{pick['infoHash']}" + ("/{fidx}" if fidx is not None
    else ""), which assumes every candidate is a torrent. An HTTP-transport
    candidate has no infoHash (normalise_candidate() sets it to None), so
    that built "http://127.0.0.1:11470/None" -- nothing ffprobe could open.
    prepare_candidate now calls stream_url_internal(pick) instead, which
    handles both transports; these tests pin the torrent url unchanged and
    the http url pointed at the /src/ proxy instead of Stremio."""

    def setUp(self):
        self._orig_pb = server.probe_and_buffer
        self._orig_pm = server.probe_media
        # Neither a live buffer nor a real ffprobe/docker exec is needed to
        # see what url got built, so both are stood in for.
        server.probe_and_buffer = lambda *a, **kw: (True, 4096, 1e9)
        server.probe_media = lambda url: (None, None, [], [])

    def tearDown(self):
        server.probe_and_buffer = self._orig_pb
        server.probe_media = self._orig_pm

    def _prepare(self, pick):
        tried = []
        # gen=None so superseded() always reads False, independent of
        # server._play_gen -- this test never accepts a play, it only
        # exercises the url prepare_candidate builds.
        return server.prepare_candidate("probe-test", pick, 100, 1, 1, None, tried)

    def test_torrent_internal_url_unchanged_without_file_index(self):
        pick, reason = contract.normalise_candidate(
            {"info_hash": "a" * 40}, "test-provider")
        self.assertIsNone(reason, reason)
        prep = self._prepare(pick)
        self.assertIsNotNone(prep)
        self.assertEqual(prep["internal"],
                          "%s/%s" % (server.STREMIO_IN, pick["infoHash"]))

    def test_torrent_internal_url_unchanged_with_file_index(self):
        pick, reason = contract.normalise_candidate(
            {"info_hash": "b" * 40, "file_index": 3}, "test-provider")
        self.assertIsNone(reason, reason)
        prep = self._prepare(pick)
        self.assertIsNotNone(prep)
        self.assertEqual(
            prep["internal"],
            "%s/%s/%s" % (server.STREMIO_IN, pick["infoHash"], pick["fileIdx"]))

    def test_http_internal_url_probes_the_src_proxy_not_stremio(self):
        pick, reason = contract.normalise_candidate(
            {"transport": "http", "url": "http://example.com/media/x.mp4"},
            "test-provider")
        self.assertIsNone(reason, reason)
        prep = self._prepare(pick)
        self.assertIsNotNone(prep)
        internal = prep["internal"]
        self.assertIn("/src/", internal)
        # Neither Stremio's port nor the "infoHash was None" telltale of
        # the bug this guards against should appear in an http source's url.
        self.assertNotIn("11470", internal)
        self.assertNotIn("None", internal)


class ProbeGopTest(unittest.TestCase):

    def setUp(self):
        self._real_run = server.subprocess.run
        self.last_argv = None

    def tearDown(self):
        server.subprocess.run = self._real_run

    def fake_run(self, stdout):
        def run(argv, **kwargs):
            self.last_argv = argv
            return FakeResult(stdout)
        server.subprocess.run = run

    def test_probe_gop_median_gap(self):
        self.fake_run("0\n2\n4\n6\n8\n")
        self.assertEqual(server.probe_gop("http://example/internal"), 2.0)

    def test_probe_gop_single_timestamp(self):
        self.fake_run("0\n")
        self.assertIsNone(server.probe_gop("http://example/internal"))

    def test_probe_gop_empty_output(self):
        self.fake_run("")
        self.assertIsNone(server.probe_gop("http://example/internal"))

    def test_probe_gop_subprocess_raises(self):
        def run(argv, **kwargs):
            raise OSError("docker exec failed")
        server.subprocess.run = run
        self.assertIsNone(server.probe_gop("http://example/internal"))

    def test_probe_gop_argv_shape(self):
        self.fake_run("0\n2\n4\n")
        server.probe_gop("http://example/internal")
        self.assertIn("-skip_frame", self.last_argv)
        self.assertIn("nokey", self.last_argv)


if __name__ == "__main__":
    unittest.main()
