"""What was last handed to a player, persisted so a restart mid-film can
pick up where it was. A leaf: imports none of the other modules, so any
of them can read it while loading.
"""
import json, os

import config

# One transcode per stream, hard-capped overall. Without this, every player
# reconnect spawns another ffmpeg while the old one keeps running against a dead
# socket -- four of them at once starved the CPU and caused the very glitching
# the transcoder exists to prevent.
# Persisted, because it only ever lived in memory before and every service
# restart made a film that was plainly playing report as "Idle".
NOW_FILE = os.path.join(config.HERE, "nowplaying.json")
def _now_load():
    try:
        with open(NOW_FILE) as f:
            return json.load(f)
    except Exception:
        return {}
def _now_save(d):
    try:
        tmp = NOW_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, NOW_FILE)
    except Exception as ex:
        print("nowplaying: could not save %s: %s" % (NOW_FILE, ex), flush=True)

_now = _now_load()        # what was last handed to the player
