"""Cinematica's provider subsystem.

Cinematica owns browsing, stream selection, playback, buffering and Sendspin
audio. Everything that talks to a *particular* outside service -- a catalogue,
a stream index -- lives in an installed provider package instead, behind the
JSON contract in contract.py.

Four modules, deliberately small:

  contract.py  the versioned wire format, and the normalisers that turn a
               provider's reply into the shapes core already understands
  store.py     what is installed, what it is configured with, what is active;
               atomic saves, secrets kept off the wire
  runner.py    provider processes: a bounded worker pool, request timeouts,
               output caps, and a crash that stays inside the provider
  host.py      the other side of runner.py -- runs INSIDE the provider process

Nothing here imports server.py, so importing this package is free of side
effects and the tests can drive it on its own.
"""

from .contract import CONTRACT_VERSION   # noqa: F401
