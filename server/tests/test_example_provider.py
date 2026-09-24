"""The documented example package, run the way Cinematica runs a real one.

docs/example-provider is what PROVIDERS.md sends a package author to read, so
it has to be a working package and not a sketch: this drives it through the
real runner.Pool -- a subprocess, providers/host.py, the line protocol, the
contract normalisers -- and checks that every operation its manifest promises
actually answers. A stale example is worse than none, and this is what makes
it fail loudly instead.

No network: the example's library is a JSON file inside the package.

Run: python3 -m unittest discover -s server/tests -t server
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from providers import contract, runner  # noqa: E402

PACKAGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "example-provider")
CONFIG = {"base_url": "http://mediabox.lan:8080/films"}
TIMEOUT = 20


class ExampleProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One worker, not the default three: this is a correctness test, and
        # three subprocesses would buy nothing but start-up time.
        cls.pool = runner.Pool("example-library", PACKAGE_DIR, CONFIG, "test-rev", workers=1)

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()

    def call(self, op, params=None):
        return self.pool.call(op, params or {}, TIMEOUT)

    def test_the_manifest_validates_and_declares_the_roles_the_code_implements(self):
        with open(os.path.join(PACKAGE_DIR, "manifest.json"), encoding="utf-8") as fh:
            manifest = contract.validate_manifest(json.load(fh))
        self.assertEqual(sorted(manifest["capabilities"]),
                         sorted([contract.ROLE_CATALOGUE, contract.ROLE_METADATA, contract.ROLE_STREAMS,
                                 contract.ROLE_CHANNELS]))
        # Every op the declared roles oblige it to answer must answer. This is
        # the check that catches an example whose manifest grew a role its
        # code never got.
        for op in sorted(contract.ops_for(manifest)):
            params = {}
            if op == contract.OP_DETAILS:
                params = {"id": "harbour-lights", "kind": contract.KIND_MOVIE}
            elif op == contract.OP_EPISODES:
                params = {"id": "harbour-lights", "season": 1}
            elif op == contract.OP_STREAMS:
                params = {"identity": {"local_id": "harbour-lights", "kind": contract.KIND_MOVIE}}
            elif op == contract.OP_SEARCH:
                params = {"query": "harbour", "limit": 5}
            elif op == contract.OP_CH_RESOLVE:
                params = {"query": "slow-rivers"}
            elif op in (contract.OP_CH_DETAILS, contract.OP_CH_LATEST):
                params = {"id": "slow-rivers"}
            elif op == contract.OP_CH_PLAY:
                params = {"id": "slow-rivers", "video": "sr-tide-notes"}
            self.call(op, params)  # raises ProviderError if it does not

    def test_browse_returns_normalised_entries_with_qualified_ids(self):
        page = self.call(contract.OP_BROWSE, {"kind": contract.KIND_MOVIE, "page": 1, "page_size": 2})
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])
        first = page["items"][0]
        self.assertEqual(first["id"], contract.qualify("example-library", "harbour-lights"))
        self.assertEqual(first["kind"], contract.KIND_MOVIE)
        last = self.call(contract.OP_BROWSE, {"kind": contract.KIND_MOVIE, "page": 2, "page_size": 2})
        self.assertFalse(last["has_more"])
        self.assertEqual(len(last["items"]), 1)

    def test_search_matches_on_title_and_details_answer_for_the_hit(self):
        hits = self.call(contract.OP_SEARCH, {"kind": contract.KIND_MOVIE, "query": "salt", "limit": 10})
        self.assertEqual([e["title"] for e in hits["items"]], ["The Salt Flats"])
        detail = self.call(contract.OP_DETAILS, {"id": "the-salt-flats", "kind": contract.KIND_MOVIE})
        # Normalised, so a year is the string the rest of the system renders.
        self.assertEqual(detail["year"], "1974")

    def test_streams_lookup_returns_a_playable_http_candidate(self):
        got = self.call(contract.OP_STREAMS,
                        {"identity": {"local_id": "night-signal", "kind": contract.KIND_MOVIE}})
        self.assertEqual(got["rejected"], 0)
        candidate = got["candidates"][0]
        self.assertEqual(candidate["transport"], contract.T_HTTP)
        self.assertEqual(candidate["url"], "http://mediabox.lan:8080/films/night-signal/night-signal.mp4")
        self.assertEqual(candidate["codec"], "H264")
        # public_candidate is what the TV is given: the upstream URL and any
        # headers stay on the server.
        self.assertNotIn("url", contract.public_candidate(candidate))

    def test_an_unknown_id_is_a_named_not_found_not_a_traceback(self):
        with self.assertRaises(contract.ProviderError) as caught:
            self.call(contract.OP_DETAILS, {"id": "no-such-film", "kind": contract.KIND_MOVIE})
        self.assertEqual(caught.exception.code, contract.E_NOTFOUND)

    def test_channels_latest_returns_uploads_newest_first(self):
        got = self.call(contract.OP_CH_LATEST, {"id": "slow-rivers"})
        self.assertEqual([v["id"] for v in got["videos"]], ["sr-culvert", "sr-flood-marks", "sr-tide-notes"])
        self.assertTrue(got["videos"][0]["published"] > got["videos"][1]["published"] > got["videos"][2]["published"])

    def test_channels_search_matches_on_title(self):
        hits = self.call(contract.OP_CH_SEARCH, {"query": "bench", "limit": 5})
        self.assertEqual([c["local_id"] for c in hits["items"]], ["bench-notes"])

    def test_channels_resolve_by_at_handle(self):
        got = self.call(contract.OP_CH_RESOLVE, {"query": "@bench-notes"})
        self.assertEqual(got["id"], contract.qualify("example-library", "bench-notes"))
        self.assertEqual(got["title"], "Bench Notes")

    def test_channels_play_returns_a_playable_url(self):
        got = self.call(contract.OP_CH_PLAY, {"id": "slow-rivers", "video": "sr-tide-notes"})
        self.assertEqual(got["url"], "http://mediabox.lan:8080/films/channels/slow-rivers/sr-tide-notes.mp4")
        self.assertEqual(got["label"], "the example player")

    def test_provider_describe_advertises_the_optional_channel_ops(self):
        described = self.call(contract.OP_DESCRIBE, {})
        self.assertEqual(set(described["channel_ops"]), {contract.OP_CH_SEARCH, contract.OP_CH_POPULAR})

    def test_without_a_base_url_the_failure_names_the_configuration(self):
        pool = runner.Pool("example-library", PACKAGE_DIR, {}, "test-rev-noconfig", workers=1)
        try:
            with self.assertRaises(contract.ProviderError) as caught:
                pool.call(contract.OP_TEST, {}, TIMEOUT)
            self.assertEqual(caught.exception.code, contract.E_CONFIG)
        finally:
            pool.close()


if __name__ == "__main__":
    unittest.main()
