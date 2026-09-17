# Cinematica for Android TV

Browse films and TV shows with your remote and play them in the app. The
[Cinematica server](../server/INSTALL.md) supplies your library and streams.

## Requirements

- Android TV or Google TV running Android 9 or newer.
- Support for 32-bit ARM apps (`armeabi-v7a`), the architecture in the current build.
- A Cinematica server reachable on your home network, with providers configured.

## Install

Get the TV APK from [Releases](https://github.com/superthom196/cinematica/releases).
Copy it to your TV and open it with an installer or file manager. Allow installation
from that app if the TV prompts you.

You can also install from a computer with Android Debug Bridge (`adb`). Enable
network debugging on the TV and authorise the computer, then run:

```bash
adb connect 192.168.1.50:5555
adb install -r /path/to/Cinematica-TV.apk
```

Replace the address, debugging port and APK path with your own. Some TVs require a
wireless-debugging pairing step before connecting.

## Connect to your server

Open Cinematica and select the server it finds, or enter its address, such as
`192.168.1.42:8090`. The app remembers the connection; you can change it in Settings.

If setup is incomplete, open the server's browser address and configure providers.
The [installation guide](../server/INSTALL.md#adding-providers) explains those steps.

## Network audio

To play the soundtrack through your hi-fi, open **Settings**, turn on **Network
audio**, and choose a **Network audio player**. The player must support Sendspin.

During playback, press **▼** for the audio-delay control and **◀** or **▶** to
adjust it. Positive values delay the sound. Allow a couple of seconds for each
change to take effect.

See the [network audio guide](../server/INSTALL.md#optional-network-audio) for
server setup and connection troubleshooting. Turn network audio off to use TV audio.

## PIN lock

Open **Settings → Lock → PIN lock** to set a four-digit PIN. The app asks for it
when opened or resumed. Enter it with the on-screen keypad or the remote's number keys.
Five incorrect attempts pause entry for 30 seconds.

Changing or disabling the PIN requires the current PIN. Playback commands from the
browser are ignored while the app is locked.

If you forget the PIN, clear Cinematica's app data in the TV's system settings.
This also removes the saved server address and other app preferences.

## Updating and troubleshooting

Install the newer APK over the existing app to retain settings. Android requires
updates to use the same signing key. A local build may therefore fail to install
over a published release. Uninstalling first removes app data.

If the server cannot be found, enter its LAN address manually and check that port
8090 is reachable. If the library loads but playback fails, check provider status
and logs on the server.

## Build from source

From `tv/`, with a JDK and Android SDK configured:

```bash
./gradlew assembleRelease
```

The APK is written to `app/build/outputs/apk/release/app-release.apk`.
CI uses JDK 21; the project compiles against Android SDK Platform 37. Point
`JAVA_HOME` and `ANDROID_HOME` at your local installations as needed.

The `build.sh` helper uses Android Studio's macOS JDK path by default. On a matching
setup, it can also install and launch the app:

```bash
./build.sh
TV=192.168.1.50:5555 ./build.sh install
```

Run the unit tests with:

```bash
./gradlew testDebugUnitTest
```

## Signing your builds

Without a release key, local release builds use a debug key. For builds that can
update each other, use the same signing key and retain it securely.

Create `tv/signing.properties` (ignored by git):

```properties
storeFile=/absolute/path/to/cinematica-release.jks
storePassword=your-store-password
keyAlias=cinematica
keyPassword=your-key-password
```

The equivalent environment variables are `CINEMATICA_KEYSTORE_PATH`,
`CINEMATICA_KEYSTORE_PASSWORD`, `CINEMATICA_KEY_ALIAS` and
`CINEMATICA_KEY_PASSWORD`. To create a key:

```bash
keytool -genkeypair -v -keystore ~/cinematica-release.jks -alias cinematica \
  -keyalg RSA -keysize 4096 -validity 10000
```

Licensed under [GPL-3.0](../LICENSE).
