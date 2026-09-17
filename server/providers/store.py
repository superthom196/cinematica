"""On-disk provider state, kept OUTSIDE the application directory.

Everything here lives under CINEMATICA_STATE (default /var/lib/cinematica),
never under server/. A deploy replaces server/ wholesale -- git checkout,
tarball, whatever -- and that must not be able to take a family's installed
providers, their credentials or the admin password down with it.

Layout, all direct children of state_dir():
    providers/<id>/   installed package files, one tree per provider
    providers.json    installed set, per-role activation, non-secret config
    secrets.json      provider credentials -- mode 0600
    admin.json        admin password hash + bootstrap/CSRF material -- 0600

registry.py owns the meaning of providers.json; this module only owns getting
bytes on and off disk without ever losing or half-writing them, and the
credential/admin/session primitives that are too security-sensitive to
duplicate per caller.
"""

import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

# RLock, not Lock -- see server.py's cache lock for the failure mode this
# avoids. claim() and set_config()'s callers read admin_state()/providers.json
# and then write it back inside the SAME lock acquisition; a plain Lock would
# deadlock the moment any helper here called another helper here.
_lock = threading.RLock()

_SESSION_TTL = 30 * 24 * 3600
_SESSION_CAP = 200
_sessions = {}   # token -> expires_at (epoch seconds); in-memory only

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


# ---- paths -------------------------------------------------------------------
def state_dir():
    # A function, not a module constant: frozen at import, CINEMATICA_STATE
    # set by a test after this module is already imported would be ignored,
    # and every test in this package relies on overriding it per-run.
    return os.environ.get("CINEMATICA_STATE", "/var/lib/cinematica")


def providers_dir():
    return os.path.join(state_dir(), "providers")


def provider_dir(provider_id):
    return os.path.join(providers_dir(), provider_id)


def ensure_dirs():
    for path in (state_dir(), providers_dir()):
        os.makedirs(path, exist_ok=True)
        try:
            # os.makedirs' mode= is masked by umask and ignored entirely when
            # the directory already exists, so the only way to be sure this
            # tree (secrets.json's parent) is not world-readable is to chmod
            # it explicitly, every time.
            os.chmod(path, 0o700)
        except OSError:
            pass


# ---- atomic JSON -------------------------------------------------------------
def read_json(name, default):
    path = os.path.join(state_dir(), name)
    with _lock:
        try:
            with open(path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # Corrupt beats crashing the server on startup -- every caller
            # already treats `default` as "nothing here yet", so a mangled
            # file just degrades to that instead of taking the process down.
            pass
    # A fresh copy: `default` is often a literal built once by the caller
    # (or, worse, a module-level constant); handing back the same object on
    # every miss would let one caller's in-place edit poison the next read.
    return copy.deepcopy(default)


def write_json(name, obj, mode=0o600):
    """Write `obj` as JSON to state_dir()/name so a crash or power loss mid-
    write can never leave a half-written file in its place.

    write -> fsync the bytes -> rename over the old file -> fsync the
    directory. The rename is what makes the switch atomic from a reader's
    point of view; the trailing fsync is what makes it durable from the
    disk's -- a journaling filesystem only guarantees a rename survives power
    loss once the directory entry itself has been flushed, not merely the
    file it now points to. Skip it and providers.json can come back after a
    crash pointing at neither the old content nor the new.
    """
    ensure_dirs()
    path = os.path.join(state_dir(), name)
    tmp = path + ".tmp"
    with _lock:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.chmod(tmp, mode)  # os.open()'s mode is masked by umask; force the real one
            with os.fdopen(fd, "w") as f:
                json.dump(obj, f, sort_keys=True, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---- secrets -------------------------------------------------------------
# {provider_id: {field_key: value}}. Split from providers.json (whose
# per-provider config never carries these) so the one file that must be 0600
# is small, and so a provider being reinstalled or dumped for support never
# walks the credential store by accident.
def _secrets_all():
    return read_json("secrets.json", {})


def get_secrets(provider_id):
    with _lock:
        return dict(_secrets_all().get(provider_id, {}))


def set_secrets(provider_id, values):
    """Replace provider_id's whole secret dict with `values`.

    Full replace, not merge -- the caller (registry.set_config) already read
    the old values and folded in only the keys the user actually submitted,
    which is what stops a blank password box from wiping a saved one. Merging
    again here would just hide that logic's bugs.
    """
    with _lock:
        all_ = _secrets_all()
        if values:
            all_[provider_id] = dict(values)
        else:
            all_.pop(provider_id, None)
        write_json("secrets.json", all_)


def drop_secrets(provider_id):
    with _lock:
        all_ = _secrets_all()
        if all_.pop(provider_id, None) is not None:
            write_json("secrets.json", all_)


def all_secret_values():
    """Every stored secret string, across every provider -- feeds straight
    into contract.redact()'s extra_secrets, so a credential typed into ONE
    provider's config still gets scrubbed out of every OTHER provider's error
    text that happens to quote it back."""
    with _lock:
        out = []
        for values in _secrets_all().values():
            for v in values.values():
                if isinstance(v, str) and v:
                    out.append(v)
        return out


# ---- admin -----------------------------------------------------------------
def admin_state():
    with _lock:
        return read_json("admin.json", {})


def _write_admin(state):
    write_json("admin.json", state)


def _hash_password(password, salt=None):
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                             n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return {"salt": salt.hex(), "hash": digest.hex(),
            "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P}


def set_admin_password(password):
    with _lock:
        state = admin_state()
        state["password"] = _hash_password(password)
        # A paper bootstrap code left lying around must stop working the
        # moment a real password exists, not just at the first successful
        # claim -- so any reset of the password retires it too.
        state.pop("bootstrap_token", None)
        _write_admin(state)


def check_admin_password(password):
    with _lock:
        rec = admin_state().get("password")
    if not rec:
        return False
    try:
        salt = bytes.fromhex(rec["salt"])
        expected = bytes.fromhex(rec["hash"])
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                              n=rec.get("n", _SCRYPT_N), r=rec.get("r", _SCRYPT_R),
                              p=rec.get("p", _SCRYPT_P), dklen=len(expected))
    except Exception:
        return False
    return hmac.compare_digest(got, expected)


def bootstrap_token():
    """The one-time install code, created on first call and handed to the
    installer. Idempotent until claimed: repeated calls (a retried install
    script) see the same token rather than invalidating the last one."""
    with _lock:
        state = admin_state()
        if state.get("password"):
            return None  # already claimed -- nothing left to bootstrap
        token = state.get("bootstrap_token")
        if not token:
            token = secrets.token_urlsafe(24)
            state["bootstrap_token"] = token
            _write_admin(state)
        return token


def claim(token, password):
    with _lock:
        state = admin_state()
        if state.get("password"):
            return False  # single-use: a password already exists
        expected = state.get("bootstrap_token")
        if not expected or not token or not hmac.compare_digest(str(expected), str(token)):
            return False
        state["password"] = _hash_password(password)
        state.pop("bootstrap_token", None)
        _write_admin(state)
        return True


def _csrf_key():
    # Generated once per install and persisted, not cached in a module
    # global -- a global would survive a test's CINEMATICA_STATE override
    # and hand back a key from a completely different state directory.
    with _lock:
        state = admin_state()
        key_hex = state.get("csrf_key")
        if not key_hex:
            key_hex = secrets.token_hex(32)
            state["csrf_key"] = key_hex
            _write_admin(state)
        return bytes.fromhex(key_hex)


def csrf_for(session):
    """The CSRF token that goes with a session token.

    HMAC(per-install key, session token) instead of a second random token
    that would need its own store: validity is a pure function of the
    session, so checking a submitted CSRF token is just recomputing this and
    comparing, with nothing extra to keep in sync or expire.
    """
    mac = hmac.new(_csrf_key(), (session or "").encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


# ---- sessions ---------------------------------------------------------------
def _evict_sessions_locked():
    now = time.time()
    for tok in [t for t, exp in _sessions.items() if exp <= now]:
        del _sessions[tok]
    if len(_sessions) > _SESSION_CAP:
        # Oldest-expiring first, so a client stuck retrying logins pushes out
        # its own earlier sessions rather than growing this dict forever.
        stale = sorted(_sessions.items(), key=lambda kv: kv[1])[:len(_sessions) - _SESSION_CAP]
        for tok, _ in stale:
            del _sessions[tok]


def new_session():
    with _lock:
        _evict_sessions_locked()
        tok = secrets.token_urlsafe(32)
        _sessions[tok] = time.time() + _SESSION_TTL
        return tok


def check_session(tok):
    with _lock:
        exp = _sessions.get(tok)
        if exp is None:
            return False
        if exp <= time.time():
            del _sessions[tok]
            return False
        return True


def drop_session(tok):
    with _lock:
        _sessions.pop(tok, None)
