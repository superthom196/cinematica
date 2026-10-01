"""Provider administration: the admin session constants, package upload
limits and safe unpacking, and the providers' health summary.
"""
import io, json, os, tarfile
from providers import contract, gateway

def providers_health():
    """Per-role provider status for /api/health, which the phone polls every
    15s -- this must stay a read: nothing here contacts a provider, only the
    registry state the gateway already tracks.

    "No providers configured" is a SETUP state, not a failure: a fresh
    install with nothing installed yet must still get a 200 here, the same
    as a fully configured one, never something that reads as unhealthy.
    """
    state = gateway.setup_required()
    roles = {role: {"provider": (state.get("roles") or {}).get(role),
                     "ready": gateway.available(role)}
             for role in contract.ROLES}
    return {"configured": bool(state.get("configured")), "roles": roles,
            "message": state.get("message") or ""}

# Everything here reaches the registry/store ONLY through providers/gateway.py
# -- this file must never import registry.py, store.py or addon.py directly
# (see gateway.py's module docstring). Unauthenticated except GET
# /api/setup/state (safe by construction) and the two routes that establish a
# session in the first place (claim, login); everything else goes through
# H._require_admin().
ADMIN_COOKIE = "cinematica_admin"
CSRF_HEADER = "X-Cinematica-CSRF"
# A package upload is the one POST body allowed past do_POST's small JSON
# cap -- generous enough for a real provider package (a few .py files, at
# most a small data file) without letting an upload exhaust memory here.
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_UNPACKED_BYTES = 96 * 1024 * 1024
MAX_PACKAGE_MEMBERS = 4000

# contract.ProviderError.code -> HTTP status. Never 500: an add-on or a
# package misbehaving is an ordinary, expected outcome of "an admin just
# pasted a URL or a file", not a bug in this process.
PROVIDER_ERROR_STATUS = {
    contract.E_CONFIG: 400, contract.E_AUTH: 401, contract.E_RATE: 429,
    contract.E_UPSTREAM: 502, contract.E_NOTFOUND: 404, contract.E_UNSUPPORTED: 400,
    contract.E_TIMEOUT: 504, contract.E_CRASH: 502, contract.E_PROTOCOL: 502,
    contract.E_INTERNAL: 500,
}


def _safe_extract_tar(data, dest_dir):
    """Extract a provider package's tar.gz into dest_dir, or raise ValueError
    with a message safe to show the admin who uploaded it.

    Every member is checked before ANY file is written: no absolute path, no
    ".." component, no symlink/hardlink (either could point outside dest_dir
    in a way a name check alone would miss), and a running total that never
    lets a small download unpack into an unbounded amount of disk.
    """
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as ex:
        raise ValueError("not a valid tar.gz package: %s" % ex)
    root = os.path.realpath(dest_dir)
    with tf:
        members = tf.getmembers()
        if len(members) > MAX_PACKAGE_MEMBERS:
            raise ValueError("package has too many files")
        total = 0
        safe = []
        for m in members:
            name = m.name or ""
            norm = os.path.normpath(name)
            if (not name or name.startswith("/") or name.startswith("\\")
                    or os.path.isabs(norm) or norm == ".." or norm.split(os.sep)[0] == ".."):
                raise ValueError("archive member %r escapes the package directory" % name[:200])
            if m.issym() or m.islnk():
                raise ValueError("archive member %r is a link, which is not allowed" % name[:200])
            if not (m.isfile() or m.isdir()):
                raise ValueError("archive member %r is not a regular file" % name[:200])
            dest_path = os.path.realpath(os.path.join(dest_dir, norm))
            if dest_path != root and not dest_path.startswith(root + os.sep):
                raise ValueError("archive member %r escapes the package directory" % name[:200])
            total += max(0, m.size)
            if total > MAX_PACKAGE_UNPACKED_BYTES:
                raise ValueError("package is too large uncompressed")
            safe.append(m)
        try:
            tf.extractall(dest_dir, members=safe, filter="data")
        except TypeError:
            # Python < 3.12 has no extraction filter -- every member was
            # already vetted by name/type above, so a plain extractall is safe.
            tf.extractall(dest_dir, members=safe)


def _read_package_manifest(package_dir):
    """manifest.json from an extracted package, validated, with its declared
    entry file confirmed present -- a package that cannot be described, or
    whose entry is missing, is never installed."""
    path = os.path.join(package_dir, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ValueError("package has no manifest.json")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ValueError("package manifest.json is not valid JSON: %s" % ex)
    manifest = contract.validate_manifest(raw)
    root = os.path.realpath(package_dir)
    entry_path = os.path.realpath(os.path.join(package_dir, manifest["entry"]))
    if entry_path != root and not entry_path.startswith(root + os.sep):
        raise ValueError("manifest entry escapes the package directory")
    if not os.path.isfile(entry_path):
        raise ValueError("package is missing its declared entry file %r" % manifest["entry"])
    return manifest
