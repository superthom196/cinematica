# Cinematica server

The server supplies the library, selects and buffers streams, and prepares audio
for the TV or a Sendspin player. The Android TV app plays the video. The browser
interface provides setup, provider management and playback controls.

For installation and everyday use, start with [Install Cinematica](INSTALL.md).
This page describes the server for developers and people maintaining an installation.

## Components

| Component | Purpose |
|---|---|
| `server.py` | HTTP API, library ranking, playback jobs and media delivery |
| `providers/` | Add-on access, package execution, configuration and credentials |
| `index.html` | Browser interface; no frontend build step |
| Stremio container | Torrent download and media delivery; also supplies ffmpeg and ffprobe |
| `sendspin_bridge.py` | Decodes and sends audio to a Sendspin player |
| Android TV app | Browsing, playback and sync corrections |

Catalogue, metadata and stream requests go through `providers/gateway.py`.
The TV reports playback state to `/api/player/heartbeat` and receives commands in
response. The server hands it a media URL to play.

Torrent sources are served by Stremio. Direct HTTP sources pass through the
server's `/src/` proxy, which attaches any required upstream headers. Converted
media is served through `/audio/`.

The main server uses Python 3.11+ and the standard library. The Sendspin bridge
runs separately in a Python 3.12 environment with its own dependencies.

## Ports

| Default port | Use |
|---|---|
| `8090` | Browser interface and Cinematica API, reached by the TV and phone |
| `11470` | Stremio HTTP media server |
| `12470` | Stremio HTTPS port, published by the container template |
| `127.0.0.1:8091` | Local communication between Cinematica and the audio bridge |

The Sendspin player's address and port depend on the player. Optional adb access
uses the TV's debugging port.

## Configuration

The installer generates `cinematica.service` from
[`deploy/cinematica.service.in`](deploy/cinematica.service.in). Its environment
sets the server address and playback defaults. Use a systemd override for local
changes rather than editing the repository's example service file:

```bash
sudo systemctl edit cinematica
```

For example:

```ini
[Service]
Environment=HEVC_ONLY=0
Environment=NATIVE_AUDIO=ac3,eac3,aac
```

Apply changes with `sudo systemctl restart cinematica`. Only list audio formats
in `NATIVE_AUDIO` that the target TV can play correctly.

| Setting | Purpose |
|---|---|
| `PUBLIC_HOST` | Server address the TV uses for media URLs |
| `PORT` | Cinematica HTTP port; default `8090` |
| `STREMIO` | Stremio URL reachable by the TV |
| `CINEMATICA_STATE` | Provider/admin state directory; default `/var/lib/cinematica` |
| `CACHE_GB` | Torrent cache limit; default `30` |
| `HEVC_ONLY` | Prefer HEVC sources; enabled by default |
| `NATIVE_AUDIO` | Audio codecs accepted without conversion; default `ac3,eac3` |
| `AUDIO_FIX` | Enable conversion of unsupported audio |
| `ADB_TV` | Optional TV debugging address for bringing the app to the foreground |

Codec preferences may relax for a small source list. AV1 is currently rejected,
and there is no video-transcoding fallback. These defaults came from the original
TV setup and may need adjustment for other hardware.

Local `.env` settings include `SENDSPIN_CLIENT_URL`, the optional fallback player
address. Provider configuration belongs in the browser settings. See
[`deploy/env.example`](deploy/env.example) and the
[provider guide](docs/PROVIDERS.md).

The installer seeds Stremio settings from
[`deploy/server-settings.json`](deploy/server-settings.json): a 30 GiB cache,
10/16 MiB/s soft/hard download limits, 100 peer connections and no hardware
transcoding. Cinematica subsequently manages the cache limit and can adjust the
connection count from network measurements.

## Network audio

The bridge decodes audio to a PCM file and sends it to the selected Sendspin
player with playback timestamps. The TV uses timing feedback to adjust video
playback, with rate changes or seeks when needed. A separate audio-delay control
lets the viewer compensate for their display and speaker setup.

The bridge API listens on localhost. Player discovery and the selected player URL
are handled separately; an empty fallback URL does not prevent discovery.

See the [setup guide](INSTALL.md#optional-network-audio),
[`sendspin_bridge.py`](sendspin_bridge.py) and
[`tests/test_hifi_sync.py`](tests/test_hifi_sync.py). Automated timing tests do not
replace a sustained playback test on the target hardware.

## Saved state

Provider packages, settings, credentials and the admin account live under
`CINEMATICA_STATE`. The audio bridge's identity and pairing files normally live
beside `server.py`. Its Python runtime is separate again.

The [installation guide](INSTALL.md#where-everything-lives) lists default paths
and backup requirements. Keep credentials and pairing keys out of source control
and release archives.

## Development

From the repository root:

```bash
python3 -m unittest discover -s server/tests -t server
bash server/deploy/package.sh
```

The script creates the server archive under `dist/`.
For a manual server run, set `PUBLIC_HOST` and a writable `CINEMATICA_STATE` before
starting `python3 server/server.py`. Playback also needs the streaming container;
running the Python process alone does not install it.

On an installed server:

```bash
curl http://localhost:8090/api/health
journalctl -u cinematica -f
```

[`deploy/fake-app.sh`](deploy/fake-app.sh) simulates TV heartbeats for API tests;
it does not decode media or verify playback. Developer deployment over SSH is
documented in [`deploy/README.md`](deploy/README.md).

Before changing buffering, seeking or ffmpeg options, read the nearby code comments
and run the relevant playback tests. The
[historical audio investigation](docs/AUDIO-INVESTIGATION.md) records an earlier
VLC issue on one TV; it is not a current setup guide.
