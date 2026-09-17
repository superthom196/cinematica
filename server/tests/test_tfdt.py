"""browser_play's fMP4 timeline patcher: shift_timeline(), track_timescales()
and deltas_for().

Rules under test:
- shift_timeline() advances every traf's tfdt and every sidx's
  earliest_presentation_time by a per-track (or, when every track shares a
  timescale, uniform) delta, in place, without changing the segment's
  total length or touching any other byte;
- a dict delta is looked up per track (tfhd.track_id for a traf, the sidx's
  reference_ID for a sidx) and a track missing from the dict is a hard
  error rather than a silent skip -- this is the A/V desync guard, because
  two tracks patched by one shared delta only stay in sync when they
  happen to share a timescale;
- an out-of-range patched value (version-0 tfdt overflowing 32 bits) is a
  TfdtPatchError, never a silently wrapped number;
- truncated input, an unknown tfdt/sidx version, and a tfhd with
  base_data_offset set are all hard errors, not a best-effort guess;
- track_timescales() reads {track_id: timescale} out of an init.mp4's
  moov/trak boxes, and deltas_for() turns that plus an anchor time into
  the per-track delta dict shift_timeline() needs.

No ffmpeg, no network, no fixture files -- every byte structure is built
with struct.pack right here.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import browser_play  # noqa: E402


# ---- box builders -----------------------------------------------------

def _box(box_type, body):
    """Pack one ISO-BMFF box: 4-byte size, 4-byte type, then the body."""
    return struct.pack(">I4s", 8 + len(body), box_type) + body


def _box_largesize(box_type, body):
    """Pack one box using the 64-bit "largesize" form (size field == 1,
    followed by an 8-byte real size)."""
    size = 16 + len(body)
    return struct.pack(">I4sQ", 1, box_type, size) + body


def _tfhd_body(track_id, flags=0x020000):
    # byte 0 is version (always 0 for tfhd), bytes 1-3 are flags -- packed
    # together as one big-endian word with the version byte forced to 0,
    # then the 4-byte track_ID. 0x020000 is default-base-is-moof, which is
    # what the measured ffmpeg output on the target host always sets.
    full_word = flags & 0x00FFFFFF
    return struct.pack(">II", full_word, track_id)


def _tfdt_body(version, base):
    if version == 0:
        return b"\x00\x00\x00\x00" + struct.pack(">I", base)
    if version == 1:
        return b"\x01\x00\x00\x00" + struct.pack(">Q", base)
    # An unrecognised version: only the version byte matters to the code
    # under test (it raises before reading anything past it), so the rest
    # is arbitrary filler of a plausible width.
    return bytes([version, 0, 0, 0]) + b"\x00" * 8


def _traf(track_id, tfdt_version, base, tfhd_flags=0x020000):
    tfhd = _box(b"tfhd", _tfhd_body(track_id, tfhd_flags))
    tfdt = _box(b"tfdt", _tfdt_body(tfdt_version, base))
    return _box(b"traf", tfhd + tfdt)


def _moof(*trafs):
    return _box(b"moof", b"".join(trafs))


def _sidx_body(version, ref_id, timescale, ept):
    version_flags = struct.pack(">I", (version & 0xFF) << 24)  # flags = 0
    if version == 0:
        rest = (
            struct.pack(">I", ref_id)
            + struct.pack(">I", timescale)
            + struct.pack(">I", ept)
            + struct.pack(">I", 0)  # first_offset
        )
    else:
        rest = (
            struct.pack(">I", ref_id)
            + struct.pack(">I", timescale)
            + struct.pack(">Q", ept)
            + struct.pack(">Q", 0)  # first_offset
        )
    rest += b"\x00\x00\x00\x00"  # reserved(2) + reference_count(0)
    return version_flags + rest


def _sidx(version, ref_id, timescale, ept):
    return _box(b"sidx", _sidx_body(version, ref_id, timescale, ept))


def _tkhd_body(track_id, version=0):
    version_flags = struct.pack(">I", (version & 0xFF) << 24)
    if version == 1:
        return version_flags + b"\x00" * 8 + b"\x00" * 8 + struct.pack(">I", track_id)
    return version_flags + b"\x00" * 4 + b"\x00" * 4 + struct.pack(">I", track_id)


def _mdhd_body(timescale, version=0):
    version_flags = struct.pack(">I", (version & 0xFF) << 24)
    if version == 1:
        return version_flags + b"\x00" * 8 + b"\x00" * 8 + struct.pack(">I", timescale)
    return version_flags + b"\x00" * 4 + b"\x00" * 4 + struct.pack(">I", timescale)


def _trak(track_id, timescale):
    tkhd = _box(b"tkhd", _tkhd_body(track_id))
    mdhd = _box(b"mdhd", _mdhd_body(timescale))
    mdia = _box(b"mdia", mdhd)
    return _box(b"trak", tkhd + mdia)


def _moov(*traks):
    return _box(b"moov", b"".join(traks))


# ---- test-only readers (independent of browser_play's internals, so the
# patcher and its verification don't share a bug) -----------------------

def _iter_boxes(data, start, end):
    pos = start
    while pos < end:
        size, box_type = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            (size,) = struct.unpack(">Q", data[pos + 8:pos + 16])
            header = 16
        elif size == 0:
            size = end - pos
        yield box_type, pos + header, pos + size
        pos += size


def _read_tfdts(data):
    """[(track_id, version, base), ...] for every traf, in file order."""
    out = []
    end = len(data)
    for box_type, content, box_end in _iter_boxes(data, 0, end):
        if box_type != b"moof":
            continue
        for bt2, c2, e2 in _iter_boxes(data, content, box_end):
            if bt2 != b"traf":
                continue
            track_id = None
            tfdt = None
            for bt3, c3, e3 in _iter_boxes(data, c2, e2):
                if bt3 == b"tfhd":
                    (track_id,) = struct.unpack(">I", data[c3 + 4:c3 + 8])
                elif bt3 == b"tfdt":
                    version = data[c3]
                    if version == 1:
                        (base,) = struct.unpack(">Q", data[c3 + 4:c3 + 12])
                    else:
                        (base,) = struct.unpack(">I", data[c3 + 4:c3 + 8])
                    tfdt = (version, base)
            out.append((track_id, tfdt[0], tfdt[1]))
    return out


def _read_sidx(data):
    """[(ref_id, version, ept), ...] for every sidx, in file order."""
    out = []
    end = len(data)
    for box_type, content, box_end in _iter_boxes(data, 0, end):
        if box_type != b"sidx":
            continue
        version = data[content]
        (ref_id,) = struct.unpack(">I", data[content + 4:content + 8])
        if version == 0:
            (ept,) = struct.unpack(">I", data[content + 12:content + 16])
        else:
            (ept,) = struct.unpack(">Q", data[content + 12:content + 20])
        out.append((ref_id, version, ept))
    return out


class ShiftTimelineBasicTest(unittest.TestCase):
    def test_single_traf_version1_patched_and_length_preserved(self):
        seg = _moof(_traf(1, 1, 90000))
        patched, trafs, sidxs = browser_play.shift_timeline(seg, 500)
        self.assertEqual(trafs, 1)
        self.assertEqual(sidxs, 0)
        self.assertEqual(len(patched), len(seg))
        self.assertEqual(_read_tfdts(patched), [(1, 1, 90500)])

    def test_version0_tfdt(self):
        seg = _moof(_traf(1, 0, 1000))
        patched, trafs, sidxs = browser_play.shift_timeline(seg, 250)
        self.assertEqual(trafs, 1)
        self.assertEqual(_read_tfdts(patched), [(1, 0, 1250)])

    def test_other_bytes_untouched(self):
        # styp before the moof and an mdat after it, both with recognisable
        # filler, must survive byte-for-byte.
        styp = _box(b"styp", b"msdhmsix")
        mdat = _box(b"mdat", b"PAYLOAD-BYTES-UNCHANGED")
        seg = styp + _moof(_traf(1, 1, 1000)) + mdat
        patched, trafs, sidxs = browser_play.shift_timeline(seg, 42)
        self.assertEqual(len(patched), len(seg))
        self.assertEqual(trafs, 1)
        self.assertTrue(patched.startswith(styp))
        self.assertTrue(patched.endswith(mdat))


class TwoTrackDesyncGuardTest(unittest.TestCase):
    def test_dict_delta_shifts_each_track_by_its_own_amount(self):
        # This is the A/V desync guard: video (track 1) and audio (track 2)
        # must each move by their OWN timescale's delta, not a shared one.
        seg = _moof(_traf(1, 1, 1_000_000), _traf(2, 1, 2_000_000))
        patched, trafs, sidxs = browser_play.shift_timeline(
            seg, {1: 737_280, 2: 2_646_000})
        self.assertEqual(trafs, 2)
        self.assertEqual(
            _read_tfdts(patched),
            [(1, 1, 1_737_280), (2, 1, 4_646_000)],
        )

    def test_int_delta_shifts_both_tracks_by_the_same_amount(self):
        # An int delta is documented as applying uniformly to every track
        # regardless of track_id -- correct only when timescales match,
        # but the function must do exactly that, not silently key on
        # track_id the way the dict form does.
        seg = _moof(_traf(1, 1, 1000), _traf(2, 1, 5000))
        patched, trafs, sidxs = browser_play.shift_timeline(seg, 300)
        self.assertEqual(trafs, 2)
        self.assertEqual(_read_tfdts(patched), [(1, 1, 1300), (2, 1, 5300)])

    def test_dict_missing_a_track_raises(self):
        seg = _moof(_traf(1, 1, 1000), _traf(2, 1, 2000))
        with self.assertRaises(browser_play.TfdtPatchError):
            browser_play.shift_timeline(seg, {1: 500})


class SidxTest(unittest.TestCase):
    def test_sidx_version0_and_version1_patched_by_matching_track(self):
        seg = (
            _sidx(0, 1, 44100, 4410)
            + _sidx(1, 2, 12288, 12288)
            + _moof(_traf(1, 1, 4410), _traf(2, 1, 12288))
        )
        patched, trafs, sidxs = browser_play.shift_timeline(
            seg, {1: 100, 2: 200})
        self.assertEqual(trafs, 2)
        self.assertEqual(sidxs, 2)
        self.assertEqual(
            _read_sidx(patched), [(1, 0, 4510), (2, 1, 12488)])
        self.assertEqual(
            _read_tfdts(patched), [(1, 1, 4510), (2, 1, 12488)])


class ErrorCasesTest(unittest.TestCase):
    def test_version0_overflow_raises_not_wraps(self):
        seg = _moof(_traf(1, 0, 0xFFFFFFF0))
        with self.assertRaises(browser_play.TfdtPatchError):
            browser_play.shift_timeline(seg, 1000)

    def test_truncated_input_raises(self):
        seg = _moof(_traf(1, 1, 90000))
        with self.assertRaises(browser_play.TfdtPatchError):
            browser_play.shift_timeline(seg[:-5], 100)

    def test_unknown_tfdt_version_raises(self):
        seg = _moof(_traf(1, 2, 0))
        with self.assertRaises(browser_play.TfdtPatchError):
            browser_play.shift_timeline(seg, 100)

    def test_base_data_offset_present_raises(self):
        # default-base-is-moof (0x020000) plus base-data-offset-present
        # (0x000001): the patcher only supports the former, and must raise
        # rather than silently produce a segment whose sample offsets are
        # now inconsistent with its (correctly) patched tfdt.
        seg = _moof(_traf(1, 1, 1000, tfhd_flags=0x020001))
        with self.assertRaises(browser_play.TfdtPatchError):
            browser_play.shift_timeline(seg, 100)


class LargesizeTest(unittest.TestCase):
    def test_64bit_largesize_box_is_walked_correctly(self):
        mdat = _box_largesize(b"mdat", b"large-box-payload-bytes")
        seg = _moof(_traf(1, 1, 1000)) + mdat
        patched, trafs, sidxs = browser_play.shift_timeline(seg, 50)
        self.assertEqual(trafs, 1)
        self.assertEqual(len(patched), len(seg))
        self.assertTrue(patched.endswith(mdat))
        self.assertEqual(_read_tfdts(patched), [(1, 1, 1050)])


class TrackTimescalesTest(unittest.TestCase):
    def test_two_traks_different_timescales(self):
        init_bytes = _moov(_trak(1, 12288), _trak(2, 44100))
        result = browser_play.track_timescales(init_bytes)
        self.assertEqual(result, {1: 12288, 2: 44100})


class DeltasForTest(unittest.TestCase):
    def test_deltas_for_anchor_60s(self):
        result = browser_play.deltas_for({1: 12288, 2: 44100}, 60.0)
        self.assertEqual(result, {1: 737280, 2: 2646000})


class RoundTripTest(unittest.TestCase):
    def test_shift_then_unshift_reproduces_original_bytes(self):
        seg = (
            _sidx(1, 1, 12288, 12288)
            + _sidx(0, 2, 44100, 4410)
            + _moof(_traf(1, 1, 12288), _traf(2, 0, 4410))
        )
        delta = {1: 737280, 2: 2646000}
        forward, _, _ = browser_play.shift_timeline(seg, delta)
        neg_delta = {track_id: -d for track_id, d in delta.items()}
        back, trafs, sidxs = browser_play.shift_timeline(forward, neg_delta)
        self.assertEqual(trafs, 2)
        self.assertEqual(sidxs, 2)
        self.assertEqual(back, bytes(seg))


if __name__ == "__main__":
    unittest.main()
