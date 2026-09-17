"""This decides what a given browser can play and how to package it, and it
is kept free of server state so it can be tested directly.

Everything in here is a pure function: no I/O, no subprocess, no globals that
change. That is the entire point of splitting it out of server.py -- these
are the lines worth unit-testing, because the codec-matching logic is where
a wrong guess turns into a black screen rather than a loud error.
"""

import re
import secrets
import struct


# The browser page probes each of these with MediaSource.isTypeSupported()
# (for the video/mp4; codecs="..." forms) and video.canPlayType() (for the
# bare container types), and sends back {"types": {probe_string: bool}}.
# DTS, TrueHD and Matroska are false in every browser today, but they are
# probed anyway -- the point is to keep the matcher data-driven rather than
# prejudiced against a codec, so the day a browser adds support, it just
# starts working here with no code change.
CODEC_PROBES = [
    # H.264
    'video/mp4; codecs="avc1.42E01E"',   # Baseline 3.0
    'video/mp4; codecs="avc1.4D401F"',   # Main 3.1
    'video/mp4; codecs="avc1.640028"',   # High 4.0
    'video/mp4; codecs="avc1.640033"',   # High 5.1
    # HEVC -- both sample-entry forms, since Safari and Chrome disagree on
    # which one they'll answer isTypeSupported() for.
    'video/mp4; codecs="hvc1.1.6.L93.B0"',
    'video/mp4; codecs="hvc1.1.6.L120.B0"',
    'video/mp4; codecs="hvc1.1.6.L150.B0"',
    'video/mp4; codecs="hvc1.2.4.L150.B0"',
    'video/mp4; codecs="hvc1.2.4.L153.B0"',
    'video/mp4; codecs="hev1.1.6.L93.B0"',
    'video/mp4; codecs="hev1.1.6.L120.B0"',
    'video/mp4; codecs="hev1.1.6.L150.B0"',
    'video/mp4; codecs="hev1.2.4.L150.B0"',
    'video/mp4; codecs="hev1.2.4.L153.B0"',
    # AV1
    'video/mp4; codecs="av01.0.08M.08"',
    'video/mp4; codecs="av01.0.13M.10"',
    # VP9
    'video/mp4; codecs="vp09.00.41.08"',
    'video/mp4; codecs="vp09.02.51.10"',
    # Audio
    'video/mp4; codecs="mp4a.40.2"',
    'video/mp4; codecs="mp4a.40.5"',
    'video/mp4; codecs="mp4a.40.34"',
    'video/mp4; codecs="ac-3"',
    'video/mp4; codecs="mp4a.a5"',
    'video/mp4; codecs="ec-3"',
    'video/mp4; codecs="mp4a.a6"',
    'video/mp4; codecs="opus"',
    'video/mp4; codecs="flac"',
    'video/mp4; codecs="dtsc"',
    'video/mp4; codecs="dtse"',
    'video/mp4; codecs="mlpa"',
    # Bare containers -- these are asked about with canPlayType(), not
    # isTypeSupported(), so they are never wrapped in codecs="...".
    "application/vnd.apple.mpegurl",
    "video/mp4",
    "video/x-matroska",
]


# ffprobe profile NAME -> H.264 profile_idc byte. A profile ffprobe reports
# that we don't recognise falls through to rfc6381_video's except clause,
# which returns [] rather than guessing -- see the comment there for why.
_H264_PROFILE_IDC = {
    "Baseline": 0x42,
    "Constrained Baseline": 0x42,
    "Main": 0x4D,
    "High": 0x64,
    "High 10": 0x6E,
    "High 4:2:2": 0x7A,
    "High 4:4:4 Predictive": 0xF4,
}


def rfc6381_video(stream):
    """Candidate RFC 6381 codec strings for an ffprobe video stream dict,
    most specific first. A caller that gets [] back should treat the
    stream as unsupported, not retry with a guess.
    """
    codec = (stream.get("codec_name") or "").lower()
    profile = stream.get("profile")
    level = stream.get("level")
    try:
        if codec == "h264":
            # Constraint flags matter for the black-vs-plays distinction on
            # some Baseline encodes, so they are looked up per profile name
            # rather than always sent as 0x00.
            idc = _H264_PROFILE_IDC[profile]
            if profile == "Constrained Baseline":
                constraint = 0xE0
            elif profile == "Baseline":
                constraint = 0x40
            else:
                constraint = 0x00
            # ffprobe already reports level as level*10 (40 means "4.0"),
            # so it is the level byte with no further scaling.
            return ["avc1.%02X%02X%02X" % (idc, constraint, int(level))]

        if codec in ("hevc", "h265"):
            if profile == "Main":
                profile_digit, compat = 1, 6
            elif profile == "Main 10":
                profile_digit, compat = 2, 4
            else:
                raise KeyError(profile)
            tag = "%d.%d.L%d.B0" % (profile_digit, compat, int(level))
            # Emit both sample-entry spellings, hvc1 first: Safari and some
            # Chromium builds accept only one or the other for a given
            # container, and the caller tries them in order.
            return ["hvc1." + tag, "hev1." + tag]

        if codec == "av01" or codec == "av1":
            pix_fmt = stream.get("pix_fmt") or ""
            depth = "10" if ("10le" in pix_fmt or "10be" in pix_fmt) else "08"
            return ["av01.0.%02dM.%s" % (int(level), depth)]

        if codec == "vp9":
            pix_fmt = stream.get("pix_fmt") or ""
            depth = "10" if ("10le" in pix_fmt or "10be" in pix_fmt) else "08"
            return ["vp09.00.%02d.%s" % (int(level), depth)]
    except (KeyError, TypeError, ValueError):
        # A profile name or level we don't recognise must not crash the
        # request. Falling back to "unsupported" is the safe direction: a
        # wrong codec string produces a black screen with no further
        # attempt, while an empty list just makes the caller move on to
        # the next candidate (remux, or a different track).
        return []

    return []


# ffprobe audio codec_name -> RFC 6381 string(s). AC-3/E-AC-3 get two
# spellings because different browsers answer isTypeSupported() for one or
# the other of the "short" and "full" mp4a.* forms.
_AUDIO_TAGS = {
    "aac": ["mp4a.40.2"],
    "ac3": ["ac-3", "mp4a.a5"],
    "eac3": ["ec-3", "mp4a.a6"],
    "mp3": ["mp4a.40.34"],
    "opus": ["opus"],
    "flac": ["flac"],
    "dts": ["dtsc", "dtse"],
    "truehd": ["mlpa"],
}


def rfc6381_audio(stream):
    """Candidate RFC 6381 codec strings for an ffprobe audio stream dict."""
    codec = (stream.get("codec_name") or "").lower()
    return list(_AUDIO_TAGS.get(codec, []))


# The raw ffprobe "level" integer scales differently per codec: H.264 and
# VP9 report level*10 (40 means "4.0"), but HEVC reports the bitstream's
# general_level_idc, which is level*30 (150 means "5.0", matching the L150
# already baked into the hvc1/hev1 codec strings). Getting this divisor
# wrong doesn't break matching -- rfc6381_video never divides -- it would
# only make a human-facing reason string print the wrong number.
_LEVEL_DIVISOR = {"h264": 10.0, "hevc": 30.0, "h265": 30.0, "vp9": 10.0}


def _level_str(codec, level):
    # Format a raw ffprobe level as the decimal a person expects to read
    # ("L150" for HEVC -> "L5.0"), but don't blow up on a missing level.
    if level is None:
        return "L?"
    try:
        divisor = _LEVEL_DIVISOR.get((codec or "").lower(), 10.0)
        return "L%.1f" % (int(level) / divisor)
    except (TypeError, ValueError):
        return "L?"


def _wrap(codec_string):
    # rfc6381_video/rfc6381_audio return bare codec strings ("avc1.640028"),
    # but the page probes (and caps["types"] is keyed on) the full
    # 'video/mp4; codecs="..."' form from CODEC_PROBES -- so every lookup
    # into caps["types"] has to re-wrap the bare string first, or it will
    # silently never match and everything looks unsupported.
    return 'video/mp4; codecs="%s"' % codec_string


def _unwrap(probe_key):
    # The inverse of _wrap(), for scanning caps["types"] keys back down to
    # a bare codec string. Returns None for a key that isn't a single
    # codecs="..." probe (a bare container type, or something else) --
    # the caller should treat that as "not relevant" rather than parse it.
    prefix, suffix = 'video/mp4; codecs="', '"'
    if probe_key.startswith(prefix) and probe_key.endswith(suffix):
        return probe_key[len(prefix):-len(suffix)]
    return None



def _video_family(codec_string):
    # The bit before the first "." is the family tag (avc1, hvc1, hev1,
    # av01, vp09) that groups the probe strings by codec for the level
    # headroom check below.
    return codec_string.split(".", 1)[0]


def _level_of(codec_string):
    # Pull the numeric level back out of a codec string that has one, for
    # the headroom comparison. Not every family encodes level the same way
    # (avc1 puts it in the last hex byte, hvc1/hev1 spell it "L<n>", av01
    # and vp09 put it right after the second dot), so each family is
    # parsed on its own terms; anything unparseable is dropped rather than
    # guessed.
    try:
        if codec_string.startswith("avc1."):
            # The level is the last hex byte of the tag, and it is already
            # in the same "value * 10" scale ffprobe reports (0x28 -> 40,
            # i.e. "4.0"), so no further scaling is applied here.
            return int(codec_string.rsplit(".", 1)[-1][-2:], 16)
        if codec_string.startswith("hvc1.") or codec_string.startswith("hev1."):
            m = re.search(r"\.L(\d+)\.", codec_string)
            return int(m.group(1)) if m else None
        if codec_string.startswith("av01."):
            return int(codec_string.split(".")[2].rstrip("M"))
        if codec_string.startswith("vp09."):
            return int(codec_string.split(".")[2])
    except (IndexError, ValueError):
        return None
    return None


# Codec string family tag -> the ffprobe-style family name used elsewhere in
# this module. hvc1 and hev1 are two sample-entry spellings of the very same
# HEVC family, so a browser confirming either one confirms both.
_FAMILY_OF_TAG = {
    "avc1": "h264", "hvc1": "hevc", "hev1": "hevc",
    "av01": "av1", "vp09": "vp9",
}


# Which entries of CODEC_PROBES name an AUDIO codec. Every probe is published
# as video/mp4 -- that is the container the browser will actually be handed,
# audio codecs included -- so the page cannot tell the two apart from the mime
# type, and it needs the split to build the video x audio pairing probes
# decide() looks for. Anything whose family tag is not a video family is
# audio, rather than a second hand-written list: _AUDIO_TAGS alone would have
# missed mp4a.40.5, which is probed here but has no ffprobe codec_name of its
# own, and a bare container type (_unwrap -> None) is neither.
AUDIO_PROBES = [p for p in CODEC_PROBES
                if _unwrap(p) and _video_family(_unwrap(p)) not in _FAMILY_OF_TAG]


def _source_family(codec_name):
    # Map an ffprobe codec_name onto the same family names _FAMILY_OF_TAG
    # uses, so a probe string and a source stream can be compared directly.
    codec = (codec_name or "").lower()
    if codec == "h264":
        return "h264"
    if codec in ("hevc", "h265"):
        return "hevc"
    if codec in ("av01", "av1"):
        return "av1"
    if codec == "vp9":
        return "vp9"
    return None


def _source_tier(family, profile):
    # The source's profile tier, ordered so that "higher" always includes
    # "lower" -- except H.264 Main10 is genuinely a separate capability
    # (10-bit decode), so that direction is never crossed downward. AV1
    # and VP9 have no tier concept here, so they always return None and
    # every same-family probe is treated as sufficient.
    if family == "h264":
        return _H264_PROFILE_IDC.get(profile)
    if family == "hevc":
        if profile == "Main":
            return 1
        if profile == "Main 10":
            return 2
        return None
    return None


def _probe_tier(tag, bare):
    # The tier a caps probe string itself confirms, read the same way
    # rfc6381_video encodes it: the idc hex byte for avc1, the profile
    # digit for hvc1/hev1. Anything else has no tier concept.
    try:
        if tag == "avc1":
            return int(bare.split(".", 1)[1][0:2], 16)
        if tag in ("hvc1", "hev1"):
            return int(bare.split(".")[1])
    except (IndexError, ValueError):
        return None
    return None


def video_supported(caps_types, video):
    """(ok, cap_level) -- whether the browser can decode this video stream.

    Matching is by family, profile tier and level rather than by exact codec
    string. CODEC_PROBES samples only a few levels, so an exact-string test
    rejected H.264 High L4.1 and HEVC Main10 L4.1 -- between them most of the
    files anyone actually has. A browser that confirms a profile and level
    also decodes everything below it, which is what this compares.
    """
    family = _source_family(video.get("codec_name"))
    if family is None:
        return False, None

    src_tier = _source_tier(family, video.get("profile"))
    src_level = video.get("level")

    cap_level = None
    for key, true in (caps_types or {}).items():
        if not true:
            continue
        bare = _unwrap(key)
        # A pair probe ("v,a") or a bare container type is not a single
        # video codec claim, so it plays no part in this comparison.
        if bare is None or "," in bare:
            continue
        tag = _video_family(bare)
        if _FAMILY_OF_TAG.get(tag) != family:
            continue
        p_tier = _probe_tier(tag, bare)
        # A probe confirming a lower tier than the source's does not
        # confirm the source -- a browser that only decodes HEVC Main
        # has not thereby proven it can decode Main 10, so that
        # direction stays strict rather than assumed. An unrecognised
        # source profile (src_tier None) can't be judged "covered" by a
        # specific tier either way, so every same-family probe counts.
        if src_tier is not None and p_tier is not None and p_tier < src_tier:
            continue
        p_level = _level_of(bare)
        if p_level is not None and (cap_level is None or p_level > cap_level):
            cap_level = p_level

    if cap_level is None:
        return False, None
    if src_level is None:
        # No level to compare against -- family and tier are enough.
        return True, cap_level
    return cap_level >= int(src_level), cap_level


def pair_string(video_str, audio_str):
    """The 'video/mp4; codecs="v,a"' pairing probe the page also has to
    build, byte for byte, for the "direct" MSE pairing check in decide() --
    named once so both sides are guaranteed to construct the same string.
    """
    return 'video/mp4; codecs="%s,%s"' % (video_str, audio_str)


def decide(caps, probe):
    """Decide how to serve one source to one browser.

    caps is {"mse": bool, "nativeHls": bool, "types": {probe_string: bool}}
    as reported by the page. probe is a normalised media description (see
    module docstring in the calling code for the exact shape). Returns
    {"mode", "reason", "vtag", "acodec", "aidx"} where mode is one of
    "direct", "remux", "audio", "skip".
    """
    types = caps.get("types") or {}
    video = probe.get("video") or {}
    aidx = probe.get("aidx", 0)

    codec = video.get("codec_name", "")
    vtag = "hvc1" if codec.lower() in ("hevc", "h265") else None

    # rfc6381_video still gives the exact string for THIS source's exact
    # level -- still needed below to build the direct-mode pair probe --
    # but it is not how support is decided. CODEC_PROBES only samples a
    # handful of levels (93/120/150/153 for HEVC, a few avc1 levels), so
    # an exact-string match would refuse H.264 High L4.1 and HEVC Main10
    # L4.1, between them most real files. video_supported() instead asks
    # whether the browser has confirmed this family at a sufficient
    # profile tier and level, which is the actual decode question.
    video_strings = rfc6381_video(video)
    video_ok, cap_level = video_supported(types, video)

    if not video_ok:
        src_level = video.get("level")
        if cap_level is not None and src_level is not None and int(src_level) > cap_level:
            reason = "this browser tops out at %s %s and this source is %s" % (
                codec, _level_str(codec, cap_level), _level_str(codec, src_level),
            )
        else:
            profile = video.get("profile", "?")
            reason = "this browser cannot decode %s %s %s" % (
                codec, profile, _level_str(codec, src_level),
            )
        return {
            "mode": "skip", "reason": reason,
            "vtag": vtag, "acodec": None, "aidx": aidx,
        }

    audio_list = probe.get("audio") or []
    try:
        track = audio_list[aidx]
    except (IndexError, TypeError):
        # No audio track at the chosen index is not a crash -- it is
        # video-only playback, which the caller can still offer.
        track = None

    if track is None:
        audio_ok = False
        audio_strings = []
    else:
        audio_strings = rfc6381_audio(track)
        audio_ok = any(types.get(_wrap(s)) for s in audio_strings)

    format_name = (probe.get("format_name") or "").lower()
    is_mp4_family = any(
        tag in format_name for tag in ("mp4", "mov", "m4a", "isom")
    )

    if video_ok and audio_ok and is_mp4_family and audio_strings and video_strings:
        # The page also probes the video+audio pair together, because a
        # platform can decode each codec alone and still refuse the
        # specific pairing within one mp4 track -- that is a property of
        # the codec PAIRING, not of the level. video_supported() above
        # already answers "does the browser decode this level"; the pair
        # probe is asking a different, narrower question on top of that.
        # So this can only be a VETO of an already-confirmed level, not a
        # second requirement for one: CODEC_PROBES samples only a few
        # exact levels, so for most real files (an H.264 High L4.1 MP4,
        # say) the pair string built from this source's own exact level
        # was simply never probed, and types.get() returns the same
        # falsy value for "probed and refused" as for "never asked".
        # Treating an absent key as a refusal sent ordinary MP4s through
        # the ffmpeg remux path for no reason -- real money on a 4-core
        # ARM box that had no need to touch those bytes at all. So the
        # key's presence is checked explicitly, and only an EXPLICIT
        # False blocks direct playback.
        pair = pair_string(video_strings[0], audio_strings[0])
        if not (pair in types and not types[pair]):
            return {
                "mode": "direct", "reason": "",
                "vtag": vtag, "acodec": None, "aidx": aidx,
            }

    if video_ok and audio_ok:
        return {
            "mode": "remux", "reason": "",
            "vtag": vtag, "acodec": "copy", "aidx": aidx,
        }

    # video_ok and not audio_ok
    return {
        "mode": "audio", "reason": "",
        "vtag": vtag, "acodec": "aac", "aidx": aidx,
    }


def grid(duration, gop):
    """Pick a segment length and count for a VOD HLS playlist.

    The segment length is deliberately coarser than the GOP: keyframes
    land at-or-after each grid point, and a grid coarser than the GOP is
    what keeps that "after" close to the grid point rather than drifting
    across a whole extra GOP before the segmenter can cut there.
    """
    try:
        gop = float(gop)
        if gop > 0:
            seg = min(12.0, max(4.0, _ceil(gop * 1.5)))
        else:
            seg = 6.0
    except (TypeError, ValueError):
        seg = 6.0
    n_segs = _ceil((duration or 0) / seg) if seg else 0
    return seg, n_segs


def _ceil(x):
    # Avoid importing math for one call site; ceil via int() is exact for
    # the positive floats this module ever sees.
    i = int(x)
    return i if i == x else i + 1


def seg_start(k, seg):
    """The start time, in seconds, of segment k on the fixed grid."""
    return k * seg


def seg_index(t, seg):
    """The segment index that covers time t on the fixed grid."""
    return int(t // seg)


# A keyframe reported a hair past the grid point (float rounding in the
# probe's csv output) is the one ffmpeg seeks to, not the one before it.
_ANCHOR_EPS = 0.001


def anchor_time(keyframes, k0, seg):
    """The real presentation time an ffmpeg run anchored at segment k0
    actually starts at, given the keyframe times probed around that point.

    `-ss` before `-i` is an input seek, and a stream being copied cannot be
    cut anywhere but a keyframe, so ffmpeg does not start the run at
    k0*seg: it starts at the last keyframe AT OR BEFORE k0*seg and copies
    from there. That real start is the number the server has to add back at
    serve time -- see shift_timeline() -- because ffmpeg then zeroes the
    run's own clock at exactly that keyframe.

    keyframes is whatever the probe managed to read (any order, possibly
    empty). With nothing usable, this falls back to the grid position
    itself: that is the old assumption, wrong by at most one GOP, and it is
    exactly right for k0 == 0, where the run starts at the first keyframe
    of the file and there is nothing before it to fall back from.
    """
    target = float(k0) * seg
    if not keyframes:
        return target
    at_or_before = [t for t in keyframes if t <= target + _ANCHOR_EPS]
    if not at_or_before:
        return target
    return max(at_or_before)


def vod_playlist(duration, seg, init="init.mp4", pattern="s%06d.m4s"):
    """Build a full VOD .m3u8 string covering the whole file from byte 0.

    Returns None when duration is falsy or non-positive -- the caller then
    falls back to a growing (EVENT-style) playlist instead. Because this
    playlist carries ENDLIST from the start, the native scrubber can show
    the real runtime immediately, and the viewer is free to seek into a
    part of the file that has not been segmented yet -- the server fills
    it in on demand when that segment is actually requested.
    """
    if not duration or duration <= 0:
        return None

    n_segs = _ceil(duration / seg)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-TARGETDURATION:%d" % _ceil(seg),
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        '#EXT-X-MAP:URI="%s"' % init,
    ]
    for k in range(n_segs):
        if k < n_segs - 1:
            length = seg
        else:
            # The last segment carries whatever is left over, so the
            # EXTINF values sum to the real duration rather than to
            # n_segs * seg. A remainder of exactly 0 (duration is an exact
            # multiple of seg) means there is no trailing segment to
            # write at all -- n_segs already excludes it because it was
            # computed with a plain ceil, not ceil-plus-one.
            length = duration - seg * (n_segs - 1)
        lines.append("#EXTINF:%.6f," % length)
        lines.append(pattern % k)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def tfdt_of(path, timescale=None):
    """Return the first moof/traf/tfdt baseMediaDecodeTime, in seconds.

    This is the drift monitor: it is how the server checks that a segment
    ffmpeg actually produced starts where the grid said it should, without
    shelling out to ffprobe just to read one number. Pure struct parsing
    of the ISO-BMFF box tree; any truncation or malformed box returns None
    instead of raising, because a monitor that can crash the request path
    is worse than a monitor that occasionally has no answer.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    if timescale is None:
        timescale = _find_timescale(data)
    if not timescale:
        return None

    moof = _find_box(data, b"moof", 0, len(data))
    if moof is None:
        return None
    moof_start, moof_end = moof
    traf = _find_box(data, b"traf", moof_start, moof_end)
    if traf is None:
        return None
    traf_start, traf_end = traf
    tfdt = _find_box(data, b"tfdt", traf_start, traf_end)
    if tfdt is None:
        return None
    body_start, body_end = tfdt

    # tfdt body: 1 byte version, 3 bytes flags, then either a 32-bit or a
    # 64-bit baseMediaDecodeTime depending on that version byte.
    if body_end - body_start < 4:
        return None
    version = data[body_start]
    try:
        if version == 0:
            if body_end - body_start < 8:
                return None
            (base,) = struct.unpack(">I", data[body_start + 4:body_start + 8])
        elif version == 1:
            if body_end - body_start < 12:
                return None
            (base,) = struct.unpack(">Q", data[body_start + 4:body_start + 12])
        else:
            return None
    except struct.error:
        return None

    return base / timescale


def _box_header(data, pos, end):
    """Return (box_type, box_off, box_size, header_size) for the ISO-BMFF
    box starting at pos, or None if its header -- or, for the 64-bit
    "largesize" form, its extended header -- does not fit before end.

    Every box walker in this module goes through this one function: the
    search-only readers below treat None as "stop searching, this box
    isn't here"; the fMP4 timeline patcher further down treats it as a
    hard parse error. Both need size==0 (box runs to end of buffer) and
    size==1 (largesize) handled identically, so there is only one place
    that has to get that right.
    """
    if pos + 8 > end:
        return None
    (size,) = struct.unpack(">I", data[pos:pos + 4])
    box_type = data[pos + 4:pos + 8]
    header_size = 8
    box_size = size
    if size == 1:
        # 64-bit "largesize" form: an 8-byte size follows the type.
        if pos + 16 > end:
            return None
        (box_size,) = struct.unpack(">Q", data[pos + 8:pos + 16])
        header_size = 16
    elif size == 0:
        # Size 0 means "extends to end of file/buffer", used only for a
        # top-level box -- treat the rest of the search range as this
        # box's body.
        box_size = end - pos
    if box_size < header_size or pos + box_size > end:
        return None
    return box_type, pos, box_size, header_size


def _find_box(data, want, start, end):
    """Search [start, end) of an ISO-BMFF byte string for a box whose type
    is `want`, at this nesting level only (does not recurse). Returns
    (body_start, body_end) or None. A box whose declared size runs past
    the buffer, or a size field it can't even read, ends the search
    rather than raising -- the caller treats that the same as "not found".
    """
    pos = start
    while pos < end:
        hdr = _box_header(data, pos, end)
        if hdr is None:
            return None
        box_type, box_off, box_size, header_size = hdr
        if box_type == want:
            return box_off + header_size, box_off + box_size
        pos = box_off + box_size
    return None


def _find_timescale(data):
    """Dig moov/trak/mdia/mdhd out of a full (non-fragmented-only) mp4 and
    return its timescale, or None if there is no moov box at all -- which
    is the normal case for a bare fragment, and the caller is expected to
    pass timescale= explicitly for those.
    """
    moov = _find_box(data, b"moov", 0, len(data))
    if moov is None:
        return None
    moov_start, moov_end = moov
    trak = _find_box(data, b"trak", moov_start, moov_end)
    if trak is None:
        return None
    trak_start, trak_end = trak
    mdia = _find_box(data, b"mdia", trak_start, trak_end)
    if mdia is None:
        return None
    mdia_start, mdia_end = mdia
    mdhd = _find_box(data, b"mdhd", mdia_start, mdia_end)
    if mdhd is None:
        return None
    body_start, body_end = mdhd
    if body_end - body_start < 4:
        return None
    version = data[body_start]
    try:
        if version == 1:
            # version(1) + flags(3) + creation(8) + modification(8), then
            # a 4-byte timescale.
            off = body_start + 4 + 8 + 8
            if off + 4 > body_end:
                return None
            (ts,) = struct.unpack(">I", data[off:off + 4])
        else:
            # version(1) + flags(3) + creation(4) + modification(4), then
            # a 4-byte timescale.
            off = body_start + 4 + 4 + 4
            if off + 4 > body_end:
                return None
            (ts,) = struct.unpack(">I", data[off:off + 4])
    except struct.error:
        return None
    return ts


class TfdtPatchError(Exception):
    """Raised when an fMP4 segment or init file cannot be timeline-patched
    safely: a truncated or malformed box, a version this module doesn't
    know how to widen, a traf with no tfdt (or more than one), a track
    with no delta supplied, an out-of-range patched value, or a tfhd that
    sets base_data_offset (see shift_timeline's docstring for why that one
    matters). Every case is a hard stop rather than a best-effort guess,
    because a segment this function silently gets wrong plays back with
    a timestamp that looks plausible and is not."""


def shift_timeline(data, delta_ticks):
    """Advance every tfdt (baseMediaDecodeTime) in one served fMP4 segment
    by delta_ticks, and every sidx earliest_presentation_time to match, IN
    PLACE -- no box changes size, so every patched field is fixed-width
    and written back at its original offset.

    Why this exists at all: ffmpeg on this server's target host cannot
    write an absolute timeline into a fragmented-MP4 segment -- after a
    seek, the first fragment's tfdt is always 0, on every muxer path this
    was tried against. So the server stamps the real film-clock time onto
    each segment's bytes itself, at serve time, which is what lets one
    session's segments be cut fresh per request while still sharing a
    single init.mp4 across every session regardless of where it started.

    Args:
        data: bytes/bytearray of one served fMP4 segment (styp/sidx/moof/
            mdat, in whatever order the muxer wrote them).
        delta_ticks: either
          - an int, applied to every traf/sidx regardless of track -- only
            correct when every track shares one timescale, since this is
            added directly to each track's own tick count, OR
          - a dict {track_id: delta_ticks_int}, applied per track (a
            traf's track_id comes from its tfhd; a sidx's from its
            reference_ID). A dict is REQUIRED whenever tracks do not
            share a timescale -- video 12288 vs audio 44100 was measured
            on the reference source -- because delta_ticks for a track
            must be anchor_seconds * that track's OWN timescale: applying
            one video-timescale delta to the audio track too was measured
            to land audio at 16.7s when the intended anchor was 60s, an
            A/V desync that looks like a mysterious playback bug rather
            than an obviously wrong number. deltas_for() is the one
            function that is supposed to compute this dict correctly; it
            should never be bypassed by hand.

    Returns:
        (patched_bytes, trafs_patched, sidx_patched)

    Raises:
        TfdtPatchError -- see the class docstring.
    """
    buf = bytearray(data)
    end = len(buf)
    trafs_patched = 0
    sidx_patched = 0

    def delta_for(track_id):
        if isinstance(delta_ticks, dict):
            if track_id not in delta_ticks:
                raise TfdtPatchError(
                    "no delta supplied for track_id=%d" % track_id)
            return delta_ticks[track_id]
        return delta_ticks

    pos = 0
    while pos < end:
        hdr = _box_header(buf, pos, end)
        if hdr is None:
            raise TfdtPatchError("truncated/invalid top-level box at offset %d" % pos)
        box_type, box_off, box_size, header_size = hdr
        content_start = box_off + header_size
        content_end = box_off + box_size

        if box_type == b"moof":
            trafs_patched += _patch_moof(buf, content_start, content_end, delta_for)
        elif box_type == b"sidx":
            sidx_patched += _patch_sidx(buf, content_start, content_end, delta_for)
        # styp, mdat, free, and anything else pass through untouched.

        pos = box_off + box_size

    return bytes(buf), trafs_patched, sidx_patched


def _patch_moof(buf, start, end, delta_for):
    count = 0
    pos = start
    while pos < end:
        hdr = _box_header(buf, pos, end)
        if hdr is None:
            raise TfdtPatchError("truncated/invalid box inside moof at offset %d" % pos)
        box_type, box_off, box_size, header_size = hdr
        if box_type == b"traf":
            count += _patch_traf(buf, box_off + header_size, box_off + box_size, delta_for)
        pos = box_off + box_size
    return count


# tfhd flags bit: base-data-offset-present. When set, sample data offsets
# in this traf are relative to the whole file rather than to this moof, a
# layout this patcher does not handle -- it only ever rewrites tfdt/sidx
# fields, never any data offset. The measured ffmpeg output on this
# server's target host always sets default-base-is-moof and never sets
# this bit, but that is a fact about the muxer's current behaviour, not a
# guarantee, so it is checked on every segment rather than assumed.
_TFHD_BASE_DATA_OFFSET_PRESENT = 0x000001


def _patch_traf(buf, start, end, delta_for):
    # Two-pass: first enumerate this traf's immediate children so tfhd
    # (for track_id and its flags) can be read before any tfdt is touched.
    children = []
    pos = start
    while pos < end:
        hdr = _box_header(buf, pos, end)
        if hdr is None:
            raise TfdtPatchError("truncated/invalid box inside traf at offset %d" % pos)
        box_type, box_off, box_size, header_size = hdr
        children.append((box_type, box_off, box_size, header_size))
        pos = box_off + box_size

    track_id = None
    for box_type, box_off, box_size, header_size in children:
        if box_type == b"tfhd":
            content = box_off + header_size
            (full_word,) = struct.unpack(">I", bytes(buf[content:content + 4]))
            flags = full_word & 0x00FFFFFF
            (track_id,) = struct.unpack(">I", bytes(buf[content + 4:content + 8]))
            if flags & _TFHD_BASE_DATA_OFFSET_PRESENT:
                raise TfdtPatchError(
                    "track %d: tfhd has base_data_offset set (flags=0x%06x) -- "
                    "this patcher only supports default-base-is-moof segments"
                    % (track_id, flags))
            break
    if track_id is None:
        raise TfdtPatchError("traf has no tfhd (cannot determine track_id)")

    delta = delta_for(track_id)
    patched = 0
    for box_type, box_off, box_size, header_size in children:
        if box_type != b"tfdt":
            continue
        _patch_tfdt(buf, box_off + header_size, track_id, delta)
        patched += 1

    if patched == 0:
        raise TfdtPatchError("traf for track %d has no tfdt box" % track_id)
    if patched > 1:
        raise TfdtPatchError(
            "traf for track %d has %d tfdt boxes (expected 1)" % (track_id, patched))
    return patched


def _patch_tfdt(buf, content, track_id, delta):
    # tfdt body: 1 byte version, 3 bytes flags, then a 32-bit (version 0)
    # or 64-bit (version 1) baseMediaDecodeTime -- the same layout tfdt_of
    # reads, but written back in place here instead of only read.
    version = buf[content]
    if version == 1:
        (old,) = struct.unpack(">Q", bytes(buf[content + 4:content + 12]))
        new = old + delta
        if not (0 <= new <= 0xFFFFFFFFFFFFFFFF):
            raise TfdtPatchError(
                "track %d: new tfdt %d out of range for the version-1 "
                "(64-bit) field (old=%d, delta=%d)" % (track_id, new, old, delta))
        buf[content + 4:content + 12] = struct.pack(">Q", new)
    elif version == 0:
        (old,) = struct.unpack(">I", bytes(buf[content + 4:content + 8]))
        new = old + delta
        if not (0 <= new <= 0xFFFFFFFF):
            raise TfdtPatchError(
                "track %d: new tfdt %d exceeds the 32-bit range for a "
                "version-0 field (old=%d, delta=%d) -- refusing to silently "
                "wrap" % (track_id, new, old, delta))
        buf[content + 4:content + 8] = struct.pack(">I", new)
    else:
        raise TfdtPatchError("track %d: unknown tfdt version %d" % (track_id, version))


def _patch_sidx(buf, content_start, content_end, delta_for):
    # Each segment carries one sidx per track, keyed by reference_ID (not
    # track_id in name, but the same value) rather than nesting inside
    # moof/traf the way tfdt does -- so it needs its own delta lookup, but
    # by the same track key, or it would silently contradict the tfdt this
    # function's sibling call already patched.
    p = content_start
    version = buf[p]
    (ref_id,) = struct.unpack(">I", bytes(buf[p + 4:p + 8]))
    delta = delta_for(ref_id)
    # version(1) + flags(3) + reference_ID(4) + timescale(4) = 12 bytes of
    # header before earliest_presentation_time.
    ept_off = p + 12
    if version == 0:
        (old,) = struct.unpack(">I", bytes(buf[ept_off:ept_off + 4]))
        new = old + delta
        if not (0 <= new <= 0xFFFFFFFF):
            raise TfdtPatchError(
                "sidx ref %d: new earliest_presentation_time %d exceeds the "
                "32-bit range (old=%d, delta=%d)" % (ref_id, new, old, delta))
        buf[ept_off:ept_off + 4] = struct.pack(">I", new)
    elif version == 1:
        (old,) = struct.unpack(">Q", bytes(buf[ept_off:ept_off + 8]))
        new = old + delta
        if not (0 <= new <= 0xFFFFFFFFFFFFFFFF):
            raise TfdtPatchError(
                "sidx ref %d: new earliest_presentation_time %d exceeds the "
                "64-bit range (old=%d, delta=%d)" % (ref_id, new, old, delta))
        buf[ept_off:ept_off + 8] = struct.pack(">Q", new)
    else:
        raise TfdtPatchError("sidx ref %d: unknown sidx version %d" % (ref_id, version))
    return 1


def fragments_only(data):
    """`data` from its first `moof` onward -- the fragments, without the
    segment header boxes (styp, sidx) that introduce them.

    Used when more than one segment file has to be served as a single HLS
    segment (see the last-slot tail in server.py's /hls/ route). Appending
    whole files back to back would put a styp and a pair of sidx boxes in
    the middle of a segment; dropping them leaves moof/mdat, moof/mdat,
    which is precisely the "one or more fragments" shape a media segment is
    defined as, and leaves nothing for a strict demuxer to object to.

    Returns b"" if there is no moof at all, so a truncated or unexpected
    file contributes nothing rather than corrupting what it is appended to.
    """
    end = len(data)
    pos = 0
    while pos < end:
        hdr = _box_header(data, pos, end)
        if hdr is None:
            return b""
        box_type, box_off, box_size, header_size = hdr
        if box_type == b"moof":
            return bytes(data[box_off:])
        pos = box_off + box_size
    return b""


def track_timescales(init_bytes):
    """Return {track_id: timescale} read from moov/trak/{tkhd,mdia/mdhd}
    in a full (non-fragment) init.mp4.

    The server calls this once per playback session, against init.mp4,
    and reuses the result for every segment shift_timeline() patches in
    that session -- a bare fMP4 fragment has no moov of its own, so this
    information has to come from the init segment instead, before the
    first fragment is ever patched.
    """
    end = len(init_bytes)
    result = {}
    pos = 0
    while pos < end:
        hdr = _box_header(init_bytes, pos, end)
        if hdr is None:
            raise TfdtPatchError("truncated/invalid top-level box at offset %d" % pos)
        box_type, box_off, box_size, header_size = hdr
        if box_type == b"moov":
            result.update(
                _timescales_in_moov(init_bytes, box_off + header_size, box_off + box_size))
        pos = box_off + box_size
    return result


def _timescales_in_moov(data, start, end):
    result = {}
    pos = start
    while pos < end:
        hdr = _box_header(data, pos, end)
        if hdr is None:
            raise TfdtPatchError("truncated/invalid box inside moov at offset %d" % pos)
        box_type, box_off, box_size, header_size = hdr
        if box_type == b"trak":
            track_id, timescale = _track_id_and_timescale(
                data, box_off + header_size, box_off + box_size)
            if track_id is not None and timescale is not None:
                result[track_id] = timescale
        pos = box_off + box_size
    return result


def _track_id_and_timescale(data, start, end):
    # tkhd is a direct child of trak; mdhd is one level deeper, under
    # trak/mdia -- so this walks trak's own children and, on reaching
    # mdia, hands off to a second walk one level in.
    track_id = None
    timescale = None
    pos = start
    while pos < end:
        hdr = _box_header(data, pos, end)
        if hdr is None:
            raise TfdtPatchError("truncated/invalid box inside trak at offset %d" % pos)
        box_type, box_off, box_size, header_size = hdr
        content = box_off + header_size
        if box_type == b"tkhd":
            track_id = _tkhd_track_id(data, content)
        elif box_type == b"mdia":
            timescale = _mdhd_timescale_in_mdia(data, content, box_off + box_size)
        pos = box_off + box_size
    return track_id, timescale


def _tkhd_track_id(data, content):
    version = data[content]
    # version(1) + flags(3) + creation_time + modification_time, then a
    # 4-byte track_ID -- the two time fields are 4 bytes each in version 0
    # and 8 bytes each in version 1.
    if version == 1:
        off = content + 4 + 8 + 8
    else:
        off = content + 4 + 4 + 4
    (track_id,) = struct.unpack(">I", bytes(data[off:off + 4]))
    return track_id


def _mdhd_timescale_in_mdia(data, start, end):
    pos = start
    while pos < end:
        hdr = _box_header(data, pos, end)
        if hdr is None:
            raise TfdtPatchError("truncated/invalid box inside mdia at offset %d" % pos)
        box_type, box_off, box_size, header_size = hdr
        if box_type == b"mdhd":
            return _mdhd_timescale(data, box_off + header_size)
        pos = box_off + box_size
    return None


def _mdhd_timescale(data, content):
    version = data[content]
    # Same version-dependent field width as tkhd above: version(1) +
    # flags(3) + creation_time + modification_time, then a 4-byte
    # timescale.
    if version == 1:
        off = content + 4 + 8 + 8
    else:
        off = content + 4 + 4 + 4
    (timescale,) = struct.unpack(">I", bytes(data[off:off + 4]))
    return timescale


def deltas_for(timescales, anchor_s):
    """{track_id: delta_ticks} for shift_timeline(), one entry per track
    in timescales (as returned by track_timescales()), each computed as
    anchor_s * that track's own timescale.

    This is the one calculation that must never be bypassed or inlined by
    hand: skip it and apply one shared delta to every track instead, and
    tracks with different timescales silently desync -- see shift_timeline's
    docstring for the measured 16.7s-instead-of-60s case this guards
    against.
    """
    return {track_id: int(round(anchor_s * ts)) for track_id, ts in timescales.items()}


def new_token():
    """A fresh opaque playback token for a browser session."""
    return secrets.token_hex(16)


RE_TOKEN = re.compile(r"^[0-9a-f]{32}$")


# ffprobe's read interval stops just short of the end timestamp it is
# given, so a keyframe sitting exactly ON that timestamp is not reported.
# The interval therefore asks for a little more than it wants; anchor_time()
# discards whatever comes back past the point it cares about. Half a second
# is far more than the exclusion, and far less than any GOP.
_PROBE_END_MARGIN = 0.5


def keyframe_probe_cmd(src, start, end):
    """The ffprobe argv -- everything after the binary -- that lists the
    keyframe presentation times of a source's first video stream from
    `start` up to and INCLUDING `end`.

    Out here with segment_cmd for the same reason: the caller in server.py
    prepends the docker-exec/binary prefix, and a test can then run this
    against a real ffprobe without a container anywhere in sight.

    It can report a keyframe slightly past `end` -- see _PROBE_END_MARGIN.
    Callers are expected to filter, which anchor_time() does anyway because
    that is its whole job.

    Two ways of writing this interval have already been wrong, both of them
    losing the keyframe at the far end -- the only one that matters here,
    since what this is for is finding the LAST keyframe at or before `end`:

    - "START%+DURATION" reads as "DURATION seconds of content" and is not:
      ffprobe measures it from where its seek ACTUALLY landed, the keyframe
      at or before START. The window finished up to one GOP early. Measured
      on a 2.52s-GOP clip: "10%+30" stopped at 35.28 and hid the keyframe at
      37.80 that ffmpeg really started from, putting that run 2.52s early.
    - "START%END" is exclusive of END, so it hid a keyframe landing exactly
      ON the seek point -- which is not an edge case at all, it is what
      happens whenever the GOP divides the grid. Measured on a 2s-GOP clip:
      seeking to 40s reported 38.0 as the last keyframe while ffmpeg started
      at 40.0, and the film's timeline ended at 58s instead of 60s.
    """
    return ["-rw_timeout", "30000000", "-v", "error",
            "-select_streams", "v:0", "-skip_frame", "nokey",
            "-read_intervals", "%.3f%%%.3f" % (start, end + _PROBE_END_MARGIN),
            "-show_entries", "frame=pts_time", "-of", "csv=p=0",
            "-analyzeduration", "5000000", "-probesize", "5000000", src]


def parse_keyframe_times(stdout):
    """Sorted keyframe times from keyframe_probe_cmd's csv output.

    Lines that are not a finite non-negative number are dropped rather than
    raised on: ffprobe emits "N/A" for a frame with no pts, and one of
    those must not cost the caller the rest of the list.
    """
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            v = float(line)
        except ValueError:
            continue
        if v == v and v not in (float("inf"), float("-inf")) and v >= 0:
            out.append(v)
    return sorted(out)


def segment_cmd(src, out_dir, playlist, k0, seg, plan, aidx):
    """Build the ffmpeg argv for the HLS packager anchored at segment k0,
    everything after the ffmpeg binary itself. The caller (bx_spawn in
    server.py) prepends ["docker", "exec", FFMPEG_CTR, FFMPEG] -- those are
    host/container details this module stays free of, same as the rest of
    browser_play.

    This is pulled out of bx_spawn precisely so a test can assert on the
    argv directly: "-c:v copy" is unconditional and no video encoder is
    ever named, because there is deliberately no video transcoding in this
    version, and nothing else guards that guarantee once the command is
    built inside a function that shells out to docker exec.

    -copyts is deliberately never passed. It looks like the obvious way to
    get absolute timestamps out of ffmpeg, but it would bake the anchor
    offset into init.mp4's moov edit list, so init.mp4 would differ per
    anchor and break the single shared init this design depends on -- and
    it still would not produce absolute timestamps, because ffmpeg zeroes
    the first fragment's tfdt after a seek regardless of -copyts. The
    server stamps the real timeline itself at serve time, in
    shift_timeline().
    """
    cmd = ["-hide_banner", "-loglevel", "error", "-y",
           "-ss", str(k0 * seg), "-i", src,
           "-map", "0:v:0", "-map", "0:a:%d" % aidx, "-c:v", "copy"]
    if plan.get("vtag"):
        cmd += ["-tag:v", plan["vtag"]]
    if plan.get("acodec") == "copy":
        cmd += ["-c:a", "copy"]
    else:
        # Never a video encoder -- -c:v copy is unconditional above -- but
        # audio is transcoded whenever the source track cannot be paired
        # directly, same as decide()'s "remux"/"audio" split above.
        cmd += ["-c:a", "aac", "-b:a", "256k", "-ac", "2", "-ar", "48000"]
    cmd += ["-f", "hls", "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_time", str(seg), "-hls_list_size", "0",
            "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments",
            "-start_number", str(k0),
            "-hls_segment_filename", "%s/s%%06d.m4s" % out_dir,
            playlist]
    return cmd
