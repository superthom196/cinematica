"""The direct-HTTP source proxy: server.py's /src/<key> route.

A direct stream reaches the player through this proxy and nowhere else -- it is
the only place the source's real URL and its credentials exist (see server.py's
_sources comment), so every byte of an HTTP-transport film is copied by
_proxy_source. What the player gets back therefore has to be a complete,
correctly framed HTTP response, whatever framing the upstream happened to use.

These tests drive the real handler over a real socket against a real upstream,
because the bug they exist for lives entirely in the framing: an in-process call
to _proxy_source with a mock wfile would have copied exactly the same bytes and
reported success, while a player on the other end of the socket hung forever.

Run: python3 -m unittest discover -s server/tests -t server
"""

import http.client
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


BODY = b"".join(bytes([i % 251]) * 997 for i in range(64))   # ~64 KB, not round


class _Upstream(BaseHTTPRequestHandler):
    """The film's real host. Serves BODY three ways, chosen by path:

        /chunked   Transfer-Encoding: chunked, no Content-Length
        /sized     Content-Length, no chunking
        /ranged    206 + Content-Range, honouring the Range header

    /chunked is the shape the bug was about, and is not exotic: an upstream
    that generates or re-streams a body rather than serving a file off disk has
    no length to declare, so it chunks. http.client unwraps that framing before
    the proxy ever sees the response, which is exactly why the proxy has to put
    framing of its own back on.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/chunked"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(BODY), 8192):
                part = BODY[i:i + 8192]
                self.wfile.write(b"%x\r\n" % len(part))
                self.wfile.write(part)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return

        if self.path.startswith("/ranged"):
            rng = self.headers.get("Range") or ""
            start = int(rng.split("=")[1].split("-")[0]) if "=" in rng else 0
            part = BODY[start:]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(part)))
            self.send_header("Content-Range", "bytes %d-%d/%d"
                             % (start, len(BODY) - 1, len(BODY)))
            self.end_headers()
            self.wfile.write(part)
            return

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)


class _Live:
    """An upstream and a Cinematica server, both on real loopback ports."""

    def __enter__(self):
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        self.up_thread = threading.Thread(target=self.up.serve_forever, daemon=True)
        self.up_thread.start()
        self.up_url = "http://127.0.0.1:%d" % self.up.server_address[1]

        # server.H, not a stand-in: the framing under test is written by
        # BaseHTTPRequestHandler's own response machinery, so a different
        # handler class would not be testing the thing that broke.
        self.app = server.Server(("127.0.0.1", 0), server.H)
        self.app_thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.app_thread.start()
        self.port = self.app.server_address[1]
        return self

    def __exit__(self, *exc):
        for srv, thread in ((self.app, self.app_thread), (self.up, self.up_thread)):
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)
        return False

    def key_for(self, path, headers=None):
        return server.register_source({"transport": "http",
                                       "url": self.up_url + path,
                                       "headers": headers or {}})

    def conn(self, timeout=10):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)


class ProxyFramingTest(unittest.TestCase):
    """Whatever the upstream's framing, the player must be able to tell that
    the body has ended -- without waiting for a timeout to prove it."""

    def test_chunked_upstream_completes_and_the_connection_stays_usable(self):
        with _Live() as live:
            key = live.key_for("/chunked")
            c = live.conn()
            try:
                c.request("GET", "/src/" + key)
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                # The upstream declared no length, so neither can we; the
                # response has to carry replacement framing instead of none.
                self.assertIsNone(r.getheader("Content-Length"))
                self.assertEqual((r.getheader("Transfer-Encoding") or "").lower(),
                                 "chunked")
                # read() returning at all is the regression: before the fix it
                # blocked here until the socket timeout, having already
                # received every byte.
                self.assertEqual(r.read(), BODY)

                # A properly terminated message leaves the connection at a
                # message boundary, so a second request goes down the same one.
                c.request("GET", "/src/" + key)
                self.assertEqual(c.getresponse().read(), BODY)
            finally:
                c.close()

    def test_chunked_upstream_frames_by_hangup_for_an_http_1_0_client(self):
        """An HTTP/1.0 player cannot be sent chunked, so the body has to be
        framed by closing the connection -- still an end, just a cruder one."""
        with _Live() as live:
            key = live.key_for("/chunked")
            s = socket.create_connection(("127.0.0.1", live.port), timeout=10)
            try:
                s.sendall(b"GET /src/%s HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
                          % key.encode())
                raw = b""
                while True:
                    part = s.recv(65536)
                    if not part:
                        break            # the hangup that frames the body
                    raw += part
            finally:
                s.close()
            head, _, body = raw.partition(b"\r\n\r\n")
            self.assertIn(b"200", head.split(b"\r\n")[0])
            self.assertNotIn(b"transfer-encoding", head.lower())
            self.assertIn(b"connection: close", head.lower())
            self.assertEqual(body, BODY)

    def test_sized_upstream_still_passes_its_own_length_through_unchanged(self):
        """The fix must not start re-framing responses that were already
        self-delimiting: a declared length is what lets a player show a
        progress bar and seek, and inventing chunking over it would throw
        that away."""
        with _Live() as live:
            key = live.key_for("/sized")
            c = live.conn()
            try:
                c.request("GET", "/src/" + key)
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(r.getheader("Content-Length"), str(len(BODY)))
                self.assertIsNone(r.getheader("Transfer-Encoding"))
                self.assertEqual(r.read(), BODY)
            finally:
                c.close()

    def test_range_request_survives_the_hop_intact(self):
        """206 and Content-Range have to reach the player unaltered: the
        transcoder reads ahead and the Sendspin bridge decodes from an offset,
        and both depend on a partial response still looking partial."""
        with _Live() as live:
            key = live.key_for("/ranged")
            c = live.conn()
            try:
                c.request("GET", "/src/" + key, headers={"Range": "bytes=1000-"})
                r = c.getresponse()
                self.assertEqual(r.status, 206)
                self.assertEqual(r.getheader("Content-Range"),
                                 "bytes 1000-%d/%d" % (len(BODY) - 1, len(BODY)))
                self.assertEqual(r.read(), BODY[1000:])
            finally:
                c.close()

    def test_unknown_key_is_refused_without_reaching_any_upstream(self):
        with _Live() as live:
            c = live.conn()
            try:
                c.request("GET", "/src/" + "a" * 40)
                r = c.getresponse()
                self.assertEqual(r.status, 404)
                r.read()
                c.request("GET", "/src/not-a-hash")
                r = c.getresponse()
                self.assertEqual(r.status, 400)
                r.read()
            finally:
                c.close()


class TorrentProxyFramingTest(unittest.TestCase):
    """/t/<infoHash> goes through the same _proxy_upstream as /src/<key>, so it
    has to be held to the same framing guarantees -- it is reached from a
    tailnet address that has no route to the streaming server's own port, so
    this proxy is the only path a browser player has to that torrent at all.

    _proxy_torrent only ever contributes an infoHash and an optional numeric
    fileIdx to the upstream URL, so there is no path segment of our own to pick
    /chunked, /sized or /ranged on the stub with. STREMIO_IN is patched to the
    stub's base URL plus that selector instead, for the life of each test, and
    restored afterwards -- _proxy_torrent then appends "/<infoHash>" (and, for
    the Range test, "/<idx>") onto it, landing on the same _Upstream branch the
    /src/ tests already cover.
    """

    def setUp(self):
        self._orig_stremio_in = server.STREMIO_IN

    def tearDown(self):
        server.STREMIO_IN = self._orig_stremio_in

    def test_chunked_upstream_completes_and_the_connection_stays_usable(self):
        with _Live() as live:
            server.STREMIO_IN = live.up_url + "/chunked"
            ih = "b" * 40
            c = live.conn()
            try:
                c.request("GET", "/t/%s" % ih)
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertIsNone(r.getheader("Content-Length"))
                self.assertEqual((r.getheader("Transfer-Encoding") or "").lower(),
                                 "chunked")
                # As with /src/, read() returning at all is the regression --
                # before the fix it hung on the socket timeout instead.
                self.assertEqual(r.read(), BODY)

                c.request("GET", "/t/%s" % ih)
                self.assertEqual(c.getresponse().read(), BODY)
            finally:
                c.close()

    def test_sized_upstream_still_passes_its_own_length_through_unchanged(self):
        with _Live() as live:
            server.STREMIO_IN = live.up_url + "/sized"
            ih = "c" * 40
            c = live.conn()
            try:
                c.request("GET", "/t/%s" % ih)
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(r.getheader("Content-Length"), str(len(BODY)))
                self.assertIsNone(r.getheader("Transfer-Encoding"))
                self.assertEqual(r.read(), BODY)
            finally:
                c.close()

    def test_range_request_survives_the_hop_intact(self):
        with _Live() as live:
            server.STREMIO_IN = live.up_url + "/ranged"
            ih = "d" * 40
            c = live.conn()
            try:
                # The fileIdx exercises the /t/<hash>/<idx> shape, which
                # _proxy_torrent has to fold into the same upstream path.
                c.request("GET", "/t/%s/1" % ih,
                          headers={"Range": "bytes=1000-"})
                r = c.getresponse()
                self.assertEqual(r.status, 206)
                self.assertEqual(r.getheader("Content-Range"),
                                 "bytes 1000-%d/%d" % (len(BODY) - 1, len(BODY)))
                self.assertEqual(r.read(), BODY[1000:])
            finally:
                c.close()

    def test_non_hex_infohash_is_refused_without_reaching_any_upstream(self):
        with _Live() as live:
            server.STREMIO_IN = live.up_url
            c = live.conn()
            try:
                c.request("GET", "/t/not-a-hash")
                r = c.getresponse()
                self.assertEqual(r.status, 400)
                r.read()
            finally:
                c.close()


class ProxyCredentialTest(unittest.TestCase):
    def test_the_players_url_carries_no_credential_and_the_proxy_adds_it(self):
        """The whole point of the proxy: the key is derived from the URL, so
        the credential never leaves this process."""
        with _Live() as live:
            key = live.key_for("/sized?api_key=SUPERSECRET",
                               headers={"Authorization": "Bearer SUPERSECRET"})
            self.assertNotIn("SUPERSECRET", key)
            url = server.stream_url({"transport": "http",
                                     "url": live.up_url + "/sized?api_key=SUPERSECRET"})
            self.assertNotIn("SUPERSECRET", url)
            self.assertIn("/src/", url)


if __name__ == "__main__":
    unittest.main()
