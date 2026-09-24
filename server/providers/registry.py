"""The installed set: what is installed, what it is configured with, and
which provider fills each role.

Imports store.py (raw disk) and contract.py (the wire shapes). Deliberately
does NOT import server.py -- this module has to be importable, and testable,
without pulling in the HTTP server, the scoring engine or a live provider key.
"""

import dataclasses
import os
import shutil
import threading
import time

from . import contract, store

# Guards a whole install/remove/set_enabled/set_config/set_active call, not
# just one disk write. Each of those is read-modify-write across providers.json
# (and sometimes secrets.json); two settings-page saves for the same provider
# racing inside that window would otherwise let the second silently discard
# the first's edit instead of merging with it.
_lock = threading.RLock()


@dataclasses.dataclass
class Installed:
    id: str
    manifest: dict
    enabled: bool
    config: dict          # merged: manifest defaults + saved non-secrets + secrets
    installed_at: float
    source: str           # an addon URL, or the literal "package"
    last_test: dict        # {"ok": bool, "at": ts, "message": str}, or None


# ---- internal helpers --------------------------------------------------------
def _load():
    return store.read_json("providers.json", {"installed": {}, "active": {}})


def _save(data):
    store.write_json("providers.json", data)


def _secret_keys(manifest):
    return {f["key"] for f in manifest.get("config") or () if f["type"] in contract.SECRET_TYPES}


def _defaults(manifest):
    return {f["key"]: f.get("default") for f in manifest.get("config") or ()}


def _full_config(provider_id, raw):
    # Order matters: manifest defaults, then whatever was saved, then secrets
    # -- a field is either secret or not, so the last two never collide, but
    # a default must never survive over an explicitly saved (even empty)
    # non-secret value.
    merged = dict(_defaults(raw["manifest"]))
    merged.update(raw.get("config") or {})
    merged.update(store.get_secrets(provider_id))
    return merged


def _record(provider_id, raw):
    return Installed(
        id=provider_id,
        manifest=raw["manifest"],
        enabled=bool(raw.get("enabled")),
        config=_full_config(provider_id, raw),
        installed_at=raw.get("installed_at"),
        source=raw.get("source"),
        last_test=raw.get("last_test"),
    )


def _make_readable(root):
    """Open a staged package to the provider account (runner.provider_user),
    which is not its owner: an upload is unpacked into a 0700 temp directory
    and copytree carries that mode across. A package is code, not a secret;
    its credentials live in secrets.json and reach it over stdin."""
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue  # chmod would follow it out of the package
            os.chmod(path, 0o755 if os.stat(path).st_mode & 0o100 else 0o644)


def swap_in_files(provider_id, files_dir, manifest):
    """Put `files_dir`'s tree in place as provider_id's installed package,
    without ever being able to leave the provider with no files at all.

    Copy first, into a staging directory beside the real one; check the copy
    actually landed; only then swap. The swap itself is two renames within
    providers/, so it is atomic and cannot half-happen, and the displaced tree
    is kept until the new one is in place -- if the second rename somehow
    fails, the old tree goes straight back.

    This exists because the obvious spelling (rmtree the old, copytree the
    new) destroys a working install the moment the copy fails: a full disk, a
    truncated upload, a permission problem part-way through, and the provider
    is left with nothing to run and no way back short of re-downloading the
    package. An update that fails must leave the previous version serving.

    Raises on failure, having restored the previous tree.
    """
    dest = store.provider_dir(provider_id)
    staged = dest + ".incoming"
    displaced = dest + ".previous"

    store.ensure_dirs()
    # Leftovers from an earlier run that died between renames. Neither is ever
    # read by anything but this function, so clearing them is always safe.
    shutil.rmtree(staged, ignore_errors=True)
    shutil.rmtree(displaced, ignore_errors=True)

    shutil.copytree(files_dir, staged)
    try:
        _make_readable(staged)
        # Validate the COPY, not the source: the point is to catch a copy that
        # went wrong, so checking the directory we were handed proves nothing.
        entry = os.path.realpath(os.path.join(staged, manifest["entry"]))
        if not (entry == os.path.realpath(staged)
                or entry.startswith(os.path.realpath(staged) + os.sep)):
            raise ValueError("staged package entry escapes the package directory")
        if not os.path.isfile(entry):
            raise ValueError("staged package is missing its entry file %r" % manifest["entry"])
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise

    had_previous = os.path.isdir(dest)
    if had_previous:
        os.replace(dest, displaced)
    try:
        os.replace(staged, dest)
    except Exception:
        if had_previous:
            os.replace(displaced, dest)
        shutil.rmtree(staged, ignore_errors=True)
        raise
    shutil.rmtree(displaced, ignore_errors=True)


# ---- installed set ------------------------------------------------------------
def list_installed():
    data = _load()
    installed = data.get("installed") or {}
    return [_record(pid, installed[pid]) for pid in sorted(installed)]


def get(provider_id):
    data = _load()
    raw = (data.get("installed") or {}).get(provider_id)
    return _record(provider_id, raw) if raw else None


def install(manifest, files_dir=None, source=None):
    """Register a provider. `manifest` is re-validated here (not just
    trusted from the caller) because a manifest that cannot be described must
    never be run, and the only way to guarantee that for every call path is
    to check it at the point of installation, not upstream of it."""
    manifest = contract.validate_manifest(manifest)
    pid = manifest["id"]

    if files_dir is not None:
        # Replace wholesale, not merge: a stale file left from a previous
        # version of this same package is exactly what "reinstall to fix it"
        # is supposed to rule out. Staged and swapped (see swap_in_files) so a
        # reinstall that fails leaves the working install untouched instead of
        # deleting it on the way to not replacing it.
        swap_in_files(pid, files_dir, manifest)

    if source is None:
        source = manifest["addon_url"] if manifest["runtime"] == "addon" else "package"

    with _lock:
        data = _load()
        data.setdefault("installed", {})[pid] = {
            "manifest": manifest,
            "enabled": True,
            "config": {},
            "installed_at": time.time(),
            "source": source,
            "last_test": None,
        }
        _save(data)
    return get(pid)


def remove(provider_id):
    with _lock:
        data = _load()
        installed = data.get("installed") or {}
        if provider_id not in installed:
            return False
        del installed[provider_id]
        for role, pid in (data.get("active") or {}).items():
            if pid == provider_id:
                data["active"][role] = None
        _save(data)
    store.drop_secrets(provider_id)
    shutil.rmtree(store.provider_dir(provider_id), ignore_errors=True)
    return True


def set_enabled(provider_id, enabled):
    with _lock:
        data = _load()
        installed = data.get("installed") or {}
        if provider_id not in installed:
            return False
        installed[provider_id]["enabled"] = bool(enabled)
        _save(data)
    return True


# ---- config -------------------------------------------------------------------
def set_config(provider_id, values, clear_keys=()):
    """Merge `values` into a provider's saved config. A secret field absent
    from `values` keeps its stored value -- leaving a password box blank on
    save must not erase it -- while a key named in `clear_keys` is removed
    outright, which is the only way to actually clear a secret."""
    with _lock:
        data = _load()
        raw = (data.get("installed") or {}).get(provider_id)
        if raw is None:
            return False
        secret_keys = _secret_keys(raw["manifest"])
        non_secret = dict(raw.get("config") or {})
        secret_vals = store.get_secrets(provider_id)

        for key, val in (values or {}).items():
            if key in secret_keys:
                secret_vals[key] = val
            else:
                non_secret[key] = val
        for key in clear_keys or ():
            if key in secret_keys:
                secret_vals.pop(key, None)
            else:
                non_secret.pop(key, None)

        raw["config"] = non_secret
        # A test result belongs to the config it was taken under. Carrying it
        # across an edit would let a just-fixed field keep showing
        # "test failed" (or a just-broken one keep showing "ready") until
        # someone happens to press test again.
        raw["last_test"] = None
        store.set_secrets(provider_id, secret_vals)
        _save(data)
    return True


def config_for(provider_id):
    """Full config, secrets included -- what actually gets handed to the
    provider process."""
    data = _load()
    raw = (data.get("installed") or {}).get(provider_id)
    return _full_config(provider_id, raw) if raw else None


def public_config(provider_id):
    """Same config, for the API: every secret field replaced by
    contract.mask(), never the value itself."""
    data = _load()
    raw = (data.get("installed") or {}).get(provider_id)
    if raw is None:
        return None
    secret_keys = _secret_keys(raw["manifest"])
    full = _full_config(provider_id, raw)
    return {k: (contract.mask(v) if k in secret_keys else v) for k, v in full.items()}


# ---- roles ----------------------------------------------------------------
def set_active(role, provider_id):
    if role not in contract.ROLES:
        raise ValueError("unknown role %r" % (role,))
    with _lock:
        if provider_id is not None:
            rec = get(provider_id)
            if rec is None:
                raise ValueError("provider %r is not installed" % (provider_id,))
            if role not in rec.manifest.get("capabilities", ()):
                raise ValueError("provider %r does not declare role %r" % (provider_id, role))
            if not rec.enabled:
                raise ValueError("provider %r is disabled" % (provider_id,))
        data = _load()
        data.setdefault("active", {})[role] = provider_id
        _save(data)


def active(role):
    data = _load()
    pid = (data.get("active") or {}).get(role)
    if not pid:
        return None
    rec = get(pid)
    if rec is None or not rec.enabled or role not in rec.manifest.get("capabilities", ()):
        # Stale pointer -- e.g. the provider was disabled, or reinstalled
        # without this role -- reported as unset rather than cleared here, so
        # re-enabling the same provider restores it with no extra step.
        return None
    return pid


def active_all():
    return {role: active(role) for role in contract.ROLES}


def status(provider_id):
    """One of "ready" / "needs_config" / "disabled" / "test_failed", plus a
    human message. These exact strings drive the settings-page badges."""
    rec = get(provider_id)
    if rec is None:
        return "disabled", "Not installed"
    if not rec.enabled:
        return "disabled", "Disabled"
    for field in rec.manifest.get("config") or ():
        if not field.get("required"):
            continue
        val = rec.config.get(field["key"])
        if val is None or val == "":
            return "needs_config", "%s is required" % (field.get("label") or field["key"])
    if rec.last_test and rec.last_test.get("ok") is False:
        return "test_failed", rec.last_test.get("message") or "Last test failed"
    return "ready", "Ready"


def config_revision(provider_id):
    rec = get(provider_id)
    if rec is None:
        return None
    # The add-on URL is passed through because it is where a Stremio add-on
    # actually keeps its configuration (and often its account credential) --
    # see contract.config_revision. "" for a package provider, which has no
    # such URL and whose config is entirely in `rec.config`.
    return contract.config_revision(provider_id, rec.manifest["version"], rec.config,
                                    rec.manifest.get("addon_url") or "")


def setup_state():
    """{"configured": bool, "roles": {...}, "message": ...}. Streams and
    catalogue must each resolve to an enabled, ready provider; metadata may
    fall back to the catalogue provider when it also declares that role --
    Stremio add-ons routinely ship both, and requiring a second install just
    to re-supply the same data the catalogue provider already has would be
    exactly the kind of pointless friction contract.py's role split exists
    to avoid."""
    cat = active(contract.ROLE_CATALOGUE)
    streams = active(contract.ROLE_STREAMS)
    meta = active(contract.ROLE_METADATA)

    fallback = False
    if meta is None and cat is not None:
        cat_rec = get(cat)
        if cat_rec and contract.ROLE_METADATA in cat_rec.manifest.get("capabilities", ()):
            meta = cat
            fallback = True

    roles = {contract.ROLE_CATALOGUE: cat, contract.ROLE_METADATA: meta, contract.ROLE_STREAMS: streams,
             contract.ROLE_CHANNELS: active(contract.ROLE_CHANNELS)}

    missing = [r for r in (contract.ROLE_CATALOGUE, contract.ROLE_STREAMS) if not roles[r]]
    # Only the three core roles gate "configured" -- channels is optional, so a
    # channels provider that still needs configuration must never make an
    # otherwise-working install report itself unconfigured.
    not_ready = [r for r in contract.CORE_ROLES if roles.get(r) and status(roles[r])[0] != "ready"]

    if missing:
        message = "%s not set" % " and ".join(missing)
    elif roles[contract.ROLE_METADATA] is None:
        message = "metadata not set (and the catalogue provider does not supply it)"
    elif not_ready:
        message = "%s provider is not ready" % not_ready[0]
    elif fallback:
        message = "configured (metadata supplied by the catalogue provider)"
    else:
        message = "configured"

    configured = not missing and roles[contract.ROLE_METADATA] is not None and not not_ready
    return {"configured": configured, "roles": roles, "message": message}


# ---- smoke test ---------------------------------------------------------------
if __name__ == "__main__":
    import os
    import tempfile

    ok = True

    def check(name, cond):
        global ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CINEMATICA_STATE"] = tmp

        manifest = {
            "id": "fake-catalogue",
            "version": "1.0.0",
            "contract": contract.CONTRACT_VERSION,
            "capabilities": [contract.ROLE_CATALOGUE, contract.ROLE_METADATA],
            "config": [
                {"key": "api_key", "type": contract.F_SECRET, "required": True},
                {"key": "region", "type": contract.F_TEXT, "required": False, "default": "US"},
            ],
        }

        rec = install(manifest, source="package")
        check("install registers provider", rec is not None and rec.id == "fake-catalogue")
        check("fresh install needs_config", status("fake-catalogue")[0] == "needs_config")

        set_config("fake-catalogue", {"api_key": "sk-super-secret-value"})
        check("secret accepted", config_for("fake-catalogue").get("api_key") == "sk-super-secret-value")

        pub = public_config("fake-catalogue")
        check("public_config masks secret", pub["api_key"] == contract.mask("sk-super-secret-value"))
        check("public_config keeps non-secret", pub["region"] == "US")
        check("status ready after secret set", status("fake-catalogue")[0] == "ready")

        # Re-save without the secret key: must NOT wipe the stored secret.
        set_config("fake-catalogue", {"region": "GB"})
        check("resave preserves secret", config_for("fake-catalogue")["api_key"] == "sk-super-secret-value")
        check("resave applies non-secret edit", config_for("fake-catalogue")["region"] == "GB")

        # clear_keys actually removes it.
        set_config("fake-catalogue", {}, clear_keys=("api_key",))
        check("clear_keys removes secret", config_for("fake-catalogue").get("api_key") is None)
        check("status needs_config once secret cleared", status("fake-catalogue")[0] == "needs_config")

        set_config("fake-catalogue", {"api_key": "sk-again"})
        set_active(contract.ROLE_CATALOGUE, "fake-catalogue")
        check("set_active/active round-trip", active(contract.ROLE_CATALOGUE) == "fake-catalogue")

        state = setup_state()
        check("setup_state metadata falls back to catalogue provider",
              state["roles"][contract.ROLE_METADATA] == "fake-catalogue")
        check("setup_state not configured (no streams provider)", state["configured"] is False)

        # Atomic write: providers.json must be valid JSON, never a .tmp leftover.
        path = os.path.join(tmp, "providers.json")
        tmp_path = path + ".tmp"
        check("atomic write leaves no .tmp file", not os.path.exists(tmp_path))
        with open(path) as f:
            import json as _json
            _json.load(f)
        check("providers.json parses", True)

        # Admin claim: single-use.
        token = store.bootstrap_token()
        check("bootstrap_token issued", bool(token))
        check("claim succeeds with correct token", store.claim(token, "hunter2") is True)
        check("claim fails once already claimed", store.claim(token, "hunter2") is False)
        check("check_admin_password verifies", store.check_admin_password("hunter2") is True)
        check("check_admin_password rejects wrong password", store.check_admin_password("nope") is False)

    print("ALL PASS" if ok else "SOME FAILED")
