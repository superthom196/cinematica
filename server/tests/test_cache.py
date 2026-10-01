"""cache_clear(): what it deletes from Stremio's cache, and what it must not.

Rules under test:
- a plain clear (playback ended) empties everything nothing is streaming;
- the sweep (older_than) only takes torrents nothing has written to for that
  long, so a film still downloading keeps its files;
- a torrent Stremio has open is kept while something is playing, released
  with /remove first when nothing is;
- a docker failure is logged and deletes nothing, and a failed rm is not
  counted as freed.

Run: python3 -m unittest discover -s server/tests -t server
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ENV_FILE", "/nonexistent/.env")
import server  # noqa: E402
import subprocess  # noqa: E402
import urllib.request  # noqa: E402
import torrents  # noqa: E402
import tvlink  # noqa: E402

OLD = "a" * 40      # untouched for hours
NEW = "b" * 40      # written to a minute ago
OPEN = "c" * 40     # old, but Stremio still has an engine on it
ROOT = "/stremio-server/stremio-cache/"


class _R:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class FakeBox:
    """Stands in for `docker exec` and Stremio's HTTP API."""

    def __init__(self, dirs, fresh=(), active=(), list_err=None, rm_fails=()):
        self.dirs, self.fresh, self.active = list(dirs), set(fresh), set(active)
        self.list_err, self.rm_fails = list_err, set(rm_fails)
        self.deleted, self.released = [], []

    def run(self, argv, **kw):
        if argv[-1].startswith("ls "):
            if self.list_err:
                return _R(stderr=self.list_err, returncode=1)
            return _R(stdout="\n".join(self.dirs) + "\n")
        if "find" in argv:
            return _R(stdout="".join(ROOT + h + "\n" + ROOT + h + "/Film.mkv\n"
                                     for h in self.fresh))
        if "rm" in argv:
            h = argv[-1][len(ROOT):]
            if h in self.rm_fails:
                return _R(stderr="Device or resource busy", returncode=1)
            self.deleted.append(h)
            return _R()
        raise AssertionError(argv)

    def urlopen(self, url, timeout=None):
        if url.endswith("/stats.json"):
            body = "{%s}" % ",".join('"%s":{}' % h for h in self.active)
        else:
            self.released.append(url.rsplit("/", 2)[-2])
            body = "{}"
        return io.BytesIO(body.encode())

    def clear(self, playing=False, **kw):
        out = io.StringIO()
        with mock.patch.object(subprocess, "run", self.run), \
             mock.patch.object(urllib.request, "urlopen", self.urlopen), \
             mock.patch.object(tvlink, "playing_now", lambda: playing), \
             redirect_stdout(out):
            torrents.cache_clear(**kw)
        return out.getvalue()


class CacheClearTest(unittest.TestCase):
    def test_playback_end_clears_everything(self):
        box = FakeBox([OLD, NEW, OPEN], fresh=[NEW], active=[OPEN])
        log = box.clear()
        self.assertEqual(sorted(box.deleted), sorted([OLD, NEW, OPEN]))
        self.assertEqual(box.released, [OPEN])
        self.assertIn("cleared 3 torrent(s)", log)
        self.assertNotIn("still", log)

    def test_sweep_keeps_what_is_still_being_written(self):
        box = FakeBox([OLD, NEW, OPEN], fresh=[NEW], active=[OPEN])
        log = box.clear(older_than=4 * 3600)
        self.assertEqual(sorted(box.deleted), sorted([OLD, OPEN]))
        self.assertIn("swept 2 torrent(s)", log)

    def test_sweep_leaves_open_torrents_while_playing(self):
        box = FakeBox([OLD, OPEN], active=[OPEN])
        log = box.clear(playing=True, older_than=4 * 3600)
        self.assertEqual(box.deleted, [OLD])
        self.assertEqual(box.released, [])
        self.assertIn("swept 1 torrent(s), kept 1 still streaming", log)

    def test_docker_failure_is_logged_and_deletes_nothing(self):
        box = FakeBox([OLD], list_err="Error: No such container: stremio-server")
        log = box.clear(older_than=4 * 3600)
        self.assertEqual(box.deleted, [])
        self.assertIn("could not list: Error: No such container", log)

    def test_failed_delete_is_not_counted(self):
        box = FakeBox([OLD, NEW], rm_fails=[OLD])
        log = box.clear()
        self.assertEqual(box.deleted, [NEW])
        self.assertIn("could not delete aaaaaaaa", log)
        self.assertIn("cleared 1 torrent(s)", log)


if __name__ == "__main__":
    unittest.main()
