"""Followed YouTube-style channels: the background refresh of their uploads,
and the wall and detail tiles both clients render.
"""
import time
from providers import contract, gateway
import shelf

import config, core

# cid -> ts of the last snap refresh, so channel_watch() only re-fetches a
# channel's avatar/banner/subscriber count once a day, not on every sweep.
_channel_snap_checked = {}

def channel_watch():
    """Keep every followed channel's stored "latest" videos, and once a day
    its snap (avatar/banner/subscribers/description), current -- so the wall
    and the channel page are always served from disk and never wait on a
    live provider round trip. Same poll-and-sleep shape as cache_watch()."""
    time.sleep(60)
    while True:
        if not gateway.available(contract.ROLE_CHANNELS):
            time.sleep(config.CHANNEL_POLL_MIN * 60)
            continue
        now = time.time()
        followed = list(shelf.followed_ids())
        # An unfollowed channel's check time is of no further use.
        for cid in set(_channel_snap_checked) - set(followed):
            _channel_snap_checked.pop(cid, None)
        for cid in followed:
            try:
                r = gateway.channel_latest(cid)
                shelf.set_latest(cid, r["videos"])
                if now - _channel_snap_checked.get(cid, 0) > 24 * 3600:
                    shelf.set_snap(cid, gateway.channel_details(cid))
                    _channel_snap_checked[cid] = now
            except contract.ProviderError as ex:
                print("channels: %s failed for %s: %s" % (ex.op, cid, ex.message), flush=True)
            except Exception as ex:
                print("channels: unexpected error for %s: %s" % (cid, ex), flush=True)
            time.sleep(1)
        # forced: the throttle would otherwise leave a sweep unsaved until the
        # next one, CHANNEL_POLL_MIN later
        shelf.save(force=True)
        time.sleep(config.CHANNEL_POLL_MIN * 60)

# Followed channels, their uploads, and a hand-off to an external app -- see
# providers/contract.py's channels role. Nothing here buffers or proxies a
# video: play() only records that it was opened and returns the provider's
# {url, package, label} for the TV to hand to another app.

# Both keyed by gateway.cache_tag(ROLE_CHANNELS), same convention as _genres
# above -- a provider swap or a config edit can never keep serving results
# gathered under the old one.
_channel_popular_cache = {"at": 0, "data": [], "tag": None}
_channel_details_cache = {}   # "<tag>@<id>" -> {"at":ts, "channel":{...}}, 1h TTL


def _channel_item(ch, cid=None):
    """The wall/detail tile shape both clients render, from either a
    normalised gateway channel (ch carries "id") or a stored shelf snap (cid
    given separately, since a snap has no id of its own). Decorated with
    this household's own state -- followed/new -- via shelf.channel_view,
    never from anything the provider said."""
    cid = cid or ch.get("id")
    view = shelf.channel_view(cid)
    return {
        "id": cid,
        "kind": "channel",
        "title": ch.get("title"),
        "poster": ch.get("avatar"),
        "backdrop": ch.get("banner"),
        "overview": ch.get("description"),
        "subscribers": ch.get("subscribers"),
        "latest_at": ch.get("latest_at"),
        "followed": view["followed"],
        "new": view["new"],
    }


def channel_popular(limit=40, seeds=()):
    """Cached 6h, same TTL as the catalogue's own popular list, and keyed on
    the followed channels (`seeds`) as well as the provider: a provider may
    suggest channels like the ones followed, so following one more must not
    wait six hours to count. Errors are never cached -- an empty result here
    is what a not-yet-usable index looks like, and caching that would keep
    the wall's Popular row empty for 6 hours after the provider recovers."""
    seeds = sorted(seeds)
    tag = "%s|%s" % (gateway.cache_tag(contract.ROLE_CHANNELS), ",".join(seeds))
    with core._lock:
        c = _channel_popular_cache
        if c["data"] and c.get("tag") == tag and time.time() - c["at"] < config.TTL_LIST:
            return c["data"]
    try:
        items = gateway.channel_popular(limit, seeds=seeds)["items"]
    except contract.ProviderError:
        return []
    with core._lock:
        _channel_popular_cache.update(at=time.time(), data=items, tag=tag)
    return items


def channel_details_cached(cid):
    """channel_details(), cached 1h -- for a channel NOT followed (a
    followed one is served from its stored shelf snap instead, refreshed by
    channel_watch()). Errors propagate; there is no stale copy worth
    swallowing an error for."""
    tag = "%s@%s" % (gateway.cache_tag(contract.ROLE_CHANNELS), cid)
    with core._lock:
        e = _channel_details_cache.get(tag)
        if e and time.time() - e["at"] < 3600:
            return e["channel"]
    ch = gateway.channel_details(cid)
    with core._lock:
        _channel_details_cache[tag] = {"at": time.time(), "channel": ch}
        core._evict(_channel_details_cache, 3600, config.MAX_STREAM_ENTRIES)
    return ch
