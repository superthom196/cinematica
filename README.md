# Cinematica

Cinematica watches films and TV shows on your own network. A server finds and
prepares streams from providers you choose, and plays them either through the
Android TV app or straight in a browser on a phone, tablet or laptop.

Audio can play through your TV or through your hi-fi using a Sendspin player.

[Releases](https://github.com/superthom196/cinematica/releases) ·
[Installation guide](server/INSTALL.md) ·
[Provider compatibility](server/docs/PROVIDERS.md)

## What you need

- **A server:** a computer or Raspberry Pi running 64-bit Debian 12+, Ubuntu 24.04+,
  or Raspberry Pi OS Bookworm+, with systemd. Both x86-64 and arm64 are supported.
- **Something to watch on:** a browser is enough — a phone, tablet or laptop
  plays films directly. For the television, an Android TV or Google TV device
  running Android 9 or newer, with support for 32-bit ARM apps; the current TV
  build contains 32-bit ARM libraries.
- **A home network:** the server and whatever you watch on need to reach each
  other. Ethernet is recommended for high-bitrate video.
- **Providers:** a catalogue to browse and a source of playable streams.

The server needs Python 3.11 or newer and Docker. The installer checks these and
can install missing dependencies. A Sendspin player is optional.

## Get started

### 1. Install the server

Download the server archive and its checksum from
[Releases](https://github.com/superthom196/cinematica/releases). For version 1.2.1:

```bash
sha256sum -c cinematica-server-1.2.1.tar.gz.sha256
tar xzf cinematica-server-1.2.1.tar.gz
cd cinematica-server
sudo ./install.sh
```

Use your server's LAN address when prompted. The installer sets up the services
and prints a browser address and a one-time setup token.

For other installation options, see the [installation guide](server/INSTALL.md).
You can preview the install with `./install.sh --dry-run`.

### 2. Choose your providers

Open the printed address in a browser on your home network. Enter the setup token,
choose an admin password, then add your providers.

A provider supplies one or more parts of the library:

| Role | What it provides |
|---|---|
| Catalogue | The films and shows you browse and search |
| Metadata | Descriptions, artwork and episode information |
| Streams | Sources the app can play |

Paste a compatible Stremio add-on manifest URL, or upload a Python integration
package. Configure it, test the connection, and select which roles it should fill.
You can use a different provider for each role; one provider can also fill several.

Cinematica bundles no service integrations at all. Add a provider with a
compatible Stremio add-on manifest URL, or write a Python integration package
against the documented [provider contract](server/docs/PROVIDERS.md) and upload
it. Nothing here is tied to any particular service.

Add-on compatibility depends on the resources, title identifiers and stream types
it supplies. Check the [provider guide](server/docs/PROVIDERS.md) when choosing a
combination. Python packages run code on your server, so only install packages
from authors you trust.

You can start watching at this point: open a film in the browser you just set
up in, on any device on your network. The TV app below is optional.

### 3. Connect your TV (optional)

Install the Cinematica TV APK on your Android TV or Google TV device. The
[TV app guide](tv/README.md) covers sideloading and building from source.

Open Cinematica and connect to your server. If discovery does not find it, enter
the server's LAN address. Your configured library will then be available on the TV.

## Listen through your hi-fi

With a Sendspin player on your network and the server's audio bridge running, open
**Settings** on the TV, turn on **Network audio**, and choose your player.
The soundtrack plays through the hi-fi while the TV shows the picture.

Playback includes sync correction and an adjustable audio delay. See the
[network audio guide](server/INSTALL.md#optional-network-audio) for setup,
manual player addresses and lip-sync adjustment.

## Current status

Cinematica is an early release. Playback testing has mainly been on the author's
hardware; compatibility across other TVs, providers and network audio players is
still being established.

- Each provider role has one active provider at a time.
- Supported sources are torrents and direct HTTP streams. Not every Stremio add-on
  or stream format is supported.
- Subtitle-provider integration is not yet supported.
- The server converts audio, but does not transcode video to make an unsupported
  video codec playable on your TV.

If something fails, [open an issue](https://github.com/superthom196/cinematica/issues)
with your server OS, TV model, app version and steps to reproduce it. Remove
credentials and private provider URLs from any logs you share.

## Updates and help

To update the server, unpack the new release and run its installer. Install the
new TV APK to update the app.

Before updating, back up `/var/lib/cinematica`, which holds provider configuration,
credentials and the admin account. Also preserve local settings and audio pairing
files under the installation directory. The
[installation guide](server/INSTALL.md#where-everything-lives) lists those locations.

For an empty library, check that your providers are configured and assigned to the
required roles in the browser settings. For playback problems, check the provider's
status and the server logs:

```bash
journalctl -u cinematica -f
```

Network audio has a separate log:

```bash
journalctl -u cinematica-sendspin -f
```

More help: [server troubleshooting](server/INSTALL.md#troubleshooting) ·
[TV app](tv/README.md) · [provider compatibility](server/docs/PROVIDERS.md).

## Development

The [Python server](server/) handles providers, stream selection and audio
processing. The [Android TV app](tv/) provides the interface and playback.

```bash
python3 -m unittest discover -s server/tests -t server
bash server/deploy/package.sh
cd tv && ./build.sh
```

Licensed under [GPL-3.0](LICENSE).
