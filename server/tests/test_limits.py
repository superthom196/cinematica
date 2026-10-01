"""Download limits: the speed and file-size lids set from the Settings page.

Rules under test:
- no lid leaves the picker as it was (MAX_GB_4K and the measured budget);
- the speed lid caps the budget, the well-seeded bonus included, so a file
  whose bitrate needs more than the lid is refused however fast the link is;
- the size lid refuses anything bigger, however long the film;
- a file never has to be bigger than the cache to be refused;
- saving drops the cached picks and pools, and persists beside the samples;
- POST /api/limits needs admin, clears a lid with 0/empty and rejects a lid
  so low it would refuse every release.

Run: python3 -m unittest discover -s server/tests -t server
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server  # noqa: E402
import catalogue  # noqa: E402
import config  # noqa: E402
import netprofile  # noqa: E402
import routes  # noqa: E402
import streams  # noqa: E402


def cand(gb, seeders=100):
    return {"codec": "HEVC", "is4k": True, "seeders": seeders, "gb": gb,
            "infoHash": "a" * 40, "pack": False, "display": "Film 2160p DDP5.1"}


def post(path, body):
    h = routes.H.__new__(routes.H)
    raw = json.dumps(body).encode()
    h.headers = {"Host": "localhost", "Content-Length": str(len(raw))}
    h.path = path
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h.close_connection = False
    h.command = "POST"
    sent = {}
    h._send = lambda code, b, ctype="application/json": sent.update(code=code, body=b)
    routes.H.do_POST(h)
    return sent["code"], sent["body"]


class LimitsBase(unittest.TestCase):
    def setUp(self):
        self._net = dict(netprofile._net)
        self._saved = []
        self.patches = [
            mock.patch.object(netprofile, "_net", {"samples": []}),
            mock.patch.object(config, "SUSTAIN_MBPS", 30.0),
            mock.patch.object(config, "MAX_GB_4K", 25.0),
            mock.patch.object(config, "CACHE_GB", 30.0),
            mock.patch.object(netprofile, "net_save", self._saved.append),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def lid(self, mbps=0, gb=0):
        with redirect_stdout(io.StringIO()):
            return netprofile.set_limits(mbps, gb)


class Picking(LimitsBase):
    def test_no_lid_passes_a_20gb_two_hour_film(self):
        # 20 GB over 120 min is ~22.8 Mbps, inside a 30 Mbps budget.
        self.assertGreater(streams.score(cand(20), runtime_min=120), 0)
        self.assertEqual(netprofile.max_gb(), 25.0)

    def test_speed_lid_refuses_the_same_film(self):
        self.lid(mbps=12)
        self.assertEqual(streams.score(cand(20), runtime_min=120), -1)
        # 8 GB over two hours is ~9.1 Mbps: still fine.
        self.assertGreater(streams.score(cand(8), runtime_min=120), 0)

    def test_speed_lid_caps_the_well_seeded_bonus(self):
        self.lid(mbps=12)
        self.assertEqual(netprofile.sustainable_mbps(config.WELL_SEEDED + 1), 12)
        self.assertEqual(netprofile.sustainable_mbps(None), 12)

    def test_speed_lid_above_the_budget_changes_nothing(self):
        self.lid(mbps=100)
        self.assertEqual(netprofile.sustainable_mbps(None), 30.0)

    def test_size_lid_refuses_anything_bigger_however_long(self):
        self.lid(gb=10)
        # 12 GB over three hours is only ~9 Mbps, but it is still 12 GB.
        self.assertEqual(streams.score(cand(12), runtime_min=180), -1)
        self.assertGreater(streams.score(cand(9.5), runtime_min=180), 0)

    def test_file_must_fit_in_the_cache(self):
        with mock.patch.object(config, "CACHE_GB", 15.0):
            self.assertEqual(netprofile.max_gb(), 15.0)
            self.assertEqual(streams.score(cand(18), runtime_min=240), -1)


class Saving(LimitsBase):
    def test_saving_drops_picks_and_pools_and_persists(self):
        streams._streams["x@1"] = {"at": 0, "pick": cand(20)}
        catalogue._pool["k"] = {"cands": []}
        got = self.lid(mbps=15, gb=12)
        self.assertEqual(streams._streams, {})
        self.assertEqual(catalogue._pool, {})
        self.assertEqual(self._saved[-1]["cap_mbps"], 15)
        self.assertEqual(self._saved[-1]["cap_gb"], 12)
        self.assertEqual(got["max_gb"], 12)

    def test_zero_clears(self):
        self.lid(mbps=15, gb=12)
        got = self.lid(0, 0)
        self.assertIsNone(got["cap_mbps"])
        self.assertIsNone(got["cap_gb"])
        self.assertEqual(got["max_gb"], 25.0)


class Route(LimitsBase):
    def setUp(self):
        super().setUp()
        self.admin = mock.patch.object(routes.H, "_require_admin", lambda self: "tok")
        self.admin.start()

    def tearDown(self):
        self.admin.stop()
        super().tearDown()

    def test_sets_and_clears(self):
        with redirect_stdout(io.StringIO()):
            code, body = post("/api/limits", {"cap_mbps": "15", "cap_gb": 12})
            self.assertEqual((code, body["cap_mbps"], body["cap_gb"]), (200, 15, 12))
            code, body = post("/api/limits", {"cap_mbps": "", "cap_gb": 0})
        self.assertEqual((code, body["cap_mbps"], body["cap_gb"]), (200, None, None))

    def test_rejects_a_lid_that_would_refuse_everything(self):
        for bad in ({"cap_mbps": 1}, {"cap_gb": 0.5}, {"cap_mbps": -3}, {"cap_gb": "lots"}):
            code, _ = post("/api/limits", bad)
            self.assertEqual(code, 400, bad)
        self.assertNotIn("cap_mbps", netprofile._net)

    def test_needs_admin(self):
        self.admin.stop()
        try:
            code, _ = post("/api/limits", {"cap_mbps": 15})
        finally:
            self.admin.start()
        self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
