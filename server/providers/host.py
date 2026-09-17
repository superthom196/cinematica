#!/usr/bin/env python3
"""Runs INSIDE a provider subprocess: `python3 -m providers.host <package_dir>`.

Loads the package's manifest, imports its declared entry file, then speaks the
line-delimited JSON protocol on stdin/stdout that runner.py's Pool drives. One
request in, one response out, forever, until stdin closes.

This process is deliberately paranoid about the provider's code, because the
provider IS someone else's code: a bug in it must become a normal error
response, never a dead process or a corrupted stdout stream, because either of
those takes every OTHER queued request on this worker down with it.
"""
import importlib.util
import json
import os
import sys

if __package__ in (None, ""):
    # `python3 providers/host.py <dir>` (a plain script path, no package
    # context) can't do a relative import -- make server/ importable by path
    # instead of failing before a single request is ever read.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from providers import contract
else:
    from . import contract

# Captured before anything provider-supplied has run, and used directly
# instead of through the `sys.stdout` name. A provider that reassigns
# sys.stdout and never restores it (rather than the stray print() this file
# already guards against) still can't touch this: _write() never looks up
# sys.stdout again after import time.
_STDOUT = sys.stdout


def _write(obj):
    _STDOUT.write(json.dumps(obj, separators=(",", ":"), default=str))
    _STDOUT.write("\n")
    _STDOUT.flush()


def _txt(v):
    return v if isinstance(v, str) else ("" if v is None else str(v))


# ---- startup: manifest + entry module ---------------------------------------
def _load_manifest(package_dir):
    path = os.path.join(package_dir, "manifest.json")
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    # Validated again here, not trusted from install time: the package
    # directory on disk can be edited or replaced between "installed" and
    # "launched", and running it on the strength of an old validation means
    # trusting whatever entry/capabilities it claims NOW, unchecked.
    return contract.validate_manifest(raw)


def _load_entry(package_dir, manifest):
    entry_path = os.path.join(package_dir, manifest["entry"])
    # Prepended, not appended: the entry file and anything it imports (a
    # sibling helper module shipped in the same package) must resolve before
    # a same-named module already on sys.path -- otherwise two providers that
    # both ship a "utils.py" would shadow each other process-wide instead of
    # each staying self-contained inside its own subprocess.
    if package_dir not in sys.path:
        sys.path.insert(0, package_dir)
    module_name = "cinematica_provider_%s" % manifest["id"].replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    if spec is None or spec.loader is None:
        raise contract.ContractError("cannot load entry %r" % manifest["entry"])
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ---- dispatch -----------------------------------------------------------------
def _dispatch(module, op, config, params):
    name = op.replace(".", "_")
    fn = getattr(module, name, None)
    if fn is not None:
        return fn(config, params)
    handler = getattr(module, "handle", None)
    if handler is not None:
        return handler(op, config, params)
    raise contract.ProviderError(contract.E_UNSUPPORTED, "provider does not implement %r" % op)


def _call_with_stdout_muted(module, op, config, params):
    # A provider's stray print() -- debug leftovers, a dependency that logs to
    # stdout by default -- would otherwise land in the middle of this
    # process's ONE output channel and desync the runner's line-per-response
    # read loop for good. Stdout is protocol; only _write() may use it, so
    # everything the provider itself does with it is quietly redirected to
    # stderr, which is free-form and already drained separately.
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        return _dispatch(module, op, config, params)
    finally:
        sys.stdout = saved


# ---- per-op result normalisation ---------------------------------------------
# A provider returns plausible-looking JSON; core needs the EXACT shapes its
# scoring and rendering code already assumes. Running every result through
# the matching contract normaliser here means a malformed reply becomes a
# named error inside THIS process, not a KeyError/TypeError deep in core with
# a provider's subprocess nowhere in the stack to blame.
def _require_list(result, what):
    if not isinstance(result, list):
        raise contract.ContractError("%s result is %s, expected a list" % (what, type(result).__name__))
    return result


def _catalogue_list_result(result, provider_id, params):
    if isinstance(result, list):
        items, rest = result, {}
    elif isinstance(result, dict):
        items = result.get("items")
        if not isinstance(items, list):
            raise contract.ContractError("catalogue result has no 'items' list")
        rest = {k: v for k, v in result.items() if k != "items"}
    else:
        raise contract.ContractError(
            "catalogue result is %s, expected a list or {'items': [...]}" % type(result).__name__)
    kind_hint = params.get("kind") if isinstance(params, dict) else None
    # normalise_entry raises on a title with no usable id/kind rather than
    # dropping it silently -- one malformed row fails the whole page, which is
    # the contract's call (see contract.py's normalise_entry docstring), not
    # a choice made here.
    rest["items"] = [contract.normalise_preview(e, provider_id, kind_hint) for e in items]
    return rest


def _episodes_result(result, params):
    if isinstance(result, dict):
        eps = result.get("episodes")
        hint = result.get("season")
    elif isinstance(result, list):
        eps, hint = result, None
    else:
        raise contract.ContractError("episodes result is %s, expected a list" % type(result).__name__)
    if hint is None and isinstance(params, dict):
        hint = params.get("season")
    eps = _require_list(eps, "episodes") if eps is not None else []
    out = []
    for e in eps:
        ne = contract.normalise_episode(e, hint)
        if ne is None:
            # Season/episode number is what stream lookup keys on downstream;
            # an episode missing it is not a lesser row, it is unplayable, so
            # it is treated the same as any other malformed entry.
            raise contract.ContractError("episode entry missing season/episode number")
        out.append(ne)
    return {"episodes": out}


def _streams_result(result, provider_id):
    items = result.get("candidates") if isinstance(result, dict) else result
    items = _require_list(items, "streams")
    candidates, rejected = [], 0
    for v in items:
        c, reason = contract.normalise_candidate(v, provider_id)
        if c is None:
            # Counted, not raised: one bad candidate in a list of forty costs
            # a source, not the whole lookup (see normalise_candidate's own
            # docstring -- this mirrors that choice rather than escalating it).
            rejected += 1
        else:
            candidates.append(c)
    return {"candidates": candidates, "rejected": rejected}


def _genres_result(result):
    # contract.py defines no normaliser for a genre list -- browse/search
    # filters (genre_ids) are still compared against whatever this returns,
    # so its shape is still worth catching here rather than at filter time.
    items = _require_list(result, "genres")
    out = []
    for g in items:
        if not isinstance(g, dict):
            raise contract.ContractError("genre entry is %s, expected an object" % type(g).__name__)
        gid, name = _txt(g.get("id")).strip(), _txt(g.get("name")).strip()
        if not gid or not name:
            raise contract.ContractError("genre entry missing id or name")
        out.append({"id": gid, "name": name[:120]})
    return out


def _normalise_result(op, result, provider_id, params):
    if op in (contract.OP_BROWSE, contract.OP_SEARCH):
        return _catalogue_list_result(result, provider_id, params)
    if op == contract.OP_DETAILS:
        if not isinstance(result, dict):
            raise contract.ContractError("details result is %s, expected an object" % type(result).__name__)
        return contract.normalise_detail(result, provider_id, params.get("kind") if isinstance(params, dict) else None)
    if op == contract.OP_EPISODES:
        return _episodes_result(result, params)
    if op == contract.OP_RATINGS:
        if not isinstance(result, dict):
            raise contract.ContractError("ratings result is %s, expected an object" % type(result).__name__)
        return contract.normalise_ratings(result)
    if op == contract.OP_STREAMS:
        return _streams_result(result, provider_id)
    if op == contract.OP_GENRES:
        return _genres_result(result)
    # provider.describe / config.test have no contract-level shape -- a
    # provider is only confirming it is alive or that credentials work. Still
    # require the reply be JSON-shaped so a provider handing back e.g. a raw
    # object fails here, in-process, rather than at json.dumps() below with
    # the response line already half-written.
    if result is not None and not isinstance(result, (dict, list, str, int, float, bool)):
        raise contract.ContractError("%s result is %s, not JSON-shaped" % (op, type(result).__name__))
    return result


# ---- request loop --------------------------------------------------------------
def _handle_line(line, module, provider_id):
    try:
        req = json.loads(line)
    except Exception as exc:
        _write({"id": None, "ok": False,
                "error": {"code": contract.E_PROTOCOL, "message": "malformed request: %s" % contract.redact(exc)}})
        return
    if not isinstance(req, dict):
        _write({"id": None, "ok": False,
                "error": {"code": contract.E_PROTOCOL, "message": "request is not an object"}})
        return

    rid = req.get("id")
    op = req.get("op")
    config = req.get("config") if isinstance(req.get("config"), dict) else {}
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    if not op or not isinstance(op, str):
        _write({"id": rid, "ok": False, "error": {"code": contract.E_PROTOCOL, "message": "request has no 'op'"}})
        return

    try:
        raw = _call_with_stdout_muted(module, op, config, params)
        result = _normalise_result(op, raw, provider_id, params)
    except contract.ProviderError as exc:
        _write({"id": rid, "ok": False, "error": exc.as_dict()})
    except contract.ContractError as exc:
        _write({"id": rid, "ok": False, "error": {"code": exc.code, "message": contract.redact(exc.message)}})
    except Exception as exc:
        # A provider is arbitrary third-party code; anything it raises that it
        # did not itself turn into a ProviderError (a KeyError, a dependency's
        # own exception type, anything) must not kill this process -- doing so
        # would take every OTHER request already queued on this worker down
        # with it, for a bug in one title.
        _write({"id": rid, "ok": False, "error": {"code": contract.E_INTERNAL, "message": contract.redact(exc)}})
    else:
        _write({"id": rid, "ok": True, "result": result})


def main(argv):
    if len(argv) < 2:
        print("usage: python3 -m providers.host <package_dir>", file=sys.stderr)
        return 2
    package_dir = argv[1]

    try:
        manifest = _load_manifest(package_dir)
        module = _load_entry(package_dir, manifest)
    except Exception as exc:
        # Nothing has read a request yet, so there is no id to answer against
        # -- fail to stderr and exit nonzero. The runner sees a dead process
        # and raises E_CRASH, which is the honest answer ("this provider
        # cannot run"), not a protocol error about a response that never had
        # a chance to exist.
        print("host: failed to start provider: %s" % contract.redact(exc), file=sys.stderr)
        return 1

    provider_id = manifest["id"]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        _handle_line(line, module, provider_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
