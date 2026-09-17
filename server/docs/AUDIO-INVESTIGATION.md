# Historical investigation: VLC audio dropouts

This records an investigation on a Sony Bravia on 13–14 September 2026. It concerns
the TV's VLC audio output before the current Sendspin workflow. It is useful
background for debugging, not a setup guide or a guarantee about other hardware.

For current network audio setup, use the
[installation guide](../INSTALL.md#optional-network-audio).

## Reported problem and outcome

The picture played normally, but audio could break up during the first one or two
minutes and occasionally drop out for about two seconds later in a film.

The investigation reported that enabling AC3 passthrough removed the repeated
mid-film dropouts on the tested TV. Follow-up viewing included several films with
different bitrates, up to an 11 GB 4K release.

| Recorded observation | Before passthrough | After passthrough |
|---|---|---|
| Silence insertions | 24 in the measured film | One near startup in the measured playback |
| Duration | 1.73–1.94 seconds | 0.73–0.77 seconds |
| Timing | Throughout playback | Around audio-output initialisation |

These are observations from that investigation, not an automated comparison across
all films or devices.

## What the logs showed

VLC was decoding AC3 to PCM and resampling it. During a dropout, it reported late
audio, flushed buffers, then inserted silence:

```text
audio output: timing screwed (drift: 125435 us): stopping resampling
audio output: playback too late (125236): up-sampling
audio output: playback way too late (180640): flushing buffers
audio output: playback way too early (-1911338): playing silence
audio output: inserting 91744 zeroes
```

At 48 kHz, 91,744 samples represent about 1.91 seconds of silence. This accounts
for the observed dropout duration. The measured film also contained 24 buffer
flushes, 53 late-playback messages and 29 timing-error messages.

The working explanation was unstable timing between VLC and the TV's audio output.
Passthrough bypassed VLC's PCM decoding/resampling path. The post-change log showed
VLC requesting AC3 output: `VLC is looking for: 'a52 ' 48000 Hz 3F2R/LFE`.

Android audio and process-management logs also showed stalls. These were possible
triggers for resynchronisation; they did not establish a single cause for every event.

## Other measurements

| Check | Recorded result |
|---|---|
| First 180 seconds of converted audio | 5,625 AC3 packets; no detected gaps, timestamp anomalies or decode errors |
| Converted audio at 2400–2700 seconds | 9,376 packets; no detected gaps |
| Network ping sample | 100 pings with no packet loss |
| Delivery over a 60-second sample | 530 KB–1.4 MB acknowledged each second; mean 814 KB/s; no stalled seconds |
| Dropout during that delivery sample | Observed despite continuous delivery |
| Socket pacing | Receive-window-limited for 97.5% of the measured period |
| Audio mixer underrun counter | Zero, although VLC was supplying silence |

Those samples supported investigating the TV's audio path. They did not rule out
network or server problems outside the measured windows. A zero mixer-underrun
counter was inconclusive because silence still counts as supplied audio.

The symptom had also been reported on a direct stream without server-side audio
conversion. That was further evidence against conversion being the sole cause.

## Remaining uncertainty

The launch-time log had already been overwritten, so the reason startup was worse
was not established. Possible explanations included initial clock alignment,
audio-buffer setup and the load of starting the app and video decoder together.

For a similar failure, capture logs before playback starts and compare timing
errors, flushes and silence insertions against audible events. Heavy diagnostic
commands can themselves disturb a resource-constrained TV.

The earlier suggestion to switch players through the `PLAYER` setting belonged to
an older external-player setup. Cinematica now uses its own TV app; that suggestion
is not a current troubleshooting procedure.

## Evidence

The retained [VLC log excerpt](audio-evidence-vlc-logcat.txt) contains audio-clock
messages from the investigation. The other measurements above are preserved from
the original notes; this document does not claim they were independently repeated.
