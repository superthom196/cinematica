# Verifying browser playback

A checklist for the first install. Everything here needs the real server, real
media and a real browser, which is exactly why none of it is covered by the
automated tests.

The automated suite (`python3 -m unittest discover -s server/tests -t server`)
covers candidate selection, capability matching, playlist generation, timeline
stamping, byte-range delivery, ownership arbitration and the release package's
contents. It does not and cannot cover real decoding in a real browser.

Most of it builds its fMP4 boxes by hand, which is right for testing the box
patcher and was wrong for testing the packager: three bugs lived in the serving
path under a fully green suite, because a hand-built fixture cannot have a
keyframe, cannot count its own clock forward, and does not need an init.mp4 to
have been parsed. `server/tests/test_bx_ffmpeg.py` closes that gap by running
the real `segment_cmd` argv against a real ffmpeg and serving the result over a
real socket. It needs `ffmpeg` and `ffprobe` on `PATH` (or named by
`CINEMATICA_TEST_FFMPEG` / `CINEMATICA_TEST_FFPROBE`) and skips itself without
them; CI installs them and fails if those tests skip.

## What could not be checked before install

ffmpeg and ffprobe live inside the streaming container, not on the machine this
was written on, so these paths have never been run against real media:

- repackaging a file whose video the browser accepts but whose container it does not;
- converting an audio track to AAC while copying the video;
- real-world seek behaviour in a browser, across a film large enough for the
  cache-retention window to matter.

Serving real HLS segments and stamping them onto the film's timeline is no
longer on that list: `test_bx_ffmpeg.py` covers it against real ffmpeg output,
including a source whose keyframe interval deliberately does not divide the
segment length.

Direct playback — the case where the browser plays the source untouched — is
covered end to end by the automated tests, including byte-range seeking.

## One thing the grid still approximates

`index.m3u8` advertises a fixed grid: segment k starts at k × seg. A copied
video stream can only be cut on a keyframe, so a run that seeks begins at the
keyframe *before* its grid point — up to one GOP of the film before the point
the viewer asked for. The media timeline is exact and gapless (every segment of
a run is shifted by that run's real start, so what the player decodes is
contiguous and correctly placed), but that pre-roll has nowhere to go in a
playlist whose slots are already spoken for.

Measured in Chromium, on the 2.52s-GOP test clip, seeking to 40s: hls.js pins
a VOD fragment to the position the playlist gives it, so the 2.2s of pre-roll
is placed *after* 40s rather than before it, and everything downstream shifts
by that much. Playback is continuous and complete to the last frame — the
buffered range came back as a single span with no holes and no errors — but
the reported duration ran to 62.2s for a 60s film.

So after a seek, expect the scrubber's total to drift by up to one GOP. It
does not accumulate across further seeks (each run is re-anchored), it never
loses content, and it cannot happen at all on an unseeked play, where the same
check gave an exact 0–60s. Eliminating it means re-encoding the first GOP of
each run so the run can start precisely on the grid point, which this version
deliberately does not do — `-c:v copy` is unconditional.

Report it with the film's GOP (`ffprobe -select_streams v:0 -skip_frame nokey
-show_entries frame=pts_time`) rather than as a stall.

## The last segment after a seek

A seeked run has more film left than the grid budgeted slots for, so it writes
files numbered past the end of the playlist. The final slot therefore serves
its own file *and* every file after it, as one segment carrying several
fragments. Without that the film ends early and silently — the player simply
runs out of playlist. Checked in Chromium: the concatenated segment is
accepted, and playback reaches `ended`.

This is also why a backward seek re-prepares rather than resuming instantly:
the packager clears the previous run's segments when it repositions, because
those bytes were stamped against a different anchor.

## Test media

Four files, one per row of the decision table. Blender's open movies are
convenient and freely redistributable.

| Case | File | Expect |
|---|---|---|
| Direct | MP4, H.264 + AAC | plays immediately, no conversion |
| Repackage | the same content in MKV | brief "Preparing", video never re-encoded |
| Audio convert | MKV, H.264 + AC-3 or FLAC | "Converting audio", picture unchanged |
| Unsupported video | AV1 or HEVC, opened in Firefox | a message naming the codec, not a spinner |

Serve them from any HTTP server the Pi can reach and configure that as the
stream source, so no torrent is involved and results are repeatable.

## The checklist

Playback, with no Android TV connected at all:

- [ ] A film starts from the Play button and reaches picture and sound.
- [ ] A complete film plays to the end without stalling.
- [ ] The scrubber shows the film's real runtime, not the prepared portion.
- [ ] Seek forward into a part not yet prepared; playback resumes there.
- [ ] Seek backward, including behind the retained window. A backward seek
      always re-cuts from the new anchor — the packager clears the old run's
      segments when it repositions, because those bytes were stamped against a
      different anchor — so expect a short re-prepare, not an instant resume.
- [ ] Scrub rapidly back and forth; it settles rather than compounding.
- [ ] Pause for several minutes, then resume. Playback must survive it.
- [ ] Switch to another tab or another app for a minute, then come back.
      Playback must still be running. The page used to beacon a Stop the
      moment the tab went hidden, which ended the session server-side and
      restored nothing on the way back, so this reads as the film simply
      stopping.
- [ ] Leave it hidden for five minutes and come back. A hidden tab's timers
      are throttled to about one a minute, so this is the case where the
      heartbeat is closest to the server's `BX_IDLE` window.
- [ ] Stop, then start a different film.
- [ ] Play the last few minutes of an episode through to the end: the picture
      must reach the real end of the episode, not stop short of it.

Autoplay of the next episode (browser). The TV does this from its own
heartbeat; the browser is offered the next episode with the media and decides
for itself, so this needs checking separately:

- [ ] At the end of an episode, a "Next: SxxEyy" panel appears with a
      countdown, and the next episode starts when it runs out.
- [ ] Cancel stops it, and it does not reappear for that episode.
- [ ] "Play now" starts the next episode immediately.
- [ ] The last episode of a season rolls into the next season's first.
- [ ] The last episode of a show offers nothing at all.
- [ ] A film never offers anything.
- [ ] Closing the tab during the countdown starts nothing.
- [ ] With `AUTOPLAY_NEXT=0` set on the server, nothing is ever offered —
      in the browser or on the TV.
- [ ] Close the tab mid-film, then check `server/transcode/` has no orphan
      `bx_*` directory and `docker exec stremio-server ps` shows no stray ffmpeg.
- [ ] Close the tab during preparation, before playback starts, and confirm the
      TV can then play — the browser must not hold the player.
- [ ] Close the overlay in the first second, before "Finding a source…" turns
      into a percentage. That is the window where the session token exists
      only inside a reply still in flight, so nothing on the page holds it:
      the TV must still be able to play straight afterwards, and
      `server/transcode/` must be left with no `bx_*` directory.

Over the tailnet, from outside the house:

- [ ] The same film plays through the tailnet address.
- [ ] Seeking works there too.
- [ ] Nothing in the browser's network panel points at a `.lan` hostname, at
      port 11470, or at port 8091.

One player at a time:

- [ ] While a browser is playing, starting a film on the TV is refused with
      "Another device is playing".
- [ ] While the TV is playing, starting one in a browser is refused the same way.
- [ ] Stop releases the player to the other device.
- [ ] Two browsers compete the same way; the second is refused, not silently
      handed the stream.
- [ ] Start a film in one browser and, while it is still preparing, start one
      in a second browser. The second is refused — a preparing session has no
      heartbeat yet, and it used to be superseded by anything that asked.
- [ ] When a candidate fails and the error offers Retry, pressing it must
      reach a picture. A retry asks for a second session, so it has to release
      its own first one before asking, or the server refuses it with "Another
      device is playing" about its own playback.

Nothing else regressed:

- [ ] A film plays on the Android TV app exactly as before.
- [ ] Sendspin hi-fi audio still works, still in sync.
- [ ] Provider settings are still reachable and still work.

The release package:

- [ ] `VERSION=x bash server/deploy/package.sh` completes.
- [ ] `tar tzf dist/*.tar.gz | grep -E 'browser_play|hls-'` lists the module,
      the player library and its licence.
- [ ] Installed from that tarball, `/static/hls-<version>.min.js` is served.

## Platforms

Record the four cases against each. Desktop Safari, Chrome and Firefox were the
targets verified during development; the handheld rows need a real device.

| Platform | Direct | Repackage | Audio convert | Unsupported |
|---|---|---|---|---|
| Desktop Chrome | | | | |
| Desktop Firefox | | | | |
| Desktop Safari | | | | |
| iPhone Safari | | | | |
| iPad Safari | | | | |
| Android Chrome | | | | |

Two known results to expect rather than treat as faults. Firefox cannot decode
HEVC, so an HEVC-only film correctly reports that no compatible source was
found. An iPhone older than iOS 17.1 has neither native HLS for our prepared
media nor Media Source, so it is told plainly that the browser cannot play
prepared video; direct sources still play there.

## If something fails

The job's own message is the first place to look — it names the candidate and
the reason. Then `journalctl -u cinematica -f` during a play, which logs the
candidate walk, the packager's segment progress and any timeline drift it
corrected.
