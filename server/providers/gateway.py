"""The single seam server.py calls into the provider system through.

Everything server.py used to know about providers directly -- which one is
active, how to reach it, how long to wait, how often it may be hit -- lives
here instead. server.py must never import addon.py or runner.py: a provider
is either a Stremio add-on (addon.AddonProvider, HTTP) or an installed Python
package (runner.PoolManager, a subprocess pool), and collapsing that choice
into one place is what lets server.py ask "browse the catalogue" without
caring which transport answers.

Three jobs, in order, on every call:
  1. Resolve "the active provider for this role" (registry.py owns that,
     including the metadata-falls-back-to-catalogue rule -- reused here, not
     reimplemented).
  2. Get (or build) a live instance for that provider and dispatch the op,
     under a per-provider throttle and a per-op deadline.
  3. Normalise the outcome: a real result in the exact contract shape, or a
     contract.ProviderError with .provider/.op set and its message redacted --
     never a bare exception from someone else's code, and never a quietly
     empty result standing in for "not configured".
"""

import threading
import time

from . import addon, contract, registry, runner, store

# ---- timeouts ----------------------------------------------------------------
# Per op, not one global number, because a details lookup and a stream search
# have very different honest costs -- and named as constants (not inlined)
# because the first time one of these needs tuning it will be from an
# incident, not a design review.
TIMEOUT_BROWSE_S = 20.0
TIMEOUT_SEARCH_S = 20.0
TIMEOUT_GENRES_S = 20.0     # a catalogue op; grouped with browse/search
TIMEOUT_DETAILS_S = 15.0
TIMEOUT_EPISODES_S = 15.0
TIMEOUT_STREAMS_S = 25.0
TIMEOUT_TEST_S = 15.0

# These constants are the timeout actually enforced against a package
# provider (runner.Pool.call() takes it as a hard parameter). An add-on
# (addon.py) enforces its own fixed ~15s per HTTP round trip internally and
# takes no timeout argument -- not overridden here, since doing that would
# mean either editing addon.py or wrapping every call in a second, redundant
# deadline thread for no real gain.

# ---- per-provider outbound throttle -------------------------------------------
# Replaces server.py's single-service throttle with a generic, per-provider
# version. WHY it exists at all: stream indexes rate-limit, and a 429
# used to be cached as "no usable stream" for three hours -- silently deleting
# films from the catalogue. Spacing a provider's own outbound calls out here,
# once, protects every op that reaches it instead of one hand-wired call site.
# Default is 0 (no wait) -- most providers (a local package, a well-behaved
# add-on) need none, and a wait nobody asked for is itself a regression.
_throttle_gap_s = {}        # provider_id -> configured minimum gap, seconds
_throttle_last = {}         # provider_id -> monotonic time of its last call
_throttle_locks = {}        # provider_id -> Lock, so provider A sleeping here
                             # never blocks provider B's unrelated calls
_throttle_setup_lock = threading.Lock()   # guards the two dicts above only --
                                            # never held across a sleep


def set_throttle(provider_id, seconds):
    """Configure provider_id's minimum gap between outbound calls. 0 (or a
    falsy value) clears it."""
    with _throttle_setup_lock:
        if seconds and seconds > 0:
            _throttle_gap_s[provider_id] = float(seconds)
        else:
            _throttle_gap_s.pop(provider_id, None)


def _throttle_lock_for(provider_id):
    with _throttle_setup_lock:
        lk = _throttle_locks.get(provider_id)
        if lk is None:
            lk = threading.Lock()
            _throttle_locks[provider_id] = lk
        return lk


def _throttle(provider_id):
    gap = _throttle_gap_s.get(provider_id, 0.0)
    if gap <= 0:
        return
    with _throttle_lock_for(provider_id):
        wait = gap - (time.monotonic() - _throttle_last.get(provider_id, 0.0))
        if wait > 0:
            time.sleep(wait)
        _throttle_last[provider_id] = time.monotonic()


# ---- instance cache ------------------------------------------------------------
# One live instance per (provider_id, config_rev), built lazily and reused --
# an AddonProvider is cheap, but a package provider is a subprocess pool, and
# rebuilding that on every call would mean paying a process-spawn cost per
# browse. Keyed by config_rev (not just provider_id) so a settings-page save
# is picked up on the NEXT call rather than needing an explicit restart; the
# actual drop-on-change happens in invalidate(), which callers run after any
# config/activation write.
_lock = threading.RLock()
_instances = {}             # provider_id -> (config_rev, instance)
_pool_manager = runner.PoolManager()


class _PackageAdapter:
    """Wraps a runner.Pool so it answers the exact same method shapes as
    addon.AddonProvider -- the only difference the rest of this module ever
    sees between an add-on and a package provider is which class got built.

    host.py already runs every reply through contract's normalisers inside
    the subprocess (see providers/host.py's _normalise_result), so what comes
    back over the pipe is contract-shaped -- except for two spots where the
    wire shape still differs from what AddonProvider hands back directly, and
    those two are reshaped here rather than in host.py, which speaks a
    generic JSON-RPC-ish protocol with no notion of "entries" or "candidates".
    """

    def __init__(self, pool, provider_id):
        self._pool = pool
        self.provider_id = provider_id

    def genres(self, kind):
        return self._pool.call(contract.OP_GENRES, {"kind": kind}, TIMEOUT_GENRES_S)

    def browse(self, kind, page=1, page_size=20, sort=None, filters=None):
        raw = self._pool.call(
            contract.OP_BROWSE,
            {"kind": kind, "page": page, "page_size": page_size, "sort": sort, "filters": filters or {}},
            TIMEOUT_BROWSE_S)
        entries = raw.get("items") if isinstance(raw, dict) else None
        entries = entries if isinstance(entries, list) else []
        has_more = raw.get("has_more") if isinstance(raw, dict) else None
        if not isinstance(has_more, bool):
            # No declared value -- fall back to the same heuristic
            # AddonProvider itself uses: a full page probably isn't the last
            # one. Wrong occasionally (short last page), never in the
            # direction that drops real results.
            has_more = len(entries) >= max(1, int(page_size or 1))
        return {"entries": entries, "has_more": has_more}

    def search(self, kind, query, limit=20):
        raw = self._pool.call(contract.OP_SEARCH, {"kind": kind, "query": query, "limit": limit},
                               TIMEOUT_SEARCH_S)
        entries = raw.get("items") if isinstance(raw, dict) else None
        return {"entries": entries if isinstance(entries, list) else []}

    def details(self, local_id, kind):
        return self._pool.call(contract.OP_DETAILS, {"id": local_id, "kind": kind}, TIMEOUT_DETAILS_S)

    def episodes(self, local_id, season):
        return self._pool.call(contract.OP_EPISODES, {"id": local_id, "season": season}, TIMEOUT_EPISODES_S)

    def streams(self, identity, season=None, episode=None):
        raw = self._pool.call(contract.OP_STREAMS,
                               {"identity": identity, "season": season, "episode": episode},
                               TIMEOUT_STREAMS_S)
        candidates = raw.get("candidates") if isinstance(raw, dict) else None
        candidates = candidates if isinstance(candidates, list) else []
        rejected = raw.get("rejected") if isinstance(raw, dict) else None
        if isinstance(rejected, dict):
            rejected_out = dict(rejected)
        elif isinstance(rejected, int) and rejected > 0:
            # host.py's package-runtime path only keeps a total count, not
            # addon.py's per-reason breakdown -- one honest bucket beats
            # inventing reasons that were never actually reported.
            rejected_out = {"rejected": rejected}
        else:
            rejected_out = {}
        return {"candidates": candidates, "rejected": rejected_out}

    def test(self):
        return self._pool.call(contract.OP_TEST, {}, TIMEOUT_TEST_S)


def _get_instance(provider_id):
    rec = registry.get(provider_id)
    if rec is None:
        raise contract.ProviderError(
            contract.E_CONFIG,
            contract.redact("provider %r is not installed" % (provider_id,), secret_values()),
            provider=provider_id)
    rev = registry.config_revision(provider_id)
    with _lock:
        cached = _instances.get(provider_id)
        if cached is not None and cached[0] == rev:
            return cached[1]
        manifest, config = rec.manifest, rec.config
        if manifest.get("runtime") == "addon":
            instance = addon.AddonProvider(manifest, config)
        else:
            package_dir = store.provider_dir(provider_id)
            pool = _pool_manager.get_or_create(provider_id, package_dir, config, rev)
            # get_or_create() only builds a NEW pool if none exists yet; if
            # one already did (under a stale rev), it does not update it --
            # reconfigure() is what makes the config edit actually reach
            # future calls, via Pool.retire_on_idle's "swap on next checkout,
            # never mid-request" rule, without killing a worker that is
            # mid-stream-lookup right now.
            _pool_manager.reconfigure(provider_id, config, rev)
            instance = _PackageAdapter(pool, provider_id)
        _instances[provider_id] = (rev, instance)
        return instance


def invalidate(provider_id=None):
    """Drop cached instances after a config or activation change. A package
    provider's pool is also closed (its subprocesses killed) so a bad
    credential or a re-pointed URL can never keep being served by workers
    spawned under the old config; an add-on instance is just plain dropped,
    since it holds no resources of its own to close."""
    with _lock:
        if provider_id is None:
            _instances.clear()
        else:
            _instances.pop(provider_id, None)
    if provider_id is None:
        _pool_manager.close_all()
    else:
        _pool_manager.remove(provider_id)


# ---- role resolution ------------------------------------------------------------
def _resolved(role):
    """The provider id that would actually serve `role` right now, including
    registry's metadata-falls-back-to-catalogue rule. Calls
    registry.setup_state() rather than re-deriving the fallback here, because
    that logic already exists in exactly one place (the settings page depends
    on it too) and a second copy would drift the moment either one changed."""
    return (registry.setup_state().get("roles") or {}).get(role)


def available(role):
    """True only if `role` resolves to a provider that is both active and
    correctly configured. An activated-but-needs_config provider would still
    make every call below fail with E_CONFIG/E_AUTH, so "available" answers
    the more useful question -- can a call actually be expected to work."""
    pid = _resolved(role)
    return bool(pid) and registry.status(pid)[0] == "ready"


def active_id(role):
    return _resolved(role)


def cache_tag(role):
    """"<provider_id>@<config_rev>", or "none" -- callers key their own
    caches on this so a provider swap or a config edit can never serve a
    cached page gathered under the old one."""
    pid = _resolved(role)
    if not pid:
        return "none"
    rev = registry.config_revision(pid)
    return "%s@%s" % (pid, rev) if rev else "none"


def supports_filters(role=contract.ROLE_CATALOGUE):
    pid = _resolved(role)
    if not pid:
        return set()
    rec = registry.get(pid)
    return set(rec.manifest.get("filters") or ()) if rec else set()


def setup_required():
    return registry.setup_state()


# ---- id qualification guard -----------------------------------------------------
def _local_id_for(qualified_or_bare, resolved_pid, op):
    """The local id to send to `resolved_pid`, given an id another provider
    may have minted.

    Three roles mean the catalogue usually mints the ids and the metadata and
    stream providers are asked about them. That is the ordinary configuration,
    not an error, so a prefix mismatch cannot simply be refused -- doing so
    broke every mixed setup, which is the arrangement this whole design exists
    to support.

    It cannot be waved through either. An id is only meaningful outside the
    provider that issued it if it belongs to a namespace everyone agrees on.
    "tt0012349" means the same film to every service that speaks IMDb ids. A
    bare "603" does not: it is one catalogue's Matrix, another's unrelated row,
    or nothing, and resolving it against the wrong service returns the WRONG
    FILM rather than an error -- much worse than refusing.

    So: a shared identifier passes to any provider, and a provider-private one
    passes only to the provider that issued it. Anything else is refused by
    name, and says which namespace it would have needed.
    """
    minted_by, local_id = contract.unqualify(qualified_or_bare)
    if minted_by is None or minted_by == resolved_pid:
        return local_id
    if contract.RE_IMDB.match(local_id or ""):
        return local_id
    raise contract.ProviderError(
        contract.E_NOTFOUND,
        "%r was issued by provider %r and is not an identifier %r can resolve; "
        "mixing these two providers needs a shared id namespace (an IMDb tt id)"
        % (local_id, minted_by, resolved_pid),
        provider=resolved_pid, op=op)


# ---- dispatch --------------------------------------------------------------------
def _resolve_or_raise(role, op):
    pid = _resolved(role)
    if not pid:
        raise contract.ProviderError(
            contract.E_CONFIG,
            contract.redact("no provider is configured for the %r role" % (role,), secret_values()),
            provider=None, op=op)
    return pid


def _invoke(provider_id, op, fn):
    """Run `fn(instance)` for provider_id's cached instance, under its
    throttle, and turn every failure mode into a ProviderError that carries
    provenance and a redacted message -- a provider bug (an unexpected
    exception type, a crash, a malformed reply) must surface as a named
    error here, never as a raw TypeError inside server.py's browse loop."""
    try:
        instance = _get_instance(provider_id)
        _throttle(provider_id)
        return fn(instance)
    except contract.ProviderError as ex:
        # Rebuilt, not mutated: ProviderError's __str__/args are fixed at
        # construction, so patching .message in place would leave an
        # unredacted copy reachable through str(ex) or logging that formats
        # the exception directly instead of reading .message.
        raise contract.ProviderError(
            ex.code, contract.redact(ex.message, secret_values()),
            retryable=ex.retryable, provider=ex.provider or provider_id, op=ex.op or op) from ex
    except Exception as ex:
        raise contract.ProviderError(
            contract.E_INTERNAL,
            contract.redact("unexpected error in %s: %s" % (op, ex), secret_values()),
            provider=provider_id, op=op) from ex


# ---- public API -------------------------------------------------------------------
def genres(kind):
    pid = _resolve_or_raise(contract.ROLE_CATALOGUE, contract.OP_GENRES)
    return _invoke(pid, contract.OP_GENRES, lambda inst: inst.genres(kind))


def browse(kind, page=1, page_size=20, sort=None, filters=None):
    pid = _resolve_or_raise(contract.ROLE_CATALOGUE, contract.OP_BROWSE)
    return _invoke(pid, contract.OP_BROWSE, lambda inst: inst.browse(kind, page, page_size, sort, filters))


def search(kind, query, limit=20):
    pid = _resolve_or_raise(contract.ROLE_CATALOGUE, contract.OP_SEARCH)
    return _invoke(pid, contract.OP_SEARCH, lambda inst: inst.search(kind, query, limit))


def details(qualified_id, kind):
    pid = _resolve_or_raise(contract.ROLE_METADATA, contract.OP_DETAILS)
    local_id = _local_id_for(qualified_id, pid, contract.OP_DETAILS)
    return _invoke(pid, contract.OP_DETAILS, lambda inst: inst.details(local_id, kind))


def episodes(qualified_id, season):
    pid = _resolve_or_raise(contract.ROLE_METADATA, contract.OP_EPISODES)
    local_id = _local_id_for(qualified_id, pid, contract.OP_EPISODES)
    return _invoke(pid, contract.OP_EPISODES, lambda inst: inst.episodes(local_id, season))


def streams(identity, season=None, episode=None):
    pid = _resolve_or_raise(contract.ROLE_STREAMS, contract.OP_STREAMS)
    # NO provider-prefix check here, deliberately.
    #
    # details() and episodes() insist that a qualified id belongs to the
    # provider about to serve it: those ids are that provider's own, and
    # resolving one against a different catalogue is how a saved selection
    # quietly becomes a different film.
    #
    # Streams are the opposite case. The whole point of separating the roles is
    # that the catalogue and the stream index are usually different services --
    # Cinemeta mints the id, some other add-on finds the file. Applying the
    # same guard here rejected every lookup the moment the two roles were
    # filled by different providers, which is the ordinary configuration and
    # the one this design exists to support. Worse, get_stream() turned the
    # refusal into a bare "no usable stream", so an entire catalogue looked
    # unplayable with nothing anywhere saying why.
    #
    # Matching identity to a source is the stream provider's job, and it has
    # what it needs: external_ids for the shared identifiers, local_id and
    # title/year for the rest. A provider that cannot work with any of them
    # says so by name (E_UNSUPPORTED), which is a real answer rather than a
    # guess made up here.
    return _invoke(pid, contract.OP_STREAMS, lambda inst: inst.streams(identity, season, episode))


def test(provider_id):
    """Round-trip provider_id's current config against the real service, for
    an admin "test connection" action. Not role-routed (a provider being
    tested need not be active yet) but still goes through the same instance
    cache and throttle as everything else, so server.py never has to reach
    into addon.py/runner.py itself just for this one button."""
    if registry.get(provider_id) is None:
        raise contract.ProviderError(
            contract.E_CONFIG,
            contract.redact("provider %r is not installed" % (provider_id,), secret_values()),
            provider=provider_id, op=contract.OP_TEST)
    return _invoke(provider_id, contract.OP_TEST, lambda inst: inst.test())


# ---- registry/store passthroughs for server.py ---------------------------------
# server.py must never import addon.py, registry.py, runner.py or store.py
# directly (see this module's docstring) -- the provider admin API reaches
# all four exclusively through the thin wrappers below.
def list_installed():
    return registry.list_installed()


def get_provider(provider_id):
    return registry.get(provider_id)


def provider_status(provider_id):
    return registry.status(provider_id)


def public_config(provider_id):
    return registry.public_config(provider_id)


def active_all():
    return registry.active_all()


def set_active(role, provider_id):
    registry.set_active(role, provider_id)


def install_provider(manifest, files_dir=None, source=None):
    return registry.install(manifest, files_dir=files_dir, source=source)


def remove_provider(provider_id):
    return registry.remove(provider_id)


def set_enabled(provider_id, enabled):
    return registry.set_enabled(provider_id, enabled)


def set_config(provider_id, values, clear_keys=()):
    return registry.set_config(provider_id, values, clear_keys=clear_keys)


def update_provider(provider_id, manifest, files_dir=None, source=None):
    """Replace an installed provider's manifest (and, for a package
    provider, its files) in place. registry.install() cannot be reused for
    this: it unconditionally resets config/enabled/last_test, which is right
    for a brand-new install and wrong for re-fetching an add-on's manifest
    or swapping a package's code without disturbing its saved credentials.

    Returns None if provider_id is not installed. A validation failure (a
    bad manifest, or one whose id no longer matches) raises before anything
    on disk is touched, and a failure while swapping the files in is rolled
    back by registry.swap_in_files, so a failed update leaves the previous
    install exactly as it was -- still installed, still runnable.

    registry.py has no setter for "replace the manifest in place", and
    adding one there is out of scope for this change -- this reaches into
    registry's own read-modify-write helpers under its own lock, the same
    way its set_config() does internally, rather than duplicating that
    logic here.
    """
    manifest = contract.validate_manifest(manifest)
    if manifest["id"] != provider_id:
        raise contract.ContractError(
            "updated manifest id %r does not match installed provider %r"
            % (manifest["id"], provider_id))
    with registry._lock:
        data = registry._load()
        raw = (data.get("installed") or {}).get(provider_id)
        if raw is None:
            return None
        if files_dir is not None:
            # Staged and swapped, never deleted-then-copied: an update whose
            # copy fails must leave the previous version installed and
            # serving, not wipe it on the way to not replacing it. Raises on
            # failure, having put the old tree back -- which is why it runs
            # BEFORE the manifest is written, so a failed file swap cannot
            # leave providers.json describing a version that is not on disk.
            registry.swap_in_files(provider_id, files_dir, manifest)
        raw["manifest"] = manifest
        if source is not None:
            raw["source"] = source
        registry._save(data)
    return registry.get(provider_id)


def record_test(provider_id, result):
    """Persist a config.test outcome onto provider_id's Installed record, so
    registry.status() reports "test_failed" immediately and /api/providers
    can show the last result without a second, separate store.

    Same reasoning as update_provider() above: nothing in registry.py ever
    writes a fresh last_test, only clears it (in set_config()).
    """
    with registry._lock:
        data = registry._load()
        raw = (data.get("installed") or {}).get(provider_id)
        if raw is None:
            return False
        raw["last_test"] = result
        registry._save(data)
    return True


# ---- addon passthroughs ---------------------------------------------------------
def fetch_addon_manifest(url):
    return addon.fetch_manifest(url)


def addon_manifest_to_provider(raw, addon_url):
    return addon.to_provider_manifest(raw, addon_url)


# ---- admin auth passthroughs -----------------------------------------------------
def admin_claimed():
    return bool(store.admin_state().get("password"))


def bootstrap_token():
    return store.bootstrap_token()


def claim_setup(token, password):
    return store.claim(token, password)


def check_admin_password(password):
    return store.check_admin_password(password)


def new_session():
    return store.new_session()


def check_session(tok):
    return store.check_session(tok)


def drop_session(tok):
    return store.drop_session(tok)


def csrf_for(session):
    return store.csrf_for(session)


# ---- smoke test -------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import tempfile

    from tests.addon_stub import StubAddon

    ok = True

    def check(name, cond):
        global ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CINEMATICA_STATE"] = tmp

        with StubAddon(resources=("catalog", "meta", "stream")) as stub:
            raw_manifest = addon.fetch_manifest(stub.manifest_url)
            manifest = addon.to_provider_manifest(raw_manifest, stub.manifest_url)
            registry.install(manifest, source=stub.manifest_url)
            pid = manifest["id"]
            for role in contract.ROLES:
                registry.set_active(role, pid)

            check("active_id resolves for all three roles",
                  all(active_id(r) == pid for r in contract.ROLES))
            check("available() is true once configured", available(contract.ROLE_CATALOGUE))
            check("cache_tag is '<pid>@<rev>'", cache_tag(contract.ROLE_CATALOGUE).startswith(pid + "@"))
            check("supports_filters reports the stub's genre filter",
                  supports_filters() == {"genre_ids"})
            check("setup_required reports configured", setup_required()["configured"] is True)

            g = genres(contract.KIND_MOVIE)
            check("genres returns the stub's genres", {x["name"] for x in g} == {"Action", "Drama", "Comedy"})

            page1 = browse(contract.KIND_MOVIE, page=1, page_size=20)
            page2 = browse(contract.KIND_MOVIE, page=2, page_size=20)
            ids1 = {e["local_id"] for e in page1["entries"]}
            ids2 = {e["local_id"] for e in page2["entries"]}
            check("browse page 1 returns a full page", len(page1["entries"]) == 20)
            check("browse page 2 returns different entries", bool(ids2) and ids1.isdisjoint(ids2))

            found = search(contract.KIND_MOVIE, "Stub Movie 1", 10)
            check("search finds the stub title", any(e["title"] == "Stub Movie 1" for e in found["entries"]))

            series_local_id = "tt9000001"
            qualified = contract.qualify(pid, series_local_id)
            d = details(qualified, contract.KIND_SERIES)
            check("details returns the right series", d["title"] == "Stub Series 1")

            eps = episodes(qualified, 1)
            check("episodes returns only season 1",
                  bool(eps["episodes"]) and all(e["season"] == 1 for e in eps["episodes"]))

            identity = {"id": qualified, "local_id": series_local_id, "kind": contract.KIND_SERIES,
                        "title": d["title"], "year": d.get("year"), "runtime": d.get("runtime"),
                        "external_ids": d.get("external_ids", {})}
            st = streams(identity, season=1, episode=1)
            check("streams returns a playable candidate", len(st["candidates"]) >= 1)
            check("streams counts the unsupported row as rejected", sum(st["rejected"].values()) >= 1)

            registry.set_active(contract.ROLE_STREAMS, None)
            invalidate()
            try:
                streams(identity, season=1, episode=1)
                check("unconfigured role raises E_CONFIG", False)
            except contract.ProviderError as ex:
                check("unconfigured role raises E_CONFIG", ex.code == contract.E_CONFIG)
            registry.set_active(contract.ROLE_STREAMS, pid)
            invalidate()

            foreign = contract.qualify("some-other-provider", series_local_id)
            try:
                details(foreign, contract.KIND_SERIES)
                check("foreign qualified id raises E_NOTFOUND", False)
            except contract.ProviderError as ex:
                check("foreign qualified id raises E_NOTFOUND", ex.code == contract.E_NOTFOUND)

            tag_before = cache_tag(contract.ROLE_CATALOGUE)
            registry.set_config(pid, {"catalog": "stub-series"})
            tag_after = cache_tag(contract.ROLE_CATALOGUE)
            check("cache_tag changes when config changes", tag_before != tag_after)

    print("ALL PASS" if ok else "SOME FAILED")


def secret_values():
    """Every credential this install holds, for contract.redact's literal pass.

    The pattern pass catches credentials shaped like credentials -- a Bearer
    header, userinfo in a URL, a well-known query parameter. It cannot catch an
    API key a provider pasted into an error message in a shape nobody
    anticipated. Matching the literal values this install actually holds does,
    and server.py cannot reach store.py directly, so it comes through here.

    Two sources, because an install has two places a credential can be. The
    declared secret config fields are the obvious one. The other is the add-on
    URL itself: a Stremio add-on is configured by its URL, so a debrid API key
    or an account token routinely sits in a path segment of the manifest URL
    and is never a "secret field" at all. Those pieces are credentials by any
    reasonable reading and belong in the same pass -- see contract.url_secrets.
    """
    out = []
    try:
        out.extend(store.all_secret_values())
    except Exception:
        pass
    try:
        for rec in registry.list_installed():
            out.extend(contract.url_secrets(rec.manifest.get("addon_url")))
            if isinstance(rec.source, str):
                out.extend(contract.url_secrets(rec.source))
    except Exception:
        pass
    return tuple(dict.fromkeys(out))
