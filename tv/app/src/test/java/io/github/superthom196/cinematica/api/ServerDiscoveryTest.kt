package io.github.superthom196.cinematica.api

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Covers only the pure helpers in ServerDiscovery.kt: parseHealth, candidateAddresses and the
 * saved/name/scan ordering. Everything else in that file (the flow, the subnet sweep, NSD) needs
 * a real Context/socket and is exercised by hand against an actual server instead.
 */
class ServerDiscoveryTest {

    private val healthJson = """
        {
          "providers": {"configured": true},
          "movies": 4200,
          "imdb_titles": 900000,
          "tv": true,
          "tv_msg": "connected",
          "sustain_mbps": 42.5,
          "net_source": "live"
        }
    """.trimIndent()

    @Test
    fun `parseHealth accepts a real health body`() {
        val server = parseHealth(
            host = "cinematica.lan:8090",
            baseUrl = "http://cinematica.lan:8090",
            via = "name",
            body = healthJson,
        )

        assertEquals(
            FoundServer(
                host = "cinematica.lan:8090",
                baseUrl = "http://cinematica.lan:8090",
                movies = 4200,
                imdbTitles = 900000,
                tvMsg = "connected",
                via = "name",
            ),
            server,
        )
    }

    @Test
    fun `parseHealth rejects a body that is not JSON`() {
        val server = parseHealth(
            host = "192.168.0.5:8090",
            baseUrl = "http://192.168.0.5:8090",
            via = "scan",
            body = "<html>not json at all</html>",
        )
        assertNull(server)
    }

    @Test
    fun `parseHealth rejects JSON that has no providers key`() {
        // Some other HTTP service on the LAN (a printer, a router UI) can easily answer with a
        // plausible-looking JSON object; without the providers key it must not be mistaken for a
        // server.
        val server = parseHealth(
            host = "192.168.0.9:8090",
            baseUrl = "http://192.168.0.9:8090",
            via = "scan",
            body = """{"status": "ok"}""",
        )
        assertNull(server)
    }

    @Test
    fun `candidateAddresses covers a slash-24 minus network, broadcast and self`() {
        val addresses = candidateAddresses("192.168.0.150")

        assertEquals(253, addresses.size)
        assertTrue("192.168.0.0" !in addresses)
        assertTrue("192.168.0.255" !in addresses)
        assertTrue("192.168.0.150" !in addresses)
        assertTrue("192.168.0.1" in addresses)
        assertTrue("192.168.0.254" in addresses)
    }

    @Test
    fun `candidateAddresses accepts an explicit prefix override`() {
        val addresses = candidateAddresses(ownIpv4 = "10.0.0.5", prefix = "10.0.1")

        assertEquals(254, addresses.size) // "self" (10.0.0.5) isn't in this /24, so nothing is filtered out
        assertTrue(addresses.all { it.startsWith("10.0.1.") })
    }

    @Test
    fun `sortFoundServers orders saved before name before scan, then by IP`() {
        val saved = FoundServer("192.168.0.20:8090", "http://192.168.0.20:8090", null, null, null, "saved")
        val nameHost = FoundServer("cinematica.lan:8090", "http://cinematica.lan:8090", null, null, null, "name")
        val scanHigh = FoundServer("192.168.0.200:8090", "http://192.168.0.200:8090", null, null, null, "scan")
        val scanLow = FoundServer("192.168.0.10:8090", "http://192.168.0.10:8090", null, null, null, "scan")

        // Deliberately shuffled input: the function, not insertion order, must produce the result.
        val sorted = sortFoundServers(listOf(scanHigh, nameHost, scanLow, saved))

        assertEquals(listOf(saved, nameHost, scanLow, scanHigh), sorted)
    }

    @Test
    fun `sortFoundServers sorts numerically by IP, not lexicographically`() {
        val dotTwo = FoundServer("192.168.0.2:8090", "http://192.168.0.2:8090", null, null, null, "scan")
        val dotTen = FoundServer("192.168.0.10:8090", "http://192.168.0.10:8090", null, null, null, "scan")

        val sorted = sortFoundServers(listOf(dotTen, dotTwo))

        assertEquals(listOf(dotTwo, dotTen), sorted)
    }

    // One Pi answers to three different host strings, and listing it three times is the bug these
    // two cover: the key is the resolved address, and the friendliest way of reaching it wins.

    @Test
    fun `serverKey keys on the resolved address, whatever name reached it`() {
        val ip = "192.168.0.150"

        assertEquals("192.168.0.150:8090", serverKey("cinematica.lan:8090", ip))
        assertEquals("192.168.0.150:8090", serverKey("cinematica:8090", ip))
        assertEquals("192.168.0.150:8090", serverKey("192.168.0.150:8090", ip))
        // A host with no port at all is the default port, the same way normaliseBaseUrl reads it.
        assertEquals("192.168.0.150:8090", serverKey("cinematica.lan", ip))
        // A second server on another port is a different server, not a collision.
        assertEquals("192.168.0.150:9000", serverKey("cinematica.lan:9000", ip))
    }

    @Test
    fun `preferredServer keeps the friendliest label for one machine`() {
        val saved = FoundServer("cinematica.lan:8090", "http://cinematica.lan:8090", 90, null, null, "saved")
        val named = FoundServer("cinematica:8090", "http://cinematica:8090", 90, null, null, "name")
        val scanned = FoundServer("192.168.0.150:8090", "http://192.168.0.150:8090", 90, null, null, "scan")

        // Whichever order the three probes land in, the saved host is the one left standing.
        assertEquals(saved, preferredServer(saved, named))
        assertEquals(saved, preferredServer(saved, scanned))
        assertEquals(saved, preferredServer(named, saved))
        assertEquals(named, preferredServer(scanned, named))
        assertEquals(named, preferredServer(named, scanned))

        // Equal rank: the newer probe wins, so a re-probe refreshes the film count in place.
        val refreshed = saved.copy(movies = 91)
        assertEquals(refreshed, preferredServer(saved, refreshed))
    }
}
