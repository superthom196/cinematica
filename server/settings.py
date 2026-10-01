"""Where the server and the Sendspin bridge get their settings.

One rule for both processes: a variable set in the environment wins (an empty
value included -- BIAS_LANG= means "any language", not "use the default"),
then the same name in .env beside these scripts ($ENV_FILE overrides where
that is), then the default the caller gives. The server used to read .env for
one setting and the environment for the other ninety-odd, so a value written
into .env by hand was silently ignored unless it happened to be that one.

Stdlib only, like the rest of the server.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    """KEY=value lines; blank lines, comments and lines with no "=" skipped,
    surrounding quotes stripped. A missing file is no settings, not an error."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return out


ENV_FILE = os.environ.get("ENV_FILE", os.path.join(HERE, ".env"))
ENV = load_env(ENV_FILE)


def get(key, default=None):
    """The setting's raw string: the environment, then .env, then `default`."""
    if key in os.environ:
        return os.environ[key]
    if key in ENV:
        return ENV[key]
    return default


def flag(key, default):
    """An on/off setting. Unset is `default`; otherwise anything but 0, false,
    no, off or empty is on -- so AUTOPLAY_NEXT=true means on, where an exact
    match on "1" used to read it as off."""
    v = get(key)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "", "false", "no", "off")
