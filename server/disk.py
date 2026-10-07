"""How much of the disk the cache may use. A leaf: imports only config and
core, so netprofile's picker and torrents' sweep can both ask it.

Everything a film writes lands on one drive, beside this file: Stremio's
torrents under stremio/ (the container's bind mount), and the AC3 conversion
and Sendspin PCM under transcode/. CACHE_GB used to be the only limit, and a
fast link picked whatever fitted in it whatever the drive held -- a 20 GB pick
with 15 GB free filled the disk mid-film and the film simply stopped. The cache
now gets the smaller of:
  * CACHE_GB, the operator's own ceiling;
  * what is free right now plus what the cache already holds, less
    DISK_RESERVE_GB (8) left for the OS -- on a 32 GB system drive that is
    most of what the OS does not already use.
"""
import os, time

import config, core

GB = 1024.0 ** 3
STREMIO_CACHE = os.path.join(config.HERE, "stremio", "stremio-cache")
# Measured at most this often: score() asks for every candidate it ranks, and
# walking the cache once per candidate would cost more than the ranking.
MEASURE_EVERY = 30
_m = {"at": 0.0, "v": None}


def _held_bytes():
    """Bytes the cache directories really occupy -- allocated blocks, not
    st_size, since a torrent's files are sparse until their pieces arrive.
    A directory this process cannot read counts as empty, which only ever
    makes the budget smaller."""
    n = 0
    for root in (STREMIO_CACHE, config.TC_HOST):
        for dp, _dns, fns in os.walk(root):
            for f in fns:
                try:
                    st = os.lstat(os.path.join(dp, f))
                except OSError:
                    continue
                blocks = getattr(st, "st_blocks", None)
                n += blocks * 512 if blocks is not None else st.st_size
    return n


def _measure():
    """(total, free, held) in bytes for the drive holding the cache."""
    st = os.statvfs(config.HERE)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize, _held_bytes()


def measure(fresh=False):
    """{"total_gb", "free_gb", "held_gb", "cache_gb"}, cached MEASURE_EVERY
    seconds, or None if the drive cannot be read at all -- a box that cannot
    statvfs is left on CACHE_GB alone rather than refused everything."""
    now = time.time()
    with core._lock:
        if not fresh and _m["v"] is not None and now - _m["at"] < MEASURE_EVERY:
            return _m["v"]
    try:
        total, free, held = _measure()
    except Exception as ex:
        print("disk: could not measure %s: %s" % (config.HERE, ex), flush=True)
        return None
    cap = free + held - config.DISK_RESERVE_GB * GB
    if config.CACHE_GB > 0:
        cap = min(cap, config.CACHE_GB * GB)
    v = {"total_gb": round(total / GB, 1), "free_gb": round(free / GB, 1),
         "held_gb": round(held / GB, 2), "cache_gb": round(max(0.0, cap) / GB, 1)}
    with core._lock:
        _m.update(at=now, v=v)
    return v


def cache_gb():
    """The most the cache may hold, in GB. Never below 0.1: a caller taking
    min() over its limits must not read 0 as "no limit"."""
    v = measure()
    if v is None:
        return config.CACHE_GB if config.CACHE_GB > 0 else float("inf")
    return max(0.1, v["cache_gb"])


def over():
    """True when the cache holds more than it may, or the drive is down to
    its reserve -- either way, everything not in use has to go now."""
    v = measure(fresh=True)
    if v is None:
        return False
    return v["held_gb"] > v["cache_gb"] or v["free_gb"] < config.DISK_RESERVE_GB
