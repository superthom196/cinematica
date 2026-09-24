"""The provider system: the generic Stremio-add-on adapter (providers/addon.py),
the wire contract (providers/contract.py) and the installed-set/secrets store
(providers/registry.py, providers/store.py).

Everything here drives tests.addon_stub.StubAddon, a deterministic in-process
Stremio add-on -- no test may touch the network. See addon_stub.py's module
docstring for why a stub, not a real add-on.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import addon_stub  # noqa: E402
from providers import addon, contract, gateway, host, registry, runner, store  # noqa: E402


class _StateIsolatedTestCase(unittest.TestCase):
    """Every provider test touches, or could accidentally touch, disk state
    (providers.json / secrets.json) via registry.py / store.py -- store.py's
    state_dir() reads CINEMATICA_STATE fresh on every call, so pointing it at
    a scratch directory per test is enough to guarantee the real
    /var/lib/cinematica is never written."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="cinematica-provider-test-")
        os.environ["CINEMATICA_STATE"] = self._tmp

    def tearDown(self):
        os.environ.pop("CINEMATICA_STATE", None)
        shutil.rmtree(self._tmp, ignore_errors=True)


def _provider_for(stub):
    """Fetch stub's live manifest, build the validated provider manifest, and
    return a ready AddonProvider -- the setup every adapter test starts from.

    Uses addon._http_get_json (not addon.fetch_manifest) so this bypasses
    addon.py's own manifest cache: the AddonProvider under test must do its
    own first real fetch when a test later flips stub.fail_with.
    """
    raw = addon._http_get_json(stub.manifest_url)
    manifest = addon.to_provider_manifest(raw, stub.manifest_url)
    return addon.AddonProvider(manifest, {})


# =============================================================================
# Adapter against the stub
# =============================================================================
class AdapterAgainstStubTest(_StateIsolatedTestCase):
    def test_manifest_discovery_passes_validate_manifest_with_roles_from_resources(self):
        with addon_stub.StubAddon(resources=("catalog", "meta", "stream")).start() as stub:
            raw = addon._http_get_json(stub.manifest_url)
            manifest = addon.to_provider_manifest(raw, stub.manifest_url)

            # Re-validating an already-validated manifest must be a no-op.
            self.assertEqual(contract.validate_manifest(manifest), manifest)
            self.assertEqual(manifest["id"], "org-cinematica-stub")
            self.assertEqual(
                manifest["capabilities"],
                sorted([contract.ROLE_CATALOGUE, contract.ROLE_METADATA, contract.ROLE_STREAMS]))
            self.assertEqual(manifest["runtime"], "addon")
            self.assertEqual(manifest["addon_url"], stub.base_url)

    def test_browse_paging_second_page_differs_and_last_page_is_short(self):
        with addon_stub.StubAddon(catalog_size=45).start() as stub:
            provider = _provider_for(stub)

            page1 = provider.browse(contract.KIND_MOVIE, page=1, page_size=20)
            page2 = provider.browse(contract.KIND_MOVIE, page=2, page_size=20)
            page3 = provider.browse(contract.KIND_MOVIE, page=3, page_size=20)

            self.assertEqual(len(page1["entries"]), 20)
            self.assertEqual(len(page2["entries"]), 20)
            self.assertEqual(len(page3["entries"]), 5)
            self.assertEqual(page1["entries"][0]["local_id"], "tt9000001")
            self.assertEqual(page2["entries"][0]["local_id"], "tt9000021")
            self.assertEqual(page3["entries"][0]["local_id"], "tt9000041")
            self.assertNotEqual(page1["entries"][0]["local_id"], page2["entries"][0]["local_id"])
            self.assertTrue(page1["has_more"])
            self.assertTrue(page2["has_more"])
            self.assertFalse(page3["has_more"])

    def test_genre_filter_and_search_both_reach_the_addon(self):
        with addon_stub.StubAddon(catalog_size=40).start() as stub:
            provider = _provider_for(stub)

            browsed = provider.browse(contract.KIND_MOVIE, page=1, page_size=20,
                                       filters={"genre_ids": ["Action"]})
            # index % 3 == 0 (Action) across 40 entries -> 0,3,...,39: 14 rows.
            self.assertEqual(len(browsed["entries"]), 14)
            self.assertTrue(all(e["genres"] == ["Action"] for e in browsed["entries"]))
            self.assertTrue(any("genre=Action" in r for r in stub.requests))

            searched = provider.search(contract.KIND_MOVIE, "Stub Movie 10")
            self.assertEqual([e["local_id"] for e in searched["entries"]], ["tt9000010"])
            self.assertTrue(any("search=Stub+Movie+10" in r for r in stub.requests))

    def test_series_details_return_seasons_and_episodes_filter_by_season(self):
        with addon_stub.StubAddon().start() as stub:
            provider = _provider_for(stub)

            detail = provider.details("tt9000001", contract.KIND_SERIES)
            self.assertEqual(
                detail["seasons"],
                [{"n": 1, "name": "Season 1", "episodes": 3, "air": "", "poster": None},
                 {"n": 2, "name": "Season 2", "episodes": 3, "air": "", "poster": None}])
            self.assertEqual(len(detail["episodes"]), 6)

            season1 = provider.episodes("tt9000001", 1)
            self.assertEqual(sorted(e["episode"] for e in season1["episodes"]), [1, 2, 3])
            self.assertTrue(all(e["season"] == 1 for e in season1["episodes"]))
            self.assertEqual(
                sorted(e["name"] for e in season1["episodes"]),
                ["S01E01", "S01E02", "S01E03"])

    def test_stream_lookup_for_series_episode_sends_composite_id(self):
        with addon_stub.StubAddon().start() as stub:
            provider = _provider_for(stub)
            identity = {"kind": contract.KIND_SERIES, "local_id": "tt9000001", "external_ids": {}}

            provider.streams(identity, season=1, episode=2)

            self.assertIn("/stream/series/tt9000001:1:2.json", stub.requests)

    def test_unsupported_ytid_stream_is_refused_by_name_and_counted(self):
        with addon_stub.StubAddon().start() as stub:
            provider = _provider_for(stub)
            identity = {"kind": contract.KIND_MOVIE, "local_id": "tt9000001", "external_ids": {}}

            result = provider.streams(identity)

            self.assertEqual(len(result["candidates"]), 1)
            self.assertEqual(result["rejected"], {"ytId (not a playable transport)": 1})

    def test_http_stream_carries_proxy_headers_and_public_candidate_strips_them(self):
        with addon_stub.StubAddon(transport="http",
                                   require_header=("X-Test-Auth", "secret-token-xyz")).start() as stub:
            provider = _provider_for(stub)
            identity = {"kind": contract.KIND_MOVIE, "local_id": "tt9000001", "external_ids": {}}

            result = provider.streams(identity)
            self.assertEqual(len(result["candidates"]), 1)
            cand = result["candidates"][0]

            self.assertEqual(cand["transport"], contract.T_HTTP)
            self.assertEqual(cand["headers"], {"X-Test-Auth": "secret-token-xyz"})
            self.assertTrue(cand["url"].startswith(stub.base_url + "/media/"))

            public = contract.public_candidate(cand)
            self.assertNotIn("url", public)
            self.assertNotIn("headers", public)
            self.assertEqual(public["source"], "direct")
            self.assertEqual(public["transport"], contract.T_HTTP)


# =============================================================================
# Failure modes
# =============================================================================
class FailureModesTest(_StateIsolatedTestCase):
    def test_fail_with_500_raises_provider_error_not_a_traceback(self):
        with addon_stub.StubAddon().start() as stub:
            provider = _provider_for(stub)
            stub.fail_with = "500"

            with self.assertRaises(contract.ProviderError) as ctx:
                provider.details("tt9000001", contract.KIND_MOVIE)
            self.assertEqual(ctx.exception.code, contract.E_UPSTREAM)

    def test_fail_with_garbage_raises_provider_error_e_protocol(self):
        with addon_stub.StubAddon().start() as stub:
            provider = _provider_for(stub)
            stub.fail_with = "garbage"

            with self.assertRaises(contract.ProviderError) as ctx:
                provider.details("tt9000001", contract.KIND_MOVIE)
            self.assertEqual(ctx.exception.code, contract.E_PROTOCOL)

    def test_fail_with_empty_returns_empty_result_not_an_exception(self):
        with addon_stub.StubAddon().start() as stub:
            provider = _provider_for(stub)
            stub.fail_with = "empty"

            result = provider.browse(contract.KIND_MOVIE, page=1, page_size=20)

            self.assertEqual(result, {"entries": [], "has_more": False})

    def test_catalog_only_addon_is_not_usable_for_metadata_or_streams_role(self):
        with addon_stub.StubAddon(resources=("catalog",)).start() as stub:
            raw = addon._http_get_json(stub.manifest_url)
            manifest = addon.to_provider_manifest(raw, stub.manifest_url)
            self.assertEqual(manifest["capabilities"], [contract.ROLE_CATALOGUE])

            registry.install(manifest, source=manifest["addon_url"])

            registry.set_active(contract.ROLE_CATALOGUE, manifest["id"])
            self.assertEqual(registry.active(contract.ROLE_CATALOGUE), manifest["id"])

            with self.assertRaises(ValueError):
                registry.set_active(contract.ROLE_METADATA, manifest["id"])
            with self.assertRaises(ValueError):
                registry.set_active(contract.ROLE_STREAMS, manifest["id"])


# =============================================================================
# Registry and secrets
# =============================================================================
def _secret_manifest(pid):
    return {
        "id": pid,
        "version": "1.0.0",
        "contract": contract.CONTRACT_VERSION,
        "capabilities": [contract.ROLE_CATALOGUE],
        "config": [
            {"key": "api_key", "type": contract.F_SECRET, "required": True},
            {"key": "region", "type": contract.F_TEXT, "required": False, "default": "US"},
        ],
    }


class RegistryAndSecretsTest(_StateIsolatedTestCase):
    def setUp(self):
        super().setUp()
        registry.install(_secret_manifest("acme-provider"), source="package")

    def test_config_save_omitting_secret_key_keeps_the_stored_secret(self):
        registry.set_config("acme-provider", {"api_key": "sk-original-value"})

        # Save again without the secret key -- must not wipe it.
        registry.set_config("acme-provider", {"region": "GB"})

        cfg = registry.config_for("acme-provider")
        self.assertEqual(cfg["api_key"], "sk-original-value")
        self.assertEqual(cfg["region"], "GB")

    def test_clear_keys_actually_removes_the_secret(self):
        registry.set_config("acme-provider", {"api_key": "sk-original-value"})
        registry.set_config("acme-provider", {}, clear_keys=("api_key",))

        self.assertIsNone(registry.config_for("acme-provider").get("api_key"))

    def test_public_config_never_returns_a_secret_value(self):
        registry.set_config("acme-provider", {"api_key": "sk-original-value", "region": "GB"})

        pub = registry.public_config("acme-provider")

        self.assertEqual(pub["api_key"], contract.mask("sk-original-value"))
        self.assertNotIn("sk-original-value", str(pub["api_key"]))
        self.assertEqual(pub["region"], "GB")  # non-secret field passes through raw

    def test_status_goes_from_needs_config_to_ready_as_required_fields_fill(self):
        self.assertEqual(registry.status("acme-provider")[0], "needs_config")

        registry.set_config("acme-provider", {"api_key": "sk-original-value"})

        self.assertEqual(registry.status("acme-provider")[0], "ready")

    def test_switching_active_and_editing_config_both_change_config_revision(self):
        registry.install(_secret_manifest("second-provider"), source="package")
        registry.set_config("acme-provider", {"api_key": "sk-a"})
        registry.set_config("second-provider", {"api_key": "sk-b"})

        registry.set_active(contract.ROLE_CATALOGUE, "acme-provider")
        rev_a = registry.config_revision("acme-provider")

        registry.set_active(contract.ROLE_CATALOGUE, "second-provider")
        rev_b = registry.config_revision("second-provider")

        self.assertNotEqual(rev_a, rev_b)
        # acme-provider's own revision is untouched by switching who is active.
        self.assertEqual(registry.config_revision("acme-provider"), rev_a)

        # Editing a provider's config changes its revision too.
        registry.set_config("second-provider", {"region": "FR"})
        rev_b2 = registry.config_revision("second-provider")
        self.assertNotEqual(rev_b, rev_b2)


# =============================================================================
# Redaction
# =============================================================================
class RedactionTest(_StateIsolatedTestCase):
    def test_redact_strips_a_stored_secret_given_all_secret_values(self):
        store.set_secrets("acme-provider", {"api_key": "sk-opaque-value-not-url-shaped"})

        message = "upstream call failed with credential sk-opaque-value-not-url-shaped attached"
        redacted = contract.redact(message, extra_secrets=store.all_secret_values())

        self.assertNotIn("sk-opaque-value-not-url-shaped", redacted)
        self.assertIn("[redacted]", redacted)

    def test_provider_error_built_with_redacted_message_does_not_leak_secret(self):
        store.set_secrets("acme-provider", {"api_key": "sk-opaque-value-not-url-shaped"})
        secrets_now = store.all_secret_values()

        raw_message = "auth rejected for key sk-opaque-value-not-url-shaped"
        exc = contract.ProviderError(
            contract.E_AUTH, contract.redact(raw_message, extra_secrets=secrets_now))

        self.assertNotIn("sk-opaque-value-not-url-shaped", str(exc))


# =============================================================================
# Cache identity
# =============================================================================
def _addon_manifest(pid, addon_url, version="1.0.0"):
    return {
        "id": pid,
        "version": version,
        "contract": contract.CONTRACT_VERSION,
        "capabilities": [contract.ROLE_CATALOGUE],
        "runtime": "addon",
        "addon_url": addon_url,
        "config": [],
    }


class CacheIdentityTest(_StateIsolatedTestCase):
    """A Stremio add-on's configuration is its URL. The revision that keys every
    cache has to move when that URL does, or a re-pointed provider keeps serving
    the previous configuration's results."""

    CONFIGURED = "https://addon.example/apikey=FIRST_KEY|sort=quality/manifest.json"
    RECONFIGURED = "https://addon.example/apikey=SECOND_KEY|sort=size/manifest.json"

    def test_repointing_an_addon_at_a_new_url_changes_the_config_revision(self):
        registry.install(_addon_manifest("addon-x", self.CONFIGURED))
        before = registry.config_revision("addon-x")

        # Nothing else moves: same provider id (it is slugified from the
        # add-on's own id), same manifest version, same empty config. The URL
        # is the only thing that changed, and it is the thing that changed what
        # the provider will return.
        registry.install(_addon_manifest("addon-x", self.RECONFIGURED))

        self.assertNotEqual(before, registry.config_revision("addon-x"))

    def test_the_revision_leaks_nothing_of_the_credential_in_the_url(self):
        registry.install(_addon_manifest("addon-x", self.CONFIGURED))

        rev = registry.config_revision("addon-x")

        # It goes into cache keys and into /api/providers, so it has to be a
        # digest of the URL and never any part of the URL itself.
        self.assertNotIn("FIRST_KEY", rev)
        self.assertNotIn("apikey", rev)

    def test_the_same_url_still_gives_the_same_revision(self):
        """The revision keys caches; if it moved on its own, nothing would ever
        hit one."""
        registry.install(_addon_manifest("addon-x", self.CONFIGURED))
        first = registry.config_revision("addon-x")
        registry.install(_addon_manifest("addon-x", self.CONFIGURED))

        self.assertEqual(first, registry.config_revision("addon-x"))


# =============================================================================
# Updating an installed package
# =============================================================================
class PackageUpdateTest(_StateIsolatedTestCase):
    """An update that fails has to leave the previous version installed. The
    files are the provider -- lose them and there is nothing to run and no way
    back except re-fetching the package."""

    MANIFEST = {"entry": "provider.py"}

    def _installed_source(self):
        with open(os.path.join(store.provider_dir("pkg"), "provider.py")) as f:
            return f.read()

    def setUp(self):
        super().setUp()
        self.v1 = tempfile.mkdtemp(prefix="cinematica-pkg-v1-")
        with open(os.path.join(self.v1, "provider.py"), "w") as f:
            f.write("VERSION = 1\n")
        registry.swap_in_files("pkg", self.v1, self.MANIFEST)

    def tearDown(self):
        shutil.rmtree(self.v1, ignore_errors=True)
        super().tearDown()

    def test_a_good_update_replaces_the_files(self):
        v2 = tempfile.mkdtemp(prefix="cinematica-pkg-v2-")
        try:
            with open(os.path.join(v2, "provider.py"), "w") as f:
                f.write("VERSION = 2\n")
            registry.swap_in_files("pkg", v2, self.MANIFEST)
        finally:
            shutil.rmtree(v2, ignore_errors=True)

        self.assertEqual(self._installed_source(), "VERSION = 2\n")

    def test_a_replacement_that_does_not_validate_leaves_version_1_installed(self):
        broken = tempfile.mkdtemp(prefix="cinematica-pkg-broken-")
        try:
            # Arrives without the entry file the manifest declares -- a
            # truncated upload, or a package built wrong.
            with open(os.path.join(broken, "README"), "w") as f:
                f.write("no provider.py here\n")
            with self.assertRaises(Exception):
                registry.swap_in_files("pkg", broken, self.MANIFEST)
        finally:
            shutil.rmtree(broken, ignore_errors=True)

        self.assertEqual(self._installed_source(), "VERSION = 1\n")

    def test_a_replacement_that_cannot_be_copied_leaves_version_1_installed(self):
        """The failure the old spelling could not survive: the copy itself
        going wrong, after the old tree had already been deleted."""
        with self.assertRaises(Exception):
            registry.swap_in_files("pkg", os.path.join(self.v1, "does-not-exist"),
                                   self.MANIFEST)

        self.assertEqual(self._installed_source(), "VERSION = 1\n")

    def test_a_failed_update_leaves_no_staging_directories_behind(self):
        broken = tempfile.mkdtemp(prefix="cinematica-pkg-broken-")
        try:
            with self.assertRaises(Exception):
                registry.swap_in_files("pkg", broken, self.MANIFEST)
        finally:
            shutil.rmtree(broken, ignore_errors=True)

        self.assertEqual(sorted(os.listdir(store.providers_dir())), ["pkg"])


# =============================================================================
# Configured URLs are credentials
# =============================================================================
class ConfiguredUrlMaskingTest(_StateIsolatedTestCase):
    """A debrid API key reaches this install as a path segment of an add-on's
    manifest URL. It is a credential wherever it is written down, so it is
    masked in API responses and redacted out of error text, exactly as a
    declared secret field would be."""

    URL = "https://addon.example/apikey=SUPERSECRETKEY|sort=quality/manifest.json"
    PUBLIC = "https://v3-cinemeta.strem.io/manifest.json"

    def test_mask_url_keeps_the_host_and_drops_the_configuration(self):
        masked = contract.mask_url(self.URL)

        self.assertNotIn("SUPERSECRETKEY", masked)
        # The host stays: an admin has to be able to tell which add-on this is.
        self.assertEqual(masked, "https://addon.example/[redacted]/manifest.json")

    def test_a_public_addon_with_nothing_configured_is_not_masked(self):
        """Masking what carries no credential would only make the settings page
        harder to read."""
        self.assertEqual(contract.mask_url(self.PUBLIC), self.PUBLIC)
        self.assertEqual(contract.url_secrets(self.PUBLIC), [])

    def test_userinfo_and_query_credentials_are_masked_too(self):
        masked = contract.mask_url("https://user:hunter2pass@addon.example/x/manifest.json?token=abcdef123456")

        self.assertNotIn("hunter2pass", masked)
        self.assertNotIn("abcdef123456", masked)
        self.assertIn("addon.example", masked)

    def test_the_url_credential_is_registered_for_literal_redaction(self):
        registry.install(_addon_manifest("addon-x", self.URL), source=self.URL)

        # The pattern pass cannot see this one: it is not userinfo, not a query
        # parameter and not a Bearer header, just a path segment.
        message = "add-on returned HTTP 500 for %s" % self.URL
        redacted = contract.redact(message, gateway.secret_values())

        self.assertNotIn("SUPERSECRETKEY", redacted)
        # ...but the host survives, so the message still says which add-on failed.
        self.assertIn("addon.example", redacted)

    def test_redaction_does_not_blank_an_addon_that_has_no_credential(self):
        registry.install(_addon_manifest("addon-y", self.PUBLIC), source=self.PUBLIC)

        message = "add-on returned HTTP 500 for %s" % self.PUBLIC
        self.assertEqual(contract.redact(message, gateway.secret_values()), message)

    def test_a_package_provider_source_is_left_alone(self):
        self.assertEqual(contract.mask_url("package"), "package")


class LoginThrottleTest(unittest.TestCase):
    def setUp(self):
        store._login_fails.clear()

    def tearDown(self):
        store._login_fails.clear()

    def test_first_misses_are_free(self):
        for _ in range(store._LOGIN_FREE - 1):
            store.note_login("10.0.0.9", False)
        self.assertEqual(store.login_wait("10.0.0.9"), 0)

    def test_backoff_after_free_misses_and_only_for_that_client(self):
        for _ in range(store._LOGIN_FREE + 3):
            store.note_login("10.0.0.9", False)
        self.assertGreater(store.login_wait("10.0.0.9"), 0)
        self.assertEqual(store.login_wait("10.0.0.10"), 0)

    def test_wait_is_capped(self):
        store._login_fails["10.0.0.9"] = (store._LOGIN_FREE + 60, store.time.time())
        self.assertLessEqual(store.login_wait("10.0.0.9"), store._LOGIN_MAX_WAIT)

    def test_success_clears_the_record(self):
        for _ in range(store._LOGIN_FREE + 3):
            store.note_login("10.0.0.9", False)
        store.note_login("10.0.0.9", True)
        self.assertEqual(store.login_wait("10.0.0.9"), 0)


class ProviderAccountTest(_StateIsolatedTestCase):
    """Python providers run as CINEMATICA_PROVIDER_USER when the installer set
    one, and exactly as before when nothing did."""

    def tearDown(self):
        os.environ.pop("CINEMATICA_PROVIDER_USER", None)
        super().tearDown()

    def test_no_account_runs_python_directly(self):
        argv, env = runner._worker_argv("/pkg")
        self.assertEqual(argv, [sys.executable, "-m", "providers.host", "/pkg"])
        self.assertNotIn("CINEMATICA_DIE_WITH_PARENT", env)

    def test_account_drops_through_sudo_with_its_own_home(self):
        os.environ["CINEMATICA_PROVIDER_USER"] = "cinematica-provider"
        home = type("pw", (), {"pw_dir": "/var/lib/cinematica-provider"})()
        with mock.patch.object(runner.pwd, "getpwnam", return_value=home):
            argv, _ = runner._worker_argv("/pkg")
        self.assertEqual(argv[:7], ["sudo", "-n", "-u", "cinematica-provider", "--", "env", "-i"])
        self.assertEqual(argv[-4:], [sys.executable, "-m", "providers.host", "/pkg"])
        self.assertIn("HOME=/var/lib/cinematica-provider", argv)
        self.assertIn("CINEMATICA_DIE_WITH_PARENT=1", argv)
        self.assertIn("PYTHONPATH=" + runner._SERVER_DIR, argv)

    def test_state_tree_is_searchable_not_listable_with_an_account(self):
        store.ensure_dirs()
        self.assertEqual(os.stat(store.providers_dir()).st_mode & 0o777, 0o700)
        os.environ["CINEMATICA_PROVIDER_USER"] = "cinematica-provider"
        store.ensure_dirs()
        self.assertEqual(os.stat(store.state_dir()).st_mode & 0o777, 0o711)
        self.assertEqual(os.stat(store.providers_dir()).st_mode & 0o777, 0o711)

    def test_installed_package_is_readable_by_another_account(self):
        src = tempfile.mkdtemp(prefix="cinematica-pkg-", dir=self._tmp)  # 0700, like an unpacked upload
        os.makedirs(os.path.join(src, "lib"))
        for name, mode in (("main.py", 0o600), (os.path.join("lib", "tool"), 0o700)):
            with open(os.path.join(src, name), "w") as f:
                f.write("")
            os.chmod(os.path.join(src, name), mode)
        manifest = {"entry": "main.py"}
        registry.swap_in_files("pkgperm", src, manifest)
        dest = store.provider_dir("pkgperm")
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(os.path.join(dest, "main.py")).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(os.path.join(dest, "lib", "tool")).st_mode & 0o777, 0o755)

    def test_host_arms_nothing_outside_sudo(self):
        with mock.patch.dict(os.environ, {"CINEMATICA_DIE_WITH_PARENT": ""}):
            host._die_with_parent()  # must return, not exit


class WorkerKillTest(unittest.TestCase):
    def test_a_killed_worker_is_reaped_not_left_a_zombie(self):
        """Pool.call() kills a worker on a timeout, an oversize reply or a
        malformed one, then close()s it -- but kill() marks it closed, so
        close() returns at once. kill() itself has to wait for the child."""
        import subprocess
        w = runner._Worker.__new__(runner._Worker)
        w.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  stdin=subprocess.PIPE)
        w._closed = False
        w.kill()
        w.close()
        self.assertIsNotNone(w.proc.returncode)


if __name__ == "__main__":
    unittest.main()
