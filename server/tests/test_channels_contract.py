"""The optional channels role: contract.py's normalisers (normalise_channel /
normalise_video / normalise_play), host.py's per-op normalisation for the
channels ops, and registry.setup_state's "channels never blocks configured"
rule.

No provider process here -- these normalisers and setup_state are pure
functions of their arguments, so unlike test_example_provider.py this drives
them directly rather than through a subprocess pool.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from providers import contract, host, registry  # noqa: E402


# =============================================================================
# contract.normalise_channel
# =============================================================================
class NormaliseChannelTest(unittest.TestCase):
    def test_good_input_is_qualified_and_carries_the_optional_fields(self):
        out = contract.normalise_channel(
            {"id": "slow-rivers", "title": "Slow Rivers", "avatar": "http://mediabox.lan/a.jpg",
             "subscribers": 4200, "description": "d", "latest_at": 1700000000},
            "example-library")
        self.assertEqual(out["id"], "example-library:slow-rivers")
        self.assertEqual(out["local_id"], "slow-rivers")
        self.assertEqual(out["kind"], "channel")
        self.assertEqual(out["title"], "Slow Rivers")
        self.assertEqual(out["avatar"], "http://mediabox.lan/a.jpg")
        self.assertEqual(out["banner"], "")
        self.assertEqual(out["subscribers"], 4200)
        self.assertEqual(out["latest_at"], 1700000000)

    def test_an_id_already_qualified_for_this_provider_keeps_its_local_part(self):
        out = contract.normalise_channel({"id": "example-library:slow-rivers", "title": "Slow Rivers"},
                                         "example-library")
        self.assertEqual(out["local_id"], "slow-rivers")
        self.assertEqual(out["id"], "example-library:slow-rivers")

    def test_missing_id_raises(self):
        with self.assertRaises(contract.ContractError):
            contract.normalise_channel({"title": "Slow Rivers"}, "example-library")

    def test_missing_title_raises(self):
        with self.assertRaises(contract.ContractError):
            contract.normalise_channel({"id": "slow-rivers"}, "example-library")

    def test_non_dict_raises(self):
        with self.assertRaises(contract.ContractError):
            contract.normalise_channel(["not", "a", "dict"], "example-library")


# =============================================================================
# contract.normalise_video
# =============================================================================
class NormaliseVideoTest(unittest.TestCase):
    def test_good_input_is_kept(self):
        out = contract.normalise_video(
            {"id": "sr-1", "title": "Tide Notes", "published": 1700000000, "duration_s": 612})
        self.assertEqual(out["id"], "sr-1")
        self.assertEqual(out["title"], "Tide Notes")
        self.assertEqual(out["published"], 1700000000)
        self.assertEqual(out["duration_s"], 612)

    def test_missing_published_defaults_to_zero_not_none(self):
        out = contract.normalise_video({"id": "sr-1", "title": "Tide Notes"})
        self.assertEqual(out["published"], 0)

    def test_missing_id_or_title_raises(self):
        with self.assertRaises(contract.ContractError):
            contract.normalise_video({"title": "Tide Notes"})
        with self.assertRaises(contract.ContractError):
            contract.normalise_video({"id": "sr-1"})


# =============================================================================
# contract.normalise_play
# =============================================================================
class NormalisePlayTest(unittest.TestCase):
    def test_good_http_url_is_kept_with_defaults(self):
        out = contract.normalise_play({"url": "http://mediabox.lan/channels/slow-rivers/sr-1.mp4"})
        self.assertEqual(out["url"], "http://mediabox.lan/channels/slow-rivers/sr-1.mp4")
        self.assertEqual(out["package"], "")
        self.assertEqual(out["label"], "another app")

    def test_package_and_label_pass_through(self):
        out = contract.normalise_play(
            {"url": "https://mediabox.lan/x.mp4", "package": "com.example.player", "label": "the example player"})
        self.assertEqual(out["package"], "com.example.player")
        self.assertEqual(out["label"], "the example player")

    def test_non_http_scheme_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            contract.normalise_play({"url": "ftp://mediabox.lan/x.mp4"})

    def test_missing_url_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            contract.normalise_play({"package": "com.example.player"})


# =============================================================================
# host._normalise_result -- channels ops
# =============================================================================
class HostNormaliseChannelsResultTest(unittest.TestCase):
    def test_resolve_normalises_a_single_channel(self):
        out = host._normalise_result(contract.OP_CH_RESOLVE, {"id": "slow-rivers", "title": "Slow Rivers"},
                                     "example-library", {})
        self.assertEqual(out["id"], "example-library:slow-rivers")

    def test_search_skips_bad_rows_instead_of_failing_the_whole_list(self):
        result = {"items": [{"id": "slow-rivers", "title": "Slow Rivers"},
                            {"id": "no-title-here"},   # missing title -- dropped, not fatal
                            {"id": "bench-notes", "title": "Bench Notes"}]}
        out = host._normalise_result(contract.OP_CH_SEARCH, result, "example-library", {})
        self.assertEqual([c["local_id"] for c in out["items"]], ["slow-rivers", "bench-notes"])

    def test_search_accepts_a_bare_list_too(self):
        out = host._normalise_result(contract.OP_CH_POPULAR,
                                     [{"id": "slow-rivers", "title": "Slow Rivers"}], "example-library", {})
        self.assertEqual(len(out["items"]), 1)

    def test_latest_normalises_videos_and_reports_no_next_page(self):
        result = {"videos": [{"id": "sr-1", "title": "Tide Notes", "published": 1700000000},
                             {"id": "bad"}]}   # missing title -- dropped
        out = host._normalise_result(contract.OP_CH_LATEST, result, "example-library", {})
        self.assertEqual([v["id"] for v in out["videos"]], ["sr-1"])
        self.assertIsNone(out["next"])

    def test_videos_op_carries_a_next_page_token(self):
        result = {"videos": [{"id": "sr-1", "title": "Tide Notes"}], "next": "page-2"}
        out = host._normalise_result(contract.OP_CH_VIDEOS, result, "example-library", {})
        self.assertEqual(out["next"], "page-2")

    def test_play_normalises_the_url(self):
        out = host._normalise_result(contract.OP_CH_PLAY, {"url": "http://mediabox.lan/x.mp4"},
                                     "example-library", {})
        self.assertEqual(out["url"], "http://mediabox.lan/x.mp4")

    def test_a_non_dict_non_list_result_raises(self):
        with self.assertRaises(contract.ContractError):
            host._normalise_result(contract.OP_CH_RESOLVE, "not an object", "example-library", {})


# =============================================================================
# registry.setup_state -- channels never blocks "configured"
# =============================================================================
def _core_manifest(pid):
    """catalogue + metadata + streams, no required config -- ready the
    moment it is installed, so setup_state's "configured" turns on the three
    core roles alone."""
    return {"id": pid, "version": "1.0.0", "contract": contract.CONTRACT_VERSION,
            "capabilities": [contract.ROLE_CATALOGUE, contract.ROLE_METADATA, contract.ROLE_STREAMS]}


def _channels_manifest(pid):
    """A channels-only provider with a required field it is never given here
    -- stays needs_config on purpose, to prove that alone cannot flip
    "configured" to False."""
    return {"id": pid, "version": "1.0.0", "contract": contract.CONTRACT_VERSION,
            "capabilities": [contract.ROLE_CHANNELS],
            "config": [{"key": "api_key", "type": contract.F_SECRET, "required": True}]}


class SetupStateChannelsOptionalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="cinematica-channels-test-")
        os.environ["CINEMATICA_STATE"] = self._tmp
        registry.install(_core_manifest("core-provider"), source="package")
        for role in contract.CORE_ROLES:
            registry.set_active(role, "core-provider")

    def tearDown(self):
        os.environ.pop("CINEMATICA_STATE", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_configured_with_channels_left_entirely_unset(self):
        state = registry.setup_state()
        self.assertTrue(state["configured"])
        self.assertIsNone(state["roles"][contract.ROLE_CHANNELS])

    def test_configured_stays_true_when_the_channels_provider_still_needs_config(self):
        registry.install(_channels_manifest("channels-provider"), source="package")
        registry.set_active(contract.ROLE_CHANNELS, "channels-provider")
        # Sanity check on the premise: this provider really is not ready.
        self.assertEqual(registry.status("channels-provider")[0], "needs_config")

        state = registry.setup_state()
        self.assertTrue(state["configured"])
        self.assertEqual(state["roles"][contract.ROLE_CHANNELS], "channels-provider")


if __name__ == "__main__":
    unittest.main()
