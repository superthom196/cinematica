"""The conversion regulator's lead, and how a resume moves it.

Rules under test:
- an ordinary play is paced against a playhead that starts at 0:00;
- a resume is paced against one that starts at the resume point, so the
  conversion is never held short of where the TV is waiting to seek;
- the bitrate fallback (no measured duration yet) follows the same rule;
- the job's start_s is what transcode_begin hands the regulator.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server  # noqa: E402,F401
import config  # noqa: E402
import jobs  # noqa: E402
import transcode  # noqa: E402


class EstimateLead(unittest.TestCase):
    def test_ordinary_play_is_paced_from_zero(self):
        # 400s converted, 120s played: 280s ahead.
        self.assertEqual(transcode.estimate_lead(120, 0, dur=400), 280)

    def test_measurement_age_counts_as_converted(self):
        self.assertEqual(transcode.estimate_lead(120, 0, dur=400, dur_age=5), 285)

    def test_resume_is_paced_from_the_resume_point(self):
        # The Dark Knight, resumed at 7:00, three minutes into the TV's wait.
        # Paced from zero the conversion was 180s "ahead" at 6:00 and suspended
        # there for good, so the TV never saw 7:10 converted and gave up.
        from_zero = transcode.estimate_lead(180, 0, dur=360)
        self.assertGreaterEqual(from_zero, config.TC_LEAD)
        resumed = transcode.estimate_lead(180, 420, dur=360)
        self.assertLess(resumed, 0)
        self.assertLess(resumed, config.TC_LEAD * config.TC_BAND)

    def test_resume_still_suspends_once_well_past_the_resume_point(self):
        lead = transcode.estimate_lead(60, 420, dur=420 + 60 + config.TC_LEAD + 1)
        self.assertGreater(lead, config.TC_LEAD)

    def test_bitrate_fallback_follows_the_resume_point(self):
        bps = 1_000_000
        self.assertEqual(transcode.estimate_lead(100, 0, size=300 * bps, bytes_per_sec=bps), 200)
        self.assertEqual(transcode.estimate_lead(100, 420, size=300 * bps, bytes_per_sec=bps), -220)


class BeginHandsOverStartS(unittest.TestCase):
    def _begin(self, start_s):
        mid = "lead-test"
        jobs.job_set(mid, start_s=start_s)
        threads = []

        class FakeThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                threads.append((target, args, kwargs or {}))

            def start(self):
                pass

        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(transcode, "tc_cleanup"), \
                mock.patch.object(transcode, "_kill_ctr"), \
                mock.patch.object(transcode, "transcode_start", return_value=proc), \
                mock.patch.object(transcode.os.path, "exists", return_value=True), \
                mock.patch.object(transcode.os.path, "getsize", return_value=1 << 40), \
                mock.patch.object(transcode.os, "remove"), \
                mock.patch.object(transcode.os, "makedirs"), \
                mock.patch.object(transcode.threading, "Thread", FakeThread):
            transcode.transcode_begin("a" * 40, 0, "http://src", 1_000_000, mid)
        self.assertEqual(len(threads), 1)
        target, _, kwargs = threads[0]
        self.assertIs(target, transcode.regulate_lead)
        return kwargs["start_s"]

    def test_resume_offset_reaches_the_regulator(self):
        self.assertEqual(self._begin(420.0), 420.0)

    def test_no_resume_is_zero(self):
        self.assertEqual(self._begin(None), 0)


if __name__ == "__main__":
    unittest.main()
