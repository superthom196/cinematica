#!/usr/bin/env python3
"""
Cinematica — browse a pluggable catalogue, see the best available stream, and
hand it to the player on the TV.

Stdlib only, deliberately: no pip install, nothing to break on a rebuild.

  catalogue/metadata/streams providers -> reached only through providers/gateway.py
  Stremio    -> http://{PUBLIC_HOST}:11470/{infoHash}/{fileIdx}
  TV app     -> picks that URL up over the heartbeat channel and plays it
  adb        -> optional: wakes the TV app to the foreground (phone remote)
"""
import os, sys, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from providers import gateway   # noqa: E402
# Every module, in an order where nothing that runs at import time reads a
# module that has not finished loading. Between them they are the server;
# this file only starts it.
import config, core, nowplaying, mediaprobe, streams, netprofile, catalogue, torrents, transcode, sendspin, tvlink, jobs, browser_session, watching, channels, admin, routes   # noqa: E402,F401

if __name__ == "__main__":
    # Anything still converting was started by the previous process and is
    # unknown to this one: an orphan by definition (see _kill_orphans).
    threading.Thread(target=transcode.transcode_stop_all, daemon=True).start()
    threading.Thread(target=torrents.prefetch, daemon=True).start()
    threading.Thread(target=torrents.cache_watch, daemon=True).start()
    threading.Thread(target=torrents.cache_size_apply, daemon=True).start()
    threading.Thread(target=channels.channel_watch, daemon=True).start()
    if config.SENDSPIN_ENABLED:
        threading.Thread(target=sendspin._ss_worker, daemon=True).start()
    print("cinematica on :%d  (stremio=%s  phone remote=%s)"
          % (config.PORT, config.STREMIO, config.ADB_TV or "off"), flush=True)
    _tok = gateway.bootstrap_token()
    if _tok:
        # None once a password has been claimed -- printed every restart
        # until then, since a fresh install has no other way to learn it.
        print("cinematica: no admin password set -- claim this install at "
              "/api/setup/claim with token: %s" % _tok, flush=True)
    routes.Server(("0.0.0.0", config.PORT), routes.H).serve_forever()
