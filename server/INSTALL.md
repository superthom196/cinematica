# Install Cinematica

This guide sets up the Cinematica server on Linux. The browser interface can
browse and play on its own; for TV playback you will also need the
[Cinematica TV app](https://github.com/superthom196/cinematica/tree/main/tv).

## What you need

- A 64-bit x86-64 or arm64 machine running Debian 12+, Ubuntu 24.04+, or
  Raspberry Pi OS Bookworm+, with systemd.
- Administrator access through `sudo`.
- Python 3.11 or newer. The installer checks the version.
- Docker. The installer can install it if needed.
- Space for the torrent cache and audio conversions. The default torrent cache
  is 30 GiB; allow additional space for converted audio and other files.
- A server address that your TV and phone can reach on your home network.

Ethernet is recommended for high-bitrate video. A DHCP reservation on your router
helps keep the server's address the same. If you use a hostname, check that the TV
can resolve it.

## Install the server

Download the archive and matching checksum from
[Releases](https://github.com/superthom196/cinematica/releases). For version 1.0.0:

```bash
sha256sum -c cinematica-server-1.0.0.tar.gz.sha256
tar xzf cinematica-server-1.0.0.tar.gz
cd cinematica-server
sudo ./install.sh
```

Enter the server's LAN address when prompted. The installer creates the service
account, copies the application to `/opt/cinematica`, starts the streaming container
and sets up the server and network audio services. Downloads can take several minutes.

At the end, it prints a setup address and a one-time token. Keep these for the next step.
To preview the installation without making changes, run `./install.sh --dry-run`.

Read the last box before going on. It says which of three things happened: the
server answered its own health check and is running, it was installed but never
answered, or it was installed with the service left to you (`--skip-systemd`).
The installer exits non-zero when the server did not come up, or when the
one-time token could not be read -- an empty token is not the same as a claimed
account, and the box distinguishes them. Re-running the installer is safe.

### Installer options

| Option | Use |
|---|---|
| `--host ADDRESS` | Set the server address without a prompt |
| `--dir PATH` | Change the application directory |
| `--user USER` | Use an existing service account |
| `--adb IP:PORT` | Let the server bring the TV app to the foreground |
| `--non-interactive` | Use defaults without prompting |
| `--install-docker` | Allow Docker installation without prompting |
| `--dry-run` | Print the steps without changing anything |
| `--skip-docker` | Leave container setup to you |
| `--skip-systemd` | Leave service setup to you |

The service account belongs to the Docker group so it can run the audio tools
inside the container. That grants it administrator-level access through Docker.

## Adding providers

1. Open the printed setup address in a browser, such as `http://192.168.1.42:8090/`.
2. Enter the one-time token and choose an admin password.
3. Add providers using a compatible add-on manifest URL or a Python package.
4. Enter any required configuration, test each provider, and assign its roles.

You need a **catalogue** for browsing, **metadata** for title and episode details,
and **streams** for playback. A single provider may supply several roles. If the
catalogue also supplies metadata, Cinematica can use it for both.

See [the provider guide](docs/PROVIDERS.md) for configuration and compatibility.
Cinematica includes no provider accounts or API keys.

If you lose the token before setting a password, the server log contains it:

```bash
journalctl -u cinematica -n 100
```

## Connect the TV

Install and open the TV app. Select your server or enter its LAN address, including
port `8090` if needed. The app remembers the connection. The TV app must be
running to play a title on the TV, unless you have configured the optional app
wake-up feature below.

The same browser address used for setup browses the library and can play a
film or episode directly in the browser, with no TV app involved.

## Play in the browser

Open the same browser address used for setup. Browsing, search and title
details work as before; the button to start a film or episode is now **Play**,
and it plays there in the browser rather than only sending a command to the TV.

The server tries each candidate source in turn: if your browser can decode its
container, video and audio, it plays directly; otherwise the server repackages
it or converts its audio to AAC, without re-encoding the video. There is no
video transcoding, so a title whose video track none of your browsers can
decode — see the limits below — cannot be played in the browser at all; the
page explains why.

Everything the browser needs is fetched from the same address you opened, so
playback works over an existing Tailscale or other remote connection with no
extra configuration.

Only one device plays at a time: the TV or a browser. Starting playback on one
refuses a competing request from the other with "Another device is playing";
stop it first to hand playback over. Two browsers compete the same way.

Seeking works anywhere in the title. Seeking into a part not yet prepared
restarts preparation from that point, and accuracy is bounded by the source's
keyframe interval (a few seconds), because video is copied rather than
re-encoded.

Playing in the browser does not start network audio and does not send commands
to the TV; the TV app and its hi-fi audio path are unaffected.

Limits to expect:

- Firefox cannot decode HEVC, and Chrome's HEVC support depends on the
  machine's hardware. A title that exists only as HEVC will report no
  compatible source in such a browser; this is expected, not a fault.
- Seeking far ahead in a torrent-backed source is slow regardless, because the
  source downloads in order and the requested part may not have arrived yet.
  This shows as buffering.
- Browser audio is stereo; surround is not downmixed for the browser in this
  version.
- A 4K source can be selected and still be too large to deliver smoothly over
  a weak connection, which also shows as buffering rather than an error.

## Optional: network audio

Network audio sends the soundtrack to a Sendspin player while the TV displays the
picture. This can be a Raspberry Pi running a Sendspin client connected to your hi-fi.

1. Check that the player is running and reachable on your home network.
2. On the TV, open **Settings** and turn **Network audio** on.
3. Select your player in **Network audio player**.
4. Start playback.

The server's bridge service handles the connection to the player. Check it with:

```bash
systemctl status cinematica-sendspin
journalctl -u cinematica-sendspin -n 50
```

If discovery does not find the player, set its WebSocket URL in
`/opt/cinematica/.env`, using the address and port supplied by that player:

```dotenv
SENDSPIN_CLIENT_URL=ws://your-player.lan:8928/sendspin
```

Then restart the bridge:

```bash
sudo systemctl restart cinematica-sendspin
```

This address is a fallback when no player is selected on the TV. It can remain
empty when you use player discovery.

### Adjusting lip sync

During playback, press **▼** to open the audio-delay control, then **◀** or **▶**
to adjust it in 25 ms steps. Positive values make the sound play later. Wait a
couple of seconds between adjustments so queued audio can play out.

The app also applies sync corrections during playback. The displayed offset is an
estimate from playback timing, not a measurement of the sound reaching your seat.

Turn **Network audio** off to return to the TV's audio output. If a player is busy
with another application, check that player's connection status before retrying.

## Optional: wake the TV app from the browser

The server can use Android Debug Bridge (`adb`) to bring Cinematica to the foreground.
Normal playback does not require this.

Enable debugging on your TV, following its manufacturer’s instructions. For TVs
that offer network debugging on port 5555, connect from the server:

```bash
adb connect 192.168.1.50:5555
adb devices
```

Accept the authorisation prompt on the TV. Then run the installer with
`--adb 192.168.1.50:5555`, keeping your existing installation options. Some TVs use
a separate wireless-debugging pairing procedure or a different port.

## Updating

Back up the saved state listed below. Unpack the new server release and run its
installer, using the same directory, service user, host and optional adb settings.
The installer preserves runtime settings and provider state, but restarts services;
update when nothing is playing.

Update the TV app by installing the new APK over the existing app. Use release notes
to check which server and TV versions belong together.

Provider configuration is managed in the browser. Older `.env` provider settings
are not a substitute for installing and activating a provider. Keep the old file
while moving those settings into the appropriate package.

## Where everything lives

These are the defaults; custom installer options can change them.

| Location | Contents |
|---|---|
| `/opt/cinematica/` | Application, browser interface and documentation |
| `/opt/cinematica/.env` | Local settings, including the optional audio-player URL |
| `/opt/cinematica/netprofile.json` | Saved connection measurements |
| `/opt/cinematica/nowplaying.json` | Saved playback information |
| `/opt/cinematica/sendspin_identity.json` | The bridge's private identity |
| `/opt/cinematica/sendspin_pairing.json` | Audio-player pairing information |
| `/opt/cinematica/stremio/` | Torrent cache and streaming-server settings |
| `/opt/cinematica/transcode/` | Generated audio and converted media |
| `/var/lib/cinematica/` | Installed providers, configuration, credentials and admin account |
| `/var/lib/cinematica-sendspin/` | Bridge Python runtime and dependencies |
| `/etc/systemd/system/cinematica.service` | Main server service |
| `/etc/systemd/system/cinematica-sendspin.service` | Audio bridge service |

Back up `/var/lib/cinematica`, local settings and audio identity/pairing files.
Keep backups private: they contain credentials and keys. Restore pairing files to
the same installation; do not use one identity on several servers. Generated media
and torrent caches do not need to be backed up.

## Troubleshooting

Start with the server status and recent log:

```bash
curl http://localhost:8090/api/health
journalctl -u cinematica -n 100
```

| Problem | What to check |
|---|---|
| Empty library | In browser settings, configure and assign the required provider roles. The health response's `providers.message` explains missing configuration. |
| Library works, playback fails | Test the stream provider and check that it accepts the catalogue's title IDs. See the provider guide. |
| TV cannot connect | Check the server address, port 8090, firewall and whether both devices can reach each other. |
| No TV app connected | Open Cinematica on the TV. This message is normal while the app is closed. |
| No compatible source in browser | The candidate's video codec cannot be decoded by that browser. See [Play in the browser](#play-in-the-browser) for the HEVC limits this usually comes from; try another browser or the TV app. |
| "Another device is playing" | Only one device, the TV or a browser, plays at a time. Stop playback on the other device first. |
| Streaming container fails | Run `docker logs stremio-server` and check `http://localhost:11470/stats.json`. An empty JSON object is normal while idle. |
| Port already in use | Run `ss -ltnp` on the server to identify the service using port 8090 or 11470. |
| Network audio fails | Check the bridge log, selected player and player address. Turn network audio off to use the TV output. |
| Root-owned conversion files | Expected: the container writes these files through a shared directory. |

Connection measurements and available sources affect stream selection. Use the
network check in the interface to reassess the connection after changing networks.

The server converts audio, not video: to AC-3 for the TV, or to AAC for a
browser. A stream still needs a video codec the target device — the TV, or that
specific browser — can decode. Codec selection defaults and advanced settings
are described in the [server reference](README.md#configuration).
