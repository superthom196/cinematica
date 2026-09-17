"""Browser playback, end to end, over real HTTP: the POST the page actually
sends, through real provider lookup (a StubAddon installed and activated
through providers/registry.py + providers/gateway.py, exactly as an admin's
add-on would be), real candidate scoring (best_stream()/score()), real
capability matching (browser_play.decide(), driven off real
rfc6381_video/rfc6381_audio output), real publishing (publish()), to a real
GET of the published media url with working byte-range seeking.

Scope is direct mode only, deliberately: it is the one mode that needs no
ffmpeg and no Docker (see browser_play.py's TODO -- there is no video
transcode path in this server at all, only remux/copy or audio-only remux,
neither of which direct mode ever touches), and it is what requirement 6
("HTTP direct... no ffmpeg needed") is actually about. Two things this
process cannot honestly do without a real ffprobe/ffmpeg are mocked, at the
narrowest points that touch them:

  - server.prepare_candidate's real body calls probe_and_buffer() (a live
    network buffer against BUFFER_MIN, tens of MB) and probe_media() (an
    ffprobe over `docker exec`). Both are replaced by a small stand-in that
    reports "prepared" and points at the real, live /src/ url for this
    candidate via the SAME helper (stream_url_internal()) production code
    uses elsewhere -- see the regression test below, which now runs the
    real (unmocked) prepare_candidate to confirm it uses that helper too.
  - server.probe_full is `docker exec ffprobe`. Replaced with a canned
    H.264/AAC/mp4 result. Everything downstream of that result -- which
    CODEC_PROBES entries it satisfies, whether decide() calls it "direct",
    how publish() shapes media, how /src/ serves and range-seeks it -- is
    real.

Provider lookup, candidate scoring, capability matching and the two HTTP
routes that do the real work (/api/bplay/<id> and GET <media.url>) are never
mocked. Registry/store state is isolated per test via CINEMATICA_STATE
(tests.addon_stub's own convention, see tests/test_providers.py).

Run: python3 -m unittest discover -s server/tests -t server
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server               # noqa: E402
import browser_play         # noqa: E402
from providers import addon, contract, gateway, registry   # noqa: E402
from tests import addon_stub                                # noqa: E402


IDLE_BX = dict(server._bx)   # the real idle shape, captured before any test
# touches it -- NOT a hand-copied literal. A literal here is a second
# definition of _bx's shape that nothing keeps in step with the first: when
# run_anchor was added to the server, every test that rebound server._bx to
# such a literal handed the segment route a dict with that key missing, and
# three tests started failing with a 500 that had nothing to do with what
# they were testing.

# The one candidate the stub ever offers, in ffprobe's own shape -- H.264
# Constrained Baseline L3.0 video, AAC audio, in an mp4-family container.
# This is what the (mocked) probe_full() reports for every candidate in this
# file; only the browser's `caps` differ from test to test.
_VIDEO = {"codec_name": "h264", "profile": "Constrained Baseline", "level": 30}
_AUDIO = {"codec_name": "aac"}
_PROBE = {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": 5400.0,
          "video": _VIDEO, "audio": [_AUDIO], "langs": ["eng"]}


def _wrap(codec_string):
    # The exact probe-key format browser_play.CODEC_PROBES and caps["types"]
    # use -- see browser_play._wrap's own comment for why every lookup has
    # to be wrapped this way.
    return 'video/mp4; codecs="%s"' % codec_string


def _direct_caps():
    """A Safari-shaped caps body, built from CODEC_PROBES + the REAL
    rfc6381_video/rfc6381_audio for _VIDEO/_AUDIO above -- not hardcoded --
    so it tracks CODEC_PROBES and the rfc6381 tables if either ever changes.
    """
    true_tags = browser_play.rfc6381_video(_VIDEO) + browser_play.rfc6381_audio(_AUDIO)
    true_set = {_wrap(t) for t in true_tags}
    assert true_set, "rfc6381_video/rfc6381_audio produced nothing for _VIDEO/_AUDIO"
    return {"mse": True, "nativeHls": False,
            "types": {p: (p in true_set) for p in browser_play.CODEC_PROBES}}


def _unsupported_caps():
    """A Firefox-shaped caps body that confirms nothing at all."""
    return {"mse": True, "nativeHls": False,
            "types": {p: False for p in browser_play.CODEC_PROBES}}


class _Live:
    """A real Cinematica server (server.Server/server.H) on a real loopback
    port, the same pattern test_proxy.py and test_bplay.py use. Also patches
    server.PORT to the port actually bound, and restores it on exit --
    bx_verify_url() (run for real by this file, not mocked) dials
    "http://127.0.0.1:%d" % PORT to sanity-check a direct url before
    publish(), and that has to be THIS server, not whatever fixed PORT the
    module happened to import with.
    """

    def __enter__(self):
        self.app = server.Server(("127.0.0.1", 0), server.H)
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.app.server_address[1]
        self._orig_port = server.PORT
        server.PORT = self.port
        return self

    def __exit__(self, *exc):
        server.PORT = self._orig_port
        self.app.shutdown()
        self.app.server_close()
        self.thread.join(timeout=5)
        return False

    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("GET", path, headers=headers or {})
            r = c.getresponse()
            return r, r.read()
        finally:
            c.close()

    def post(self, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = json.dumps(body or {}).encode()
            h = {"Content-Type": "application/json"}
            h.update(headers or {})
            c.request("POST", path, body=data, headers=h)
            r = c.getresponse()
            return r, r.read()
        finally:
            c.close()


class BplayE2ETest(unittest.TestCase):
    """One StubAddon (http transport), installed and activated as the
    catalogue/metadata/streams provider through the real registry/gateway,
    per test. mid is always "tt9000001" -- the stub's first title, and a
    real IMDb-shaped id, so no provider-qualification wrinkle (see
    gateway._local_id_for) ever enters into it.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="cinematica-bplay-e2e-")
        os.environ["CINEMATICA_STATE"] = self._tmp

        server._jobs = {}
        server._bx = dict(IDLE_BX)
        server._play_gen = 0
        server._cancel_gen = 0
        server._streams = {}
        server._sources = {}

        self._orig_prepare = server.prepare_candidate
        self._orig_probe_full = server.probe_full
        self._orig_ctr_pid = server._ctr_pid
        self._orig_adb_enabled = server.ADB_ENABLED

        server.prepare_candidate = self._fake_prepare_candidate
        server.probe_full = lambda url_internal: {
            "format_name": _PROBE["format_name"], "duration": _PROBE["duration"],
            "video": dict(_VIDEO), "audio": [dict(_AUDIO)], "langs": list(_PROBE["langs"])}
        # No docker in this environment (and the task rules it out anyway):
        # a beat's pause/resume path looks up the packager's pid before it
        # would ever shell out to `kill`, and this candidate never actually
        # starts one, but the stub keeps this pinned regardless.
        server._ctr_pid = lambda name: None
        server.ADB_ENABLED = False

        self.stub = addon_stub.StubAddon(
            resources=("catalog", "meta", "stream"), transport="http").start()
        raw = addon._http_get_json(self.stub.manifest_url)
        manifest = addon.to_provider_manifest(raw, self.stub.manifest_url)
        registry.install(manifest, source=manifest["addon_url"])
        for role in (contract.ROLE_CATALOGUE, contract.ROLE_METADATA, contract.ROLE_STREAMS):
            registry.set_active(role, manifest["id"])
        self.provider_id = manifest["id"]
        self.mid = "tt9000001"

    def tearDown(self):
        self.stub.stop()
        server.prepare_candidate = self._orig_prepare
        server.probe_full = self._orig_probe_full
        server._ctr_pid = self._orig_ctr_pid
        server.ADB_ENABLED = self._orig_adb_enabled
        server._jobs = {}
        server._bx = dict(IDLE_BX)
        server._play_gen = 0
        server._cancel_gen = 0
        server._streams = {}
        server._sources = {}
        gateway.invalidate()
        os.environ.pop("CINEMATICA_STATE", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _fake_prepare_candidate(mid, pick, runtime_min, i, total, gen, tried):
        """Stand-in for the real probe_and_buffer()/probe_media() pair (live
        network buffering + `docker exec ffprobe`, neither available here).
        Reports every candidate as prepared, and -- unlike the real
        prepare_candidate(), see the KNOWN BUG test below -- correctly uses
        stream_url_internal() for the internal probe url, which is what lets
        the real bx_verify_url()/GET-the-media-url assertions in this file
        exercise the real, live /src/ proxy afterwards.
        """
        tried.append("%s (test double, no ffmpeg)" % (pick.get("tag") or pick.get("key")))
        return {"internal": server.stream_url_internal(pick), "acodec": "aac",
                "adur": None, "alangs": [], "acodecs": [], "aidx": 0,
                "got": len(addon_stub._MEDIA), "rate": 1e9}

    # ---- helpers --------------------------------------------------------------

    def _bplay(self, live, caps):
        r, body = live.post("/api/bplay/" + self.mid, {"caps": caps})
        return r, json.loads(body)

    def _poll_progress(self, live, job, deadline=10.0):
        t0 = time.time()
        last = {}
        while time.time() - t0 < deadline:
            r, body = live.get("/api/progress/" + job)
            self.assertEqual(r.status, 200)
            last = json.loads(body)
            if last.get("stage") in ("playing", "error"):
                return last
            time.sleep(0.02)
        self.fail("job %r stuck at stage=%r after %.1fs (last=%r)"
                  % (job, last.get("stage"), deadline, last))

    def _play_and_wait(self, live, caps=None):
        caps = _direct_caps() if caps is None else caps
        r, resp = self._bplay(live, caps)
        self.assertEqual(r.status, 202, resp)
        self.assertEqual(resp.get("job"), self.mid)
        self.assertIsNotNone(browser_play.RE_TOKEN.match(resp.get("token") or ""),
                             "token %r does not match RE_TOKEN" % resp.get("token"))
        self.assertIsInstance(resp.get("gen"), int)
        final = self._poll_progress(live, resp["job"])
        return resp, final

    # ---- 1. happy path, direct mode --------------------------------------------

    def test_direct_play_reaches_playing_with_a_relative_media_url(self):
        with _Live() as live:
            resp, final = self._play_and_wait(live)
            self.assertEqual(final.get("stage"), "playing", final)
            self.assertTrue(final.get("ok"))
            media = final.get("media") or {}
            self.assertEqual(media.get("kind"), "direct")
            url = media.get("url") or ""
            # Requirement 6: relative, and naming neither the streaming
            # server's host nor its port.
            self.assertTrue(url.startswith("/"), "media.url must be relative: %r" % url)
            self.assertNotIn(server.PUBLIC_HOST, url)
            self.assertNotIn(":11470", url)

    # ---- 2. the media actually serves, and seeks -------------------------------

    def test_direct_media_serves_full_body_and_byte_ranges(self):
        with _Live() as live:
            _resp, final = self._play_and_wait(live)
            url = final["media"]["url"]
            total = len(addon_stub._MEDIA)

            r, body = live.get(url)
            self.assertEqual(r.status, 200)
            self.assertEqual(body, addon_stub._MEDIA)

            r, body = live.get(url, headers={"Range": "bytes=100-199"})
            self.assertEqual(r.status, 206)
            self.assertEqual(r.getheader("Content-Range"), "bytes 100-199/%d" % total)
            self.assertEqual(body, addon_stub._MEDIA[100:200])

            tail_start, tail_end = total - 100, total - 1
            r, body = live.get(url, headers={"Range": "bytes=%d-%d" % (tail_start, tail_end)})
            self.assertEqual(r.status, 206)
            self.assertEqual(r.getheader("Content-Range"),
                             "bytes %d-%d/%d" % (tail_start, tail_end, total))
            self.assertEqual(body, addon_stub._MEDIA[tail_start:tail_end + 1])

    # ---- 3. no TV required ------------------------------------------------------

    def test_plays_with_no_tv_app_and_adb_disabled(self):
        self.assertFalse(server.ADB_ENABLED)
        self.assertIsNone(server.app_fresh())
        with _Live() as live:
            _resp, final = self._play_and_wait(live)
            self.assertEqual(final.get("stage"), "playing", final)
            self.assertIsNone(server.app_fresh())

    # ---- 4. ownership, over real HTTP -------------------------------------------

    def test_ownership_over_real_http(self):
        """TV-vs-browser arbitration (claim_owner()) needs a TV app in the
        picture to be worth anything -- with none connected, /api/play/...
        refuses with 502 "no TV app" before it ever reaches claim_owner(),
        for reasons that have nothing to do with ownership. app_fresh() is
        faked truthy here (the same technique tests/test_ownership.py uses)
        purely so the real /api/play/<id> route reaches the real ownership
        check; nothing about the "no TV required" claim in requirement 3
        (its own test above) depends on this.
        """
        with _Live() as live:
            resp, final = self._play_and_wait(live)
            self.assertEqual(final.get("stage"), "playing", final)
            token, gen = resp["token"], resp["gen"]

            orig_app_fresh = server.app_fresh
            server.app_fresh = lambda: {"id": "test-tv", "name": "Test TV",
                                        "job": None, "state": "idle", "acked": 0}
            try:
                r, body = live.post("/api/play/" + self.mid, {})
                self.assertEqual(r.status, 409)
                self.assertEqual(json.loads(body).get("msg"), "Another device is playing")

                r, body = live.post("/api/bx/beat", {
                    "token": token, "gen": gen, "pos": 1.0, "state": "playing"})
                self.assertEqual(r.status, 200)
                self.assertEqual(json.loads(body), {"ok": True})

                r, body = live.post("/api/bx/beat", {
                    "token": "wrong-token", "gen": gen, "pos": 1.0, "state": "playing"})
                self.assertEqual(r.status, 409)
                self.assertTrue(json.loads(body).get("stale"))

                r, body = live.post("/api/bx/stop", {"token": token, "gen": gen})
                self.assertEqual(r.status, 200)
                self.assertFalse(server.browser_playing())

                # The payoff: the TV is no longer refused. Checked directly
                # against claim_owner() -- the same function the real
                # /api/play/<id> route above already exercised -- rather
                # than a second real POST, which would need to spawn and
                # then somehow safely abandon a real run_play_job() worker
                # thread (ffmpeg/adb machinery this file has no business
                # touching) just to observe its 202.
                ok, msg = server.claim_owner("tv")
                self.assertEqual((ok, msg), (True, None))
            finally:
                server.app_fresh = orig_app_fresh

    # ---- 5. nothing TV-ward is touched -------------------------------------------

    def test_never_touches_tv_or_sendspin_machinery(self):
        """The exact four named in requirement 3: app_cmd/launch/wake_app
        (TV handoff) and _ss_q.put (Sendspin bridge commands) -- every one
        of them a command with a side effect. app_fresh() is deliberately
        NOT tripwired here: it is a harmless read, and claim_owner() (shared
        by the TV and browser routes, called from start_play() before
        run_browser_job even starts) legitimately reads it on every play,
        browser included, to check the TV is not already mid-film -- see
        tv_playback_state()'s own docstring. run_browser_job's docstring
        promise is narrower than "never reads app_fresh": it is "never
        touches _hifi, _ss_q, app_cmd, app_fresh, wake_app or adb()" FROM
        ITSELF, which this test still proves by tripwiring the ones that
        actually do something.
        """
        def tripwire(name):
            def f(*a, **kw):
                raise AssertionError("must never call %s for a browser play" % name)
            return f
        orig = (server.app_cmd, server.launch, server.wake_app, server._ss_q.put)
        server.app_cmd = tripwire("app_cmd")
        server.launch = tripwire("launch")
        server.wake_app = tripwire("wake_app")
        server._ss_q.put = tripwire("_ss_q.put")
        try:
            with _Live() as live:
                _resp, final = self._play_and_wait(live)
                self.assertEqual(final.get("stage"), "playing", final)
        finally:
            server.app_cmd, server.launch, server.wake_app, server._ss_q.put = orig

    # ---- 6. unsupported video ----------------------------------------------------

    def test_unsupported_video_codec_is_named_in_the_error(self):
        with _Live() as live:
            _resp, final = self._play_and_wait(live, caps=_unsupported_caps())
            self.assertEqual(final.get("stage"), "error", final)
            self.assertFalse(final.get("ok"))
            msg = (final.get("msg") or "").lower()
            self.assertIn("h264", msg, "error message must name the codec: %r" % final.get("msg"))

    # ---- regression: prepare_candidate must use stream_url_internal() ------------

    def test_prepare_candidate_probes_http_sources_through_the_src_proxy(self):
        """server.prepare_candidate() (server.py) used to build the url it
        hands to probe_media() by hand:

            internal = f"{STREMIO_IN}/{pick['infoHash']}" + (f"/{fidx}" if fidx is not None else "")

        which assumed a torrent candidate. For an HTTP-transport candidate
        (contract.T_HTTP -- every candidate an http-mode Stremio add-on
        returns, this StubAddon in http mode included), pick["infoHash"] is
        None, so this literally became "http://127.0.0.1:11470/None": never
        the file. server.py already has the correct helper for exactly this
        -- stream_url_internal(), which probe_and_buffer() (via stream_url())
        and the /src/ and /t/ proxies all use -- and prepare_candidate() now
        calls it too, instead of duplicating the logic.

        This calls the REAL (unmocked-by-this-file) prepare_candidate with
        an http-transport pick, with only probe_and_buffer/probe_media stood
        in for (no live buffering or ffprobe needed here -- this checks what
        URL gets built, not what a probe of it would return), and confirms
        the internal url it computes now agrees with stream_url_internal(pick).

        Before the fix this meant every HTTP-transport candidate's
        probe_media()/probe_full() call (the second of which also feeds
        browser_play.decide() directly, via run_browser_job's own
        probe_full(prep["internal"]) call) ran against a dead url, so an
        HTTP-transport provider's candidates probed as codec_name=None and
        browser_play.decide() reported them all "skip" -- unplayable --
        regardless of whether the browser could actually have played them
        directly. This file's own happy-path tests above do not exercise
        this: their _fake_prepare_candidate stands in for prepare_candidate
        entirely and uses stream_url_internal() itself, which is what let
        them test the rest of the pipeline (provider lookup, scoring,
        decide(), publish(), real byte-range serving) independently of
        this bug.
        """
        pick = {"transport": contract.T_HTTP, "url": "http://127.0.0.1:1/media/x.mp4",
                "headers": {}, "infoHash": None, "fileIdx": None,
                "codec": "?", "gb": None, "tag": "buggy-http-candidate"}
        orig_pb, orig_pm = server.probe_and_buffer, server.probe_media
        server.probe_and_buffer = lambda *a, **kw: (True, 4096, 1e9)
        server.probe_media = lambda url: (None, None, [], [])
        try:
            tried = []
            prep = self._orig_prepare("bugtest", pick, 100, 1, 1, 0, tried)
        finally:
            server.probe_and_buffer, server.probe_media = orig_pb, orig_pm
        self.assertIsNotNone(prep)
        self.assertEqual(
            prep["internal"], server.stream_url_internal(pick),
            "prepare_candidate() must probe the http-transport source's own "
            "url (via stream_url_internal()), not an infoHash-shaped "
            "STREMIO_IN url built from a None infoHash")


if __name__ == "__main__":
    unittest.main()
