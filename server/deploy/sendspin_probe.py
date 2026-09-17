#!/usr/bin/env python3
"""Manual sendspin probe: connect, print the hello, push 3s of a test tone, exit.

Run by hand with the venv312 interpreter (never as a service, never on a live
box while a film might be playing on the same player):
  ~/cinematica-venv312/bin/python server/deploy/sendspin_probe.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import sys
import time
from pathlib import Path

from aiosendspin.clock import RawMonotonicClock
from aiosendspin.models.types import ConnectionReason
from aiosendspin.noise.keys import Identity, b64url_decode
from aiosendspin.noise.trust_store import FileServerPairingStore
from aiosendspin.server import AudioFormat, SendspinServer

SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_DIR = SCRIPT_DIR.parent


def load_env(path):
    out = {}
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return out


ENV = load_env(str(SERVER_DIR / ".env"))
CLIENT_URL = os.environ.get("SENDSPIN_CLIENT_URL") or ENV.get("SENDSPIN_CLIENT_URL", "")
STATE_DIR = Path(os.environ.get("SENDSPIN_STATE_DIR") or ENV.get("SENDSPIN_STATE_DIR") or SERVER_DIR)


def tone(seconds=3.0, hz=440.0, rate=48000):
    frames = bytearray()
    for i in range(int(seconds * rate)):
        s = int(32767 * 0.3 * math.sin(2 * math.pi * hz * i / rate))
        frames += struct.pack("<hh", s, s)
    return bytes(frames)


async def main():
    if not CLIENT_URL:
        sys.exit("sendspin_probe: SENDSPIN_CLIENT_URL not set")

    identity = Identity.from_private_bytes(
        b64url_decode(json.loads((STATE_DIR / "sendspin_identity.json").read_text())["private_b64u"])
    )
    store = await FileServerPairingStore.open(STATE_DIR / "sendspin_pairing.json")
    server = SendspinServer(
        asyncio.get_event_loop(), identity, "Cinematica-probe",
        pairing_store=store, clock=RawMonotonicClock(),
        allow_unencrypted=True,  # same transition-mode clients as the bridge
    )

    print("connecting to", CLIENT_URL)
    await server.connect_to_client_and_wait(
        CLIENT_URL, connection_reason=ConnectionReason.PLAYBACK, retry_initial_connection=True
    )
    client_id = server.get_client_id_for_url(CLIENT_URL)
    client = server.get_client(client_id)
    hello = client.info
    print("hello: name=%s roles=%s unpaired_access=%s" % (hello.name, hello.supported_roles, hello.unpaired_access.enabled))
    print("pair methods:", hello.supported_pair_methods)
    print("player format:", hello.player_support)

    if hello.unpaired_access.enabled:
        await server.trust_unpaired(client_id)

    deadline = time.monotonic() + 10
    while not client.roles_by_family("player") and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    stream = client.group.start_stream()
    stream.set_live_source(False)
    fmt = AudioFormat(48000, 16, 2)
    pcm = tone()
    play_start_us = None
    for i in range(0, len(pcm), 9600):
        stream.prepare_audio(pcm[i:i + 9600], fmt)
        ts = await stream.commit_audio()
        if play_start_us is None:
            play_start_us = ts
        await stream.sleep_to_limit_buffer(1_000_000)

    print("play_start_us:", play_start_us, "now_us:", stream.now_us())
    await client.group.stop()
    server.disconnect_from_client(CLIENT_URL)
    await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main())
