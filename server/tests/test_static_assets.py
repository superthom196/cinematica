"""The vendored front-end assets, and the four places that have to agree
about them.

hls.js is pinned by PATH, not by a package manager: the filename carries
the version, and four separate files name it -- the static route map in
server.py, the lazy <script> load in index.html, the copy list in
deploy/package.sh, and the prose in README.md. Nothing makes them agree,
and three of the four fail silently when they drift: the page asks for a
file the route map does not serve, or the release tarball ships a file
nothing references, and both look fine until someone tries to play
something in a browser that needs hls.js.

Rules under test:
- exactly one vendored hls.js build is present, with its licence beside it;
- the route map, the page and the packager all name that same file;
- the sha256 recorded in server.py's provenance comment is the sha256 of
  the bytes actually sitting in static/ -- the comment is the only record
  of where that bundle came from, so a stale one is worse than none.

Run: python3 -m unittest discover -s server/tests -t server
"""

import hashlib
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import server  # noqa: E402

STATIC = os.path.join(HERE, "static")
REPO = os.path.dirname(HERE)


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


class VendoredHlsTest(unittest.TestCase):
    def setUp(self):
        builds = sorted(fn for fn in os.listdir(STATIC)
                        if re.fullmatch(r"hls-[\d.]+\.min\.js", fn))
        # Two builds present means an upgrade that removed nothing: the old
        # one still ships in the tarball and still sits on installed boxes.
        self.assertEqual(len(builds), 1,
                         "expected exactly one vendored hls.js build, found %r" % builds)
        self.name = builds[0]
        self.version = re.fullmatch(r"hls-([\d.]+)\.min\.js", self.name).group(1)

    def test_licence_ships_beside_the_build(self):
        licence = "hls-%s-LICENSE.txt" % self.version
        self.assertTrue(os.path.isfile(os.path.join(STATIC, licence)),
                        "%s is missing -- the bundle is Apache-2.0 and the "
                        "licence has to travel with it" % licence)

    def test_route_map_serves_exactly_this_build(self):
        route = "/static/" + self.name
        self.assertIn(route, server.STATIC,
                      "server.py does not serve %s" % route)
        served, ctype = server.STATIC[route]
        self.assertEqual(served, self.name)
        self.assertEqual(ctype, "text/javascript")
        # And no OTHER hls build is still routed.
        stale = [p for p in server.STATIC
                 if "hls-" in p and p != route]
        self.assertEqual(stale, [], "stale hls routes still mapped: %r" % stale)

    def test_the_page_asks_for_this_build(self):
        html = _read(os.path.join(HERE, "index.html"))
        asked = re.findall(r"/static/(hls-[\d.]+\.min\.js)", html)
        self.assertEqual(set(asked), {self.name},
                         "index.html asks for %r, static/ has %r" % (asked, self.name))

    def test_the_packager_copies_this_build_and_its_licence(self):
        sh = _read(os.path.join(HERE, "deploy", "package.sh"))
        named = set(re.findall(r"hls-[\d.]+(?:\.min\.js|-LICENSE\.txt)", sh))
        self.assertEqual(named, {self.name, "hls-%s-LICENSE.txt" % self.version},
                         "package.sh copies %r" % sorted(named))

    def test_provenance_hash_matches_the_bytes(self):
        # server.py records the bundle's sha256 in a comment, because a
        # minified blob in a repo with no package manager has no other
        # provenance trail at all. A comment that no longer describes the
        # file is a trail pointing at the wrong place.
        src = _read(os.path.join(HERE, "server.py"))
        recorded = set(re.findall(r"\b([0-9a-f]{64})\b", src))
        with open(os.path.join(STATIC, self.name), "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        self.assertIn(actual, recorded,
                      "no comment in server.py records sha256 %s for %s"
                      % (actual, self.name))

    def test_readme_names_this_build(self):
        readme = _read(os.path.join(HERE, "README.md"))
        named = set(re.findall(r"hls-[\d.]+\.min\.js", readme))
        self.assertEqual(named, {self.name},
                         "README.md names %r" % sorted(named))


if __name__ == "__main__":
    unittest.main()
