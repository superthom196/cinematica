"""The one lock every module's shared state is guarded by, and the cache
eviction every module's caches use.
"""
import threading, time

# RLock, not Lock. Every thread in a 22-thread dump was blocked on this lock with
# none holding it in a visible frame, which means the holder was itself blocked
# taking it a second time -- a permanent self-deadlock that froze the whole
# server. Re-entrancy makes that class of bug impossible rather than fatal.
_lock   = threading.RLock()

def _evict(cache, ttl, cap):
    """Drop expired entries, then cap what's left to the newest `cap` by "at".
    Call with _lock held -- this mutates the dict in place."""
    now = time.time()
    for k in [k for k, v in cache.items() if now - v.get("at", 0) > ttl]:
        del cache[k]
    if len(cache) > cap:
        oldest = sorted(cache.items(), key=lambda kv: kv[1].get("at", 0))
        for k, _ in oldest[:len(cache) - cap]:
            del cache[k]
