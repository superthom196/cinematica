"""ffprobe: what a stream actually contains -- codecs, audio tracks,
languages, keyframes.
"""
import json, math, re, subprocess
import browser_play

import config

# Measured on this TV: AC3/E-AC3/DTS play cleanly, but AAC 5.1 has to be
# software-decoded and resampled by VLC and its audio clock drifts badly --
# "timing screwed", inserted silence, audible break-up while video stays fine.
# YTS releases are both the smallest (so the bitrate budget favours them) and
# AAC 5.1, so the size preference was actively selecting the broken format.
RE_AC3  = re.compile(r"\b(ac3|eac3|e-?ac-?3|ddp|dd\+|dd[0-9]|dolby ?digital|truehd|atmos|dts)", re.I)
RE_AAC  = re.compile(r"\baac", re.I)          # matches AAC, AAC5.1, AAC2.0

def audio_kind(c):
    blob = (c.get("display") or "") + " " + (c.get("tag") or "")
    if RE_AC3.search(blob):
        return "ac3"
    if RE_AAC.search(blob):
        return "aac"
    return "?"

def probe_full(url_internal):
    """Everything ffprobe knows about the file, video and audio alike.

    probe_media() only ever asked about audio, because the TV panel's question
    was always "can this thing decode the sound" -- video went out as-copied to
    a box with a hardware decoder for everything a source is likely to carry.
    A browser
    has no such guarantee: it needs the video codec, its profile and level, and
    the pixel format before it can say whether it can play the file at all, so
    this widens the same ffprobe call by dropping -select_streams a and asking
    about every stream, instead of duplicating the docker-exec/timeout/JSON
    plumbing a second time for a video-only probe.

    Same call shape as before -- -rw_timeout, -v error, -analyzeduration,
    -probesize, -of json, timeout=120 -- so there is exactly one place that
    knows how to reach into the ffmpeg container. Never raises: a probe that
    throws mid-transcode-decision is worse than one that answers "unknown".
    """
    empty = {"format_name": "", "duration": None, "video": None, "audio": [], "langs": []}
    try:
        r = subprocess.run(
            ["docker", "exec", config.FFMPEG_CTR, config.FFPROBE, "-rw_timeout", "30000000", "-v", "error",
             "-show_entries",
             "stream=index,codec_type,codec_name,profile,level,pix_fmt,width,height,channels:"
             "stream_tags=language:format=format_name,duration",
             "-of", "json",
             "-analyzeduration", "5000000", "-probesize", "5000000", url_internal],
            capture_output=True, text=True, timeout=120)
        d = json.loads(r.stdout or "{}")
        streams = d.get("streams") or []
        fmt = d.get("format") or {}
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        langs = [((s.get("tags") or {}).get("language") or "und").strip().lower() or "und"
                 for s in audio]
        try:
            dur = float(fmt.get("duration") or 0) or None
        except (TypeError, ValueError):
            dur = None
        return {"format_name": fmt.get("format_name") or "", "duration": dur,
                "video": video, "audio": audio, "langs": langs}
    except Exception:
        return empty

def probe_media(url_internal):
    """(codec of the first audio track, duration in seconds, audio languages, per-track codecs).

    Duration comes back from the same ffprobe call because it is free here and
    the alternative was expensive: when the catalogue has no runtime for a film, the
    transcode regulator fell back to dividing by a one-minute film and computed
    a bitrate ~100x too high. JSON output rather than the flat format -- with
    two -show_entries sections the line order depends on whether the stream was
    found at all, which is exactly the positional parsing this codebase avoids
    elsewhere.

    ALL audio streams, not a:0: the language question is about the file's set of
    tracks, and one extra -show_entries field costs nothing on a call that is
    already being made. Tracks with no language tag come back as "und".

    Now a thin view over probe_full(): there is only one ffprobe call shape to
    maintain, and this just reshapes its audio list into the tuple callers expect.
    """
    full = probe_full(url_internal)
    audio = full["audio"]
    codec = (audio[0].get("codec_name") or "").strip().lower() or None if audio else None
    codecs = [(s.get("codec_name") or "").strip().lower() or None for s in audio]
    return codec, full["duration"], full["langs"], codecs

def probe_gop(url_internal, window=40):
    """Median gap between keyframes, in seconds, over the first `window` seconds.

    Only the first window seconds: the pre-buffer has already pulled that part
    of the file into the local cache before this is ever called, so reading it
    again costs a couple of seconds of ffprobe time and no extra network
    traffic -- probing the whole file would mean fetching parts nothing else
    needs yet, just to answer a question the first few keyframes already settle.

    The answer picks a segment length. A stream that is being copied rather
    than re-encoded can only be cut on keyframe boundaries, so a segment grid
    finer than the keyframe interval is a promise the copy can't keep -- every
    segment would actually start early or late at the nearest keyframe instead.
    """
    try:
        r = subprocess.run(
            ["docker", "exec", config.FFMPEG_CTR, config.FFPROBE, "-rw_timeout", "30000000", "-v", "error",
             "-select_streams", "v:0", "-skip_frame", "nokey",
             "-read_intervals", "%%+%d" % window,
             "-show_entries", "frame=pts_time", "-of", "csv=p=0",
             "-analyzeduration", "5000000", "-probesize", "5000000", url_internal],
            capture_output=True, text=True, timeout=120)
        times = sorted(float(line) for line in (r.stdout or "").splitlines() if line.strip())
        if len(times) < 2:
            return None
        gaps = sorted(b - a for a, b in zip(times, times[1:]))
        n = len(gaps)
        mid = n // 2
        median = gaps[mid] if n % 2 else (gaps[mid - 1] + gaps[mid]) / 2
        if not math.isfinite(median) or median <= 0:
            return None
        return median
    except Exception:
        return None

def probe_keyframes(url_internal, t, window=30):
    """Keyframe presentation times, in seconds, in a window ENDING at t.

    Same ffprobe shape as probe_gop above, and windowed for the same
    reason: this is called once per packager run, against a source that may
    be a torrent still filling in, and reading the whole file to list every
    keyframe would pull down parts nothing is going to watch yet just to
    answer a question about one point in the film.

    The window ends at t because the only keyframe this has to find is the
    one ffmpeg's input seek will land on -- the last one AT OR BEFORE t. A
    window shorter than the GOP finds nothing, so this asks for `window`
    seconds of lead-in, comfortably more than grid()'s largest segment. The
    end of that window is absolute, not a duration -- see
    keyframe_probe_cmd, where getting that wrong hid the one keyframe this
    whole call exists to find.
    Returns [] on any failure; anchor_time() treats that as "assume the
    grid position", which is what this code did before it asked at all.

    The timeout is short on purpose, and much shorter than probe_gop's. That
    one runs once while the film is being prepared and the viewer is already
    watching a progress message; this one runs inside bx_restart, on the
    request thread of the segment fetch that IS the seek, so every second it
    spends is a second of the viewer staring at a stalled scrubber. Giving
    up costs at most one GOP of anchor accuracy -- worth far less than the
    wait.
    """
    end = float(t)
    start = max(0.0, end - window)
    if end <= start:
        return []
    try:
        r = subprocess.run(
            ["docker", "exec", config.FFMPEG_CTR, config.FFPROBE]
            + browser_play.keyframe_probe_cmd(url_internal, start, end),
            capture_output=True, text=True, timeout=20)
        return browser_play.parse_keyframe_times(r.stdout)
    except Exception:
        return []
