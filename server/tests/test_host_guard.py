"""The DNS-rebinding guard: server.py's _host_ok().

_origin_ok() compares the Origin header against the Host header, which stops an
ordinary forged cross-site POST. It does not stop DNS rebinding. There the
attacker owns a name, points it at this box's private address, and the browser
then treats the page as same-origin: it sends Origin and Host that agree --
both saying the attacker's name -- and sets Sec-Fetch-Site: same-origin. Every
check _origin_ok() makes passes.

What the attack cannot do is address the request to a name this server actually
answers to, so that is what _host_ok() checks, on GET as well as POST. GET
matters because after rebinding the browser will read the response back, so
/src/ and /audio/ hand over the film just as surely as a forged POST starts one.

An IP literal is accepted deliberately: rebinding needs a name whose DNS the
attacker controls, and no page can make a browser put a raw address it does not
own into Host. That is what keeps the LAN address and the tailnet address
working without an allowlist entry for either.

Run: python3 -m unittest discover -s server/tests -t server
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


def host_ok(host, public_host=None, allow=()):
    """_host_ok() against a bare handler -- it reads nothing but headers."""
    h = server.H.__new__(server.H)
    h.headers = {} if host is None else {"Host": host}
    old_public, old_allow = server.PUBLIC_HOST, server.HOST_ALLOW
    if public_host is not None:
        server.PUBLIC_HOST = public_host
    server.HOST_ALLOW = tuple(allow)
    try:
        return server.H._host_ok(h)
    finally:
        server.PUBLIC_HOST, server.HOST_ALLOW = old_public, old_allow


class HostGuard(unittest.TestCase):

    def test_attacker_name_is_refused(self):
        """The whole point: a name we do not answer to, however it is dressed."""
        for host in ("evil.com", "evil.com:8090", "rebind.attacker.example:8090",
                     "EVIL.COM", "cinematica.evil.com"):
            self.assertFalse(host_ok(host), host)

    def test_ip_literals_are_accepted(self):
        """LAN and tailnet addresses, v4 and v6, with and without a port."""
        for host in ("192.168.1.50", "192.168.1.50:8090", "127.0.0.1:8090",
                     "100.101.102.103:8090", "[::1]:8090", "::1", "[fd7a::1]:8090"):
            self.assertTrue(host_ok(host), host)

    def test_names_this_server_answers_to(self):
        for host in ("localhost", "localhost:8090", "mediabox.lan:8090",
                     "mediabox.local", "box.tailnet-abcd.ts.net"):
            self.assertTrue(host_ok(host), host)

    def test_public_host_is_accepted_under_any_case(self):
        self.assertTrue(host_ok("MediaBox:8090", public_host="mediabox"))

    def test_allowlist_adds_a_name(self):
        self.assertFalse(host_ok("films.example.com"))
        self.assertTrue(host_ok("films.example.com", allow=("films.example.com",)))

    def test_missing_host_is_accepted(self):
        """An HTTP/1.0 client omits Host. No browser does, so this is not a hole."""
        self.assertTrue(host_ok(None))
        self.assertTrue(host_ok(""))

    def test_suffix_match_is_not_a_substring_match(self):
        """'evil-lan' and 'notts.net' must not ride the .lan / .ts.net suffixes."""
        for host in ("evil-lan", "badlan", "notts.net", "evil.com.lan.attacker.io"):
            self.assertFalse(host_ok(host), host)


if __name__ == "__main__":
    unittest.main()
