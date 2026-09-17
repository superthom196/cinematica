"""Browser playback, end to end inside server.py: run_browser_job's walk
down the ranked candidates, browser_picks()'s pre-filter ordering, and the
/api/bx/probes, /api/bx/beat and /api/bx/stop routes.

No ffmpeg, no network, no real sockets to the outside. run_browser_job is
exercised directly (not through start_play/threads) with prepare_candidate,
probe_full, probe_gop, bx_begin, stream_url_public and the direct-URL
verification (bx_verify_url) all monkeypatched -- only browser_play.decide()
itself is real, driven by hand-built probe/caps dicts, because that codec
matching logic is exactly what a stubbed decide() would stop testing.

The three routes ARE driven over a real loopback socket (server.Server on
127.0.0.1:0, the same pattern test_bx_serve.py and test_proxy.py already
use) -- that is local, not "the outside", and the point is to exercise the
real request parsing (JSON body regardless of Content-Type, for
sendBeacon's benefit).

Run: python3 -m unittest discover -s server/tests -t server
"""

import http.client
import json
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server           # noqa: E402
import browser_play     # noqa: E402


IDLE_BX = dict(server._bx)   # the real idle shape, captured before any test
# touches it -- NOT a hand-copied literal. A literal here is a second
# definition of _bx's shape that nothing keeps in step with the first: when
# run_anchor was added to the server, every test that rebound server._bx to
# such a literal handed the segment route a dict with that key missing, and
# three tests started failing with a 500 that had nothing to do with what
# they were testing.


def _pick(key, codec="HEVC"):
    return {"key": key, "infoHash": None, "url": None, "codec": codec,
            "tag": "release-%s" % key, "gb": 4.0}


# ---- probe/caps fixtures, crafted so the REAL browser_play.decide() lands
# on a specific mode -- see test_browser_caps.py for decide() in isolation;
# here the point is only to pick fixtures that exercise each branch of
# run_browser_job, not to re-test decide() itself.

# H.264 Constrained Baseline L3.0 + AAC in an mp4 container, with both of
# CODEC_PROBES' matching entries confirmed true and no pair veto -- decide()
# lands on "direct".
_DIRECT_VIDEO = {"codec_name": "h264", "profile": "Constrained Baseline", "level": 30}
_DIRECT_AUDIO = {"codec_name": "aac"}
DIRECT_CAPS = {"mse": True, "nativeHls": False, "types": {
    'video/mp4; codecs="avc1.42E01E"': True,
    'video/mp4; codecs="mp4a.40.2"': True,
}}
DIRECT_PROBE = {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": 5400.0,
                 "video": _DIRECT_VIDEO, "audio": [_DIRECT_AUDIO], "langs": ["eng"]}

# Same video/audio, but NOT an mp4-family container -- decide() skips the
# direct branch entirely and lands on "remux" (acodec "copy").
REMUX_PROBE = dict(DIRECT_PROBE, format_name="matroska,webm")

# Two candidates neither of which this (typesless) browser can decode --
# both land on "skip", by a different codec each, for the "names every
# candidate's codec" error-message test.
NO_TYPES_CAPS = {"mse": True, "nativeHls": False, "types": {}}
SKIP_PROBE_HEVC = {"format_name": "matroska,webm", "duration": 5400.0,
                    "video": {"codec_name": "hevc", "profile": "Main", "level": 120},
                    "audio": [{"codec_name": "aac"}], "langs": ["eng"]}
SKIP_PROBE_H264 = {"format_name": "matroska,webm", "duration": 5400.0,
                    "video": {"codec_name": "h264", "profile": "High", "level": 40},
                    "audio": [{"codec_name": "aac"}], "langs": ["eng"]}


class RunBrowserJobTest(unittest.TestCase):
    def setUp(self):
        server._jobs = {}
        server._bx = dict(IDLE_BX)
        server._play_gen = 0
        server._cancel_gen = 0
        self._orig = {name: getattr(server, name) for name in (
            "prepare_candidate", "probe_full", "probe_gop", "bx_begin",
            "stream_url_public", "bx_verify_url")}

        self.preps = {}           # candidate key -> prep dict, or absent = None
        self.probes = {}          # internal url -> probe_full()-shaped dict
        self.prepare_calls = []   # candidate keys prepare_candidate was called for
        self.bx_begin_calls = []
        self.bx_begin_result = True
        self.verify_result = True

        def fake_prepare(mid, pick, runtime_min, i, total, gen, tried):
            key = server.candidate_key(pick)
            self.prepare_calls.append(key)
            prep = self.preps.get(key)
            tried.append("tried-%s" % key)
            return dict(prep) if prep else None

        def fake_probe_full(url_internal):
            p = self.probes.get(url_internal)
            if p is None:
                return {"format_name": "", "duration": None, "video": None,
                        "audio": [], "langs": []}
            return dict(p)

        def fake_probe_gop(url_internal, window=40):
            return 4.0

        def fake_bx_begin(token, src_internal, plan, seg, duration, mid, gen):
            self.bx_begin_calls.append({
                "token": token, "src": src_internal, "plan": plan,
                "seg": seg, "duration": duration, "mid": mid, "gen": gen})
            return self.bx_begin_result

        def fake_stream_url_public(pick):
            return "/src/%s" % server.candidate_key(pick)

        def fake_verify(url_path):
            return self.verify_result

        server.prepare_candidate = fake_prepare
        server.probe_full = fake_probe_full
        server.probe_gop = fake_probe_gop
        server.bx_begin = fake_bx_begin
        server.stream_url_public = fake_stream_url_public
        server.bx_verify_url = fake_verify
        # publish() now registers _bx for every mode (see its own
        # docstring), so a remux/audio publish's real bx_begin path is not
        # exercised here -- but /api/bx/beat's real pause/resume branch,
        # exercised over a live socket further down, would otherwise shell
        # out to `docker exec ... pgrep`. No docker in these tests either.
        self._orig_ctr_pid = server._ctr_pid
        server._ctr_pid = lambda name: None

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(server, name, fn)
        server._ctr_pid = self._orig_ctr_pid
        server._jobs = {}
        server._bx = dict(IDLE_BX)
        server._play_gen = 0
        server._cancel_gen = 0

    def _run(self, picks, caps, skip=None, token="tok1", mid="m1"):
        server.run_browser_job(mid, picks, 100, "Some Title", 0, token, caps, skip)
        return server.job_get(mid)

    # ---- mode selection -> media shape -------------------------------------

    def test_direct_candidate_reaches_playing_with_relative_url(self):
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = DIRECT_PROBE
        job = self._run([pick], DIRECT_CAPS)
        self.assertEqual(job.get("stage"), "playing")
        self.assertTrue(job.get("ok"))
        media = job.get("media") or {}
        self.assertEqual(media.get("kind"), "direct")
        self.assertTrue((media.get("url") or "").startswith("/"),
                        "direct url must be relative: %r" % media.get("url"))
        self.assertEqual(self.bx_begin_calls, [])   # no packager for a direct play

    def test_remux_candidate_uses_hls_and_calls_bx_begin_once(self):
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = REMUX_PROBE
        job = self._run([pick], DIRECT_CAPS, token="tokABC")
        self.assertEqual(job.get("stage"), "playing")
        media = job.get("media") or {}
        self.assertEqual(media.get("kind"), "hls")
        self.assertEqual(media.get("url"), "/hls/tokABC/index.m3u8")
        self.assertEqual(len(self.bx_begin_calls), 1)
        self.assertEqual(self.bx_begin_calls[0]["seg"], media.get("seg"))

    # ---- autoplay: the next episode, offered with the media -----------------
    # The TV fires autoplay itself, from app_heartbeat. A browser cannot be
    # driven that way -- nothing tells the server the film ended until the
    # page says so -- so the server OFFERS the next episode alongside the
    # media and the page decides. That makes this a property of the
    # published media, which is what these check.

    def _patch_next(self, result, autoplay=True):
        orig_next, orig_flag = server.next_episode, server.AUTOPLAY_NEXT
        def restore():
            server.next_episode, server.AUTOPLAY_NEXT = orig_next, orig_flag
        self.addCleanup(restore)
        calls = []
        def fake(tid, s, e):
            calls.append((tid, s, e))
            if isinstance(result, Exception):
                raise result
            return result
        server.next_episode = fake
        server.AUTOPLAY_NEXT = autoplay
        return calls

    def _play_episode(self, probe=None, mid="tv:1399:1:1"):
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = probe or REMUX_PROBE
        job = self._run([pick], DIRECT_CAPS, mid=mid)
        self.assertEqual(job.get("stage"), "playing", job.get("msg"))
        return job.get("media") or {}

    def test_episode_media_offers_the_next_episode(self):
        calls = self._patch_next((1, 2))
        media = self._play_episode()
        self.assertEqual(calls, [("1399", 1, 1)])
        self.assertEqual(media.get("next"),
                         {"id": "1399", "s": 1, "e": 2, "label": "S01E02"})

    def test_a_film_is_never_offered_a_next_episode(self):
        calls = self._patch_next((1, 2))
        media = self._play_episode(mid="m1")
        self.assertIsNone(media.get("next"))
        self.assertEqual(calls, [], "a film must not cost a provider lookup")

    def test_the_last_episode_of_a_show_offers_nothing(self):
        self._patch_next(None)
        self.assertIsNone(self._play_episode().get("next"))

    def test_autoplay_next_off_offers_nothing(self):
        calls = self._patch_next((1, 2), autoplay=False)
        self.assertIsNone(self._play_episode().get("next"))
        self.assertEqual(calls, [], "the flag must be checked before the lookup")

    def test_a_provider_failure_costs_the_offer_and_nothing_else(self):
        # A film that plays is worth more than an autoplay that works: a
        # lookup blowing up must not take the playback down with it.
        self._patch_next(RuntimeError("provider down"))
        media = self._play_episode()
        self.assertIsNone(media.get("next"))
        self.assertEqual(media.get("kind"), "hls")

    def test_direct_mode_offers_it_too(self):
        # Direct play never touches the packager, and the offer is not the
        # packager's -- an episode the browser can play untouched still has
        # a next episode.
        self._patch_next((2, 1))
        media = self._play_episode(probe=DIRECT_PROBE)
        self.assertEqual(media.get("kind"), "direct")
        self.assertEqual(media.get("next"),
                         {"id": "1399", "s": 2, "e": 1, "label": "S02E01"})

    # ---- publish() must register _bx for EVERY mode -------------------------
    # active_job() cannot answer "is a browser watching something": a
    # published job is stage="playing", which is not in JOB_ACTIVE. _bx is
    # the only thing browser_playing()/claim_owner()/cache_watch() trust for
    # that, and a direct play never calls bx_begin() -- so without publish()
    # registering it too, a direct-played film looked idle to cache_watch()
    # and got its cache cleared out from under it ~60s in.

    def test_direct_publish_is_visible_to_cache_watch_and_claim_owner(self):
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = DIRECT_PROBE
        job = self._run([pick], DIRECT_CAPS, token="tok-direct")
        self.assertEqual(job.get("stage"), "playing")
        self.assertTrue(server.browser_playing())
        self.assertTrue(server.playing_now())    # the cache_watch() guard
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_remux_publish_is_visible_to_cache_watch_and_claim_owner(self):
        # Same three assertions as the direct case above, so the two modes
        # are proven equivalent here rather than incidentally different.
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = REMUX_PROBE
        job = self._run([pick], DIRECT_CAPS, token="tok-remux")
        self.assertEqual(job.get("stage"), "playing")
        self.assertTrue(server.browser_playing())
        self.assertTrue(server.playing_now())
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (False, "Another device is playing"))

    def test_beat_ok_true_right_after_a_direct_publish(self):
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = DIRECT_PROBE
        self._run([pick], DIRECT_CAPS, token="tok-direct")
        with _Live() as live:
            r, body = live.post("/api/bx/beat", {
                "token": "tok-direct", "gen": 0, "pos": 1.0, "state": "playing"})
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(body), {"ok": True})   # not 409 stale

    def test_beat_ok_true_right_after_a_remux_publish(self):
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = REMUX_PROBE
        self._run([pick], DIRECT_CAPS, token="tok-remux")
        with _Live() as live:
            r, body = live.post("/api/bx/beat", {
                "token": "tok-remux", "gen": 0, "pos": 1.0, "state": "playing"})
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(body), {"ok": True})

    def test_stale_heartbeat_after_direct_publish_still_releases_the_claim(self):
        # The fix must not make a direct session impossible to reclaim --
        # only a LIVE one is protected.
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = DIRECT_PROBE
        self._run([pick], DIRECT_CAPS, token="tok-direct")
        self.assertTrue(server.browser_playing())
        with server._lock:
            server._bx["at"] = time.time() - (server.BX_IDLE + 1)
        self.assertFalse(server.browser_playing())
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (True, None))

    # ---- cancellation must be rechecked immediately before publish ----------
    # Every stand-down check before this one is separated from publish() by a
    # network round trip (bx_verify_url) or a process launch (bx_begin), and a
    # Stop or a newer play can land inside either. Publishing after that puts
    # a cancelled film back on screen AND back into _bx, where
    # browser_playing() then holds the player for a viewer who has gone.

    def test_direct_cancelled_during_verification_is_not_published(self):
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = DIRECT_PROBE
        def cancelling_verify(url_path):
            server.play_claim()      # what /api/stop and /api/cancel do
            return True
        server.bx_verify_url = cancelling_verify
        job = self._run([pick], DIRECT_CAPS, token="tok-gone")
        self.assertEqual(job.get("stage"), "error")
        self.assertFalse(job.get("ok"))
        self.assertIn("Superseded", job.get("msg") or "")
        self.assertIsNone(job.get("media"))

    def test_direct_cancelled_during_verification_does_not_claim_the_player(self):
        # The half that actually stranded the user: _bx is what
        # browser_playing() and claim_owner() read, and publish() is the only
        # thing that ever fills it for a direct play.
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = DIRECT_PROBE
        def cancelling_verify(url_path):
            server.play_claim()
            return True
        server.bx_verify_url = cancelling_verify
        self._run([pick], DIRECT_CAPS, token="tok-gone")
        self.assertIsNone(server._bx["token"])
        self.assertFalse(server.browser_playing())
        self.assertEqual(server.claim_owner("tv"), (True, None))

    def test_direct_still_published_when_nothing_cancelled_it(self):
        # The guard must only fire on a real supersede -- an ordinary
        # verification still ends in playback.
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = DIRECT_PROBE
        job = self._run([pick], DIRECT_CAPS, token="tok-ok")
        self.assertEqual(job.get("stage"), "playing")
        self.assertEqual(server._bx["token"], "tok-ok")

    def test_hls_cancelled_while_the_packager_started_is_not_published(self):
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = REMUX_PROBE
        stopped = []
        orig_stop = server.bx_stop_all
        self.addCleanup(lambda: setattr(server, "bx_stop_all", orig_stop))
        server.bx_stop_all = lambda reason=None: stopped.append(reason)
        def cancelling_begin(token, src, plan, seg, duration, mid, gen):
            self.bx_begin_calls.append({"token": token})
            # Mimic the real bx_begin, which registers the session before
            # anything can be published from it.
            with server._lock:
                server._bx.update(token=token, gen=gen, state="starting",
                                  at=time.time())
            server.play_claim()
            return True
        server.bx_begin = cancelling_begin
        job = self._run([pick], DIRECT_CAPS, token="tok-hls-gone")
        self.assertEqual(job.get("stage"), "error")
        self.assertIn("Superseded", job.get("msg") or "")
        self.assertIsNone(job.get("media"))
        # ffmpeg is already running by this point: leaving it writing
        # segments for a session nothing will ever publish is the whole
        # reason this branch tears down rather than just returning.
        self.assertEqual(stopped, ["superseded"])

    def test_hls_teardown_never_stops_the_session_that_superseded_it(self):
        # If the request that won the race has already begun its own
        # session, _bx is no longer this worker's to clear.
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = REMUX_PROBE
        stopped = []
        orig_stop = server.bx_stop_all
        self.addCleanup(lambda: setattr(server, "bx_stop_all", orig_stop))
        server.bx_stop_all = lambda reason=None: stopped.append(reason)
        def cancelling_begin(token, src, plan, seg, duration, mid, gen):
            self.bx_begin_calls.append({"token": token})
            server.play_claim()
            with server._lock:     # the winner's session, not ours
                server._bx.update(token="tok-winner", gen=99,
                                  state="starting", at=time.time())
            return True
        server.bx_begin = cancelling_begin
        job = self._run([pick], DIRECT_CAPS, token="tok-loser")
        self.assertEqual(job.get("stage"), "error")
        self.assertEqual(stopped, [])
        self.assertEqual(server._bx["token"], "tok-winner")

    # ---- the skip contract --------------------------------------------------

    def test_skip_param_advances_the_walk_to_the_next_candidate(self):
        p1, p2 = _pick("c1", codec="H264"), _pick("c2", codec="H264")
        self.preps["c2"] = {"internal": "internal://c2", "aidx": 0}
        self.probes["internal://c2"] = REMUX_PROBE
        job = self._run([p1, p2], DIRECT_CAPS, skip=["c1"])
        self.assertEqual(job.get("stage"), "playing")
        self.assertEqual(job.get("pick", {}).get("key"), "c2")

    def test_skipped_candidate_is_never_attempted_at_all(self):
        p1, p2 = _pick("c1", codec="H264"), _pick("c2", codec="H264")
        self.preps["c2"] = {"internal": "internal://c2", "aidx": 0}
        self.probes["internal://c2"] = REMUX_PROBE
        self._run([p1, p2], DIRECT_CAPS, skip=["c1"])
        self.assertNotIn("c1", self.prepare_calls)
        self.assertIn("c2", self.prepare_calls)

    def test_all_candidates_skip_names_every_codec_in_the_error(self):
        p1, p2 = _pick("c1", codec="HEVC"), _pick("c2", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.preps["c2"] = {"internal": "internal://c2", "aidx": 0}
        self.probes["internal://c1"] = SKIP_PROBE_HEVC
        self.probes["internal://c2"] = SKIP_PROBE_H264
        job = self._run([p1, p2], NO_TYPES_CAPS)
        self.assertEqual(job.get("stage"), "error")
        self.assertFalse(job.get("ok"))
        msg = job.get("msg") or ""
        self.assertIn("HEVC", msg)
        self.assertIn("H264", msg)

    def test_tried_keys_lands_on_the_job(self):
        pick = _pick("c1", codec="H264")
        self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
        self.probes["internal://c1"] = REMUX_PROBE
        job = self._run([pick], DIRECT_CAPS)
        self.assertEqual(job.get("tried_keys"), ["c1"])

    # ---- requirement-3 guard: no TV/hifi machinery, ever ---------------------

    def test_never_touches_tv_app_or_hifi_machinery(self):
        def tripwire(name):
            def f(*a, **kw):
                raise AssertionError("run_browser_job must never call %s" % name)
            return f
        orig = (server.app_cmd, server.launch, server.wake_app,
                server.app_fresh, server.adb, server._ss_q.put, server._hifi)

        class _TripwireHifi(dict):
            def __setitem__(self, k, v):
                if k == "src":
                    raise AssertionError(
                        "run_browser_job must never set _hifi['src']")
                super().__setitem__(k, v)

        server.app_cmd = tripwire("app_cmd")
        server.launch = tripwire("launch")
        server.wake_app = tripwire("wake_app")
        server.app_fresh = tripwire("app_fresh")
        server.adb = tripwire("adb")
        server._ss_q.put = tripwire("_ss_q.put")
        server._hifi = _TripwireHifi(orig[6])
        try:
            pick = _pick("c1", codec="H264")
            self.preps["c1"] = {"internal": "internal://c1", "aidx": 0}
            self.probes["internal://c1"] = REMUX_PROBE
            job = self._run([pick], DIRECT_CAPS)
            self.assertEqual(job.get("stage"), "playing")
        finally:
            (server.app_cmd, server.launch, server.wake_app, server.app_fresh,
             server.adb, server._ss_q.put, server._hifi) = orig


class BrowserPicksTest(unittest.TestCase):
    """browser_picks() is only a pre-filter on release metadata, not a
    probe -- these check the ordering rule directly, without touching
    run_browser_job."""

    def test_unsupported_codec_moves_to_the_back_stably(self):
        av1 = _pick("av1-1", codec="AV1")
        hevc1 = _pick("hevc-1", codec="HEVC")
        av1b = _pick("av1-2", codec="AV1")
        hevc2 = _pick("hevc-2", codec="HEVC")
        entry = {"picks_all": [av1, hevc1, av1b, hevc2]}
        # No AV1 probe ever answers true in a real browser; HEVC's does here.
        caps = {"types": {'video/mp4; codecs="hvc1.1.6.L150.B0"': True}}
        out = browser_play_test_order(entry, caps)
        self.assertEqual(out, ["hevc-1", "hevc-2", "av1-1", "av1-2"])

    def test_unknown_codec_is_not_demoted(self):
        unknown = _pick("u1", codec="?")
        av1 = _pick("av1-1", codec="AV1")
        entry = {"picks_all": [av1, unknown]}
        caps = {"types": {}}    # nothing confirmed supported at all
        out = browser_play_test_order(entry, caps)
        # "?" is left where it was (possibly playable); AV1 is demoted.
        self.assertEqual(out, ["u1", "av1-1"])


def browser_play_test_order(entry, caps):
    return [c["key"] for c in server.browser_picks(entry, caps)]


class BxRoutesTest(unittest.TestCase):
    """/api/bx/probes, /api/bx/beat and /api/bx/stop, driven over a real
    loopback socket the way test_bx_serve.py drives /hls/."""

    def setUp(self):
        server._jobs = {}
        server._bx = dict(IDLE_BX)
        server._play_gen = 0
        server._cancel_gen = 0
        # No real docker/ffmpeg here: a beat's pause/resume branch looks up
        # the packager's pid before it would ever shell out to `kill`.
        self._orig_ctr_pid = server._ctr_pid
        server._ctr_pid = lambda name: None

    def tearDown(self):
        server._jobs = {}
        server._bx = dict(IDLE_BX)
        server._play_gen = 0
        server._cancel_gen = 0
        server._ctr_pid = self._orig_ctr_pid

    def test_bx_probes_matches_the_module_list(self):
        with _Live() as live:
            r, body = live.get("/api/bx/probes")
            self.assertEqual(r.status, 200)
            data = json.loads(body)
            self.assertEqual(data["probes"], list(browser_play.CODEC_PROBES))

    def test_bx_probes_publishes_the_audio_subset(self):
        # Every probe is published as video/mp4 -- that is the container the
        # browser is really handed, audio codecs included -- so the page
        # cannot work out which of them name an audio codec. It splits on
        # this list to build the video x audio pairing probes decide() reads;
        # without it every audio string landed in the video list, the audio
        # list came out empty, and the page built zero pairs.
        with _Live() as live:
            r, body = live.get("/api/bx/probes")
            self.assertEqual(r.status, 200)
            data = json.loads(body)
            self.assertEqual(data["audio"], list(browser_play.AUDIO_PROBES))

    def test_bx_probes_audio_subset_is_a_real_split_of_the_list(self):
        with _Live() as live:
            _, body = live.get("/api/bx/probes")
        data = json.loads(body)
        probes, audio = data["probes"], data["audio"]
        self.assertTrue(set(audio) <= set(probes),
                        "every audio probe must also be in the probe list")
        self.assertTrue(audio, "an empty audio list builds no pairs at all")
        video = [p for p in probes if p not in set(audio)
                 and 'codecs="' in p]
        self.assertTrue(video, "an empty video list builds no pairs at all")
        # The split has to be by codec, not by mime type: both halves are
        # spelled video/mp4, which is the whole reason the server sends it.
        self.assertTrue(all(p.startswith("video/mp4") for p in audio))
        # A sanity check on which side things landed, named rather than
        # counted so a new codec does not break this test for no reason.
        self.assertIn('video/mp4; codecs="mp4a.40.2"', audio)
        self.assertIn('video/mp4; codecs="mp4a.40.5"', audio)   # HE-AAC
        self.assertIn('video/mp4; codecs="ec-3"', audio)
        self.assertIn('video/mp4; codecs="avc1.640028"', video)
        self.assertIn('video/mp4; codecs="av01.0.08M.08"', video)

    def test_bx_probes_audio_subset_pairs_against_pair_string(self):
        # The pairs the page builds are keyed by browser_play.pair_string();
        # this proves the two halves of the split really do compose into the
        # key decide() looks up, rather than into something shaped like it.
        vid = 'video/mp4; codecs="avc1.640028"'
        aud = 'video/mp4; codecs="mp4a.40.2"'
        self.assertIn(aud, browser_play.AUDIO_PROBES)
        self.assertNotIn(vid, browser_play.AUDIO_PROBES)
        self.assertEqual(browser_play.pair_string("avc1.640028", "mp4a.40.2"),
                         'video/mp4; codecs="avc1.640028,mp4a.40.2"')

    def test_beat_mismatched_token_is_409_stale_and_changes_nothing(self):
        server._bx.update(token="real-token", gen=5, at=0.0, pos=0.0)
        with _Live() as live:
            r, body = live.post("/api/bx/beat", {
                "token": "wrong-token", "gen": 5, "pos": 99.0, "state": "playing"})
            self.assertEqual(r.status, 409)
            self.assertTrue(json.loads(body).get("stale"))
        self.assertEqual(server._bx["at"], 0.0)
        self.assertEqual(server._bx["pos"], 0.0)

    def test_beat_matching_token_updates_at_and_pos(self):
        server._bx.update(token="tok-live", gen=5, at=0.0, pos=0.0, state="playing")
        with _Live() as live:
            r, _ = live.post("/api/bx/beat", {
                "token": "tok-live", "gen": 5, "pos": 12.5, "state": "playing"})
            self.assertEqual(r.status, 200)
        self.assertGreater(server._bx["at"], 0.0)
        self.assertEqual(server._bx["pos"], 12.5)

    def test_paused_beat_does_not_retire_the_job(self):
        server._bx.update(token="tok-live", gen=5, at=0.0, pos=0.0, state="playing")
        server.job_set("m1", stage="buffering", owner="browser", otoken="tok-live")
        with _Live() as live:
            r, _ = live.post("/api/bx/beat", {
                "token": "tok-live", "gen": 5, "pos": 1.0, "state": "paused"})
            self.assertEqual(r.status, 200)
        job = server.job_get("m1")
        self.assertEqual(job.get("stage"), "buffering")

    def test_bx_stop_matching_bx_session_tears_it_down(self):
        server._bx.update(token="tok-live", gen=1, at=time.time(), state="playing")
        with _Live() as live:
            r, _ = live.post("/api/bx/stop", {"token": "tok-live", "gen": 1})
            self.assertEqual(r.status, 200)
        self.assertIsNone(server._bx["token"])

    def test_bx_stop_tolerates_sendbeacon_content_type(self):
        server._bx.update(token="tok-live", gen=1, at=time.time(), state="playing")
        with _Live() as live:
            r, _ = live.post(
                "/api/bx/stop", raw=json.dumps({"token": "tok-live"}).encode(),
                headers={"Content-Type": "text/plain"})
            self.assertEqual(r.status, 200)
        self.assertIsNone(server._bx["token"])

    def test_bx_stop_retires_a_still_buffering_job_with_no_bx_session(self):
        # bx_begin() only runs at the very end of run_browser_job -- a job
        # still probing/buffering has no _bx session at all yet, so the stop
        # has to find it by otoken through _jobs directly, independently of
        # whatever (nothing, here) _bx holds.
        server.job_set("m1", stage="buffering", owner="browser", otoken="tok-early")
        with _Live() as live:
            r, _ = live.post("/api/bx/stop", {"token": "tok-early", "gen": 1})
            self.assertEqual(r.status, 200)
        job = server.job_get("m1")
        self.assertEqual(job.get("stage"), "error")
        # The user-visible payoff: the TV is not left refusing to play
        # because active_job() still thinks a browser owns the player.
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (True, None))

    def test_bx_stop_with_wrong_token_leaves_job_untouched(self):
        server.job_set("m1", stage="buffering", owner="browser", otoken="tok-real")
        with _Live() as live:
            r, _ = live.post("/api/bx/stop", {"token": "tok-other", "gen": 1})
            self.assertEqual(r.status, 200)
        job = server.job_get("m1")
        self.assertEqual(job.get("stage"), "buffering")
        ok, msg = server.claim_owner("tv")
        self.assertEqual((ok, msg), (False, "Another device is playing"))


class _Live:
    """A real Cinematica server on a real loopback port -- see
    test_bx_serve.py's own _Live for why (Range/keep-alive/framing behaviour
    an in-process do_GET/do_POST call would not exercise the same way)."""

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
            return r, r.read()
        finally:
            c.close()

    def post(self, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            if raw is not None:
                data = raw
                h = dict(headers or {})
            else:
                data = json.dumps(body or {}).encode()
                h = {"Content-Type": "application/json"}
                h.update(headers or {})
            c.request("POST", path, body=data, headers=h)
            r = c.getresponse()
            return r, r.read()
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
