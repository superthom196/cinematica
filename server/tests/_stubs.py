"""Stand-ins for the bridge's third-party imports (aiosendspin, zeroconf,
aiohttp.web), so its decoder lifecycle and status logic can be tested with the
stdlib alone -- on a laptop and in CI, where none of them are installed. Only
what the bridge touches at import time or in the paths under test exists here.
Install with install() BEFORE importing sendspin_bridge."""

import sys
import types


class _Resp:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status


def _json_response(data, status=200):
    return _Resp(data, status)


class _Enum:
    Added, Updated, Removed = "Added", "Updated", "Removed"


class _Anything:
    def __init__(self, *a, **k):
        self.args = a
        self.kwargs = k

    def __getattr__(self, name):
        return _Anything()

    def __call__(self, *a, **k):
        return _Anything()


class _AudioFormat:
    def __init__(self, sample_rate, bit_depth, channels):
        self.sample_rate, self.bit_depth, self.channels = sample_rate, bit_depth, channels


def _module(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def install():
    web = _module("aiohttp.web", json_response=_json_response, Application=_Anything,
                  AppRunner=_Anything, TCPSite=_Anything)
    _module("aiohttp", web=web)
    _module("zeroconf", ServiceStateChange=_Enum)
    _module("zeroconf.asyncio", AsyncServiceBrowser=_Anything, AsyncServiceInfo=_Anything,
            AsyncZeroconf=_Anything)
    _module("aiosendspin")
    _module("aiosendspin.clock", RawMonotonicClock=_Anything)
    _module("aiosendspin.models")
    _module("aiosendspin.models.types", ConnectionReason=_Anything())
    _module("aiosendspin.noise")
    _module("aiosendspin.noise.keys", Identity=_Anything, b64url_decode=lambda s: s)
    _module("aiosendspin.noise.trust_store", FileServerPairingStore=_Anything)
    _module("aiosendspin.server", AudioFormat=_AudioFormat, SendspinServer=_Anything)
