package io.github.superthom196.cinematica.api

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/**
 * Covers the pure helpers around [CinematicaApi]: base-URL/path/query building, error mapping,
 * and the SSE parsing [CinematicaApi.searchStream] uses (its own body needs a live socket, so
 * that stays exercised by hand). kotlinx.serialization's `Json` runs fine on the plain JVM, so the
 * decode paths are tested with the real [json] instance, not a fake.
 */
class CinematicaApiTest {

    // ---- normaliseBaseUrl ----------------------------------------------------

    @Test
    fun `normaliseBaseUrl adds the default port to a bare host, and keeps an explicit one`() {
        assertEquals("http://cinematica.lan:8090", normaliseBaseUrl("cinematica.lan"))
        assertEquals("http://192.168.1.50:9000", normaliseBaseUrl("192.168.1.50:9000"))
    }

    @Test
    fun `normaliseBaseUrl strips a http or https prefix and a trailing slash`() {
        assertEquals("http://cinematica.lan:8090", normaliseBaseUrl("http://cinematica.lan:8090/"))
        // https is stripped the same as http -- the app only ever talks to the LAN server in plain http.
        assertEquals("http://cinematica.lan:8090", normaliseBaseUrl("https://cinematica.lan:8090"))
    }

    @Test
    fun `normaliseBaseUrl trims whitespace and falls back to localhost when blank`() {
        assertEquals("http://cinematica.lan:8090", normaliseBaseUrl("  cinematica.lan  "))
        assertEquals("http://localhost:8090", normaliseBaseUrl(""))
        assertEquals("http://localhost:8090", normaliseBaseUrl("   "))
    }

    // ---- Throwable.friendly ---------------------------------------------------

    @Test
    fun `friendly maps the well-known network exceptions to short reasons`() {
        assertEquals("no such host", java.net.UnknownHostException().friendly())
        assertEquals("connection refused", java.net.ConnectException().friendly())
        assertEquals("timed out", java.net.SocketTimeoutException().friendly())
        assertEquals("no route to the server", java.net.NoRouteToHostException().friendly())
        assertEquals("TLS error", javax.net.ssl.SSLException("boom").friendly())
        assertEquals("unreadable answer from the server", kotlinx.serialization.SerializationException().friendly())
    }

    @Test
    fun `friendly keeps a plain IOException's own message, or says network error when it has none`() {
        assertEquals("disk full", IOException("disk full").friendly())
        assertEquals("network error", IOException("").friendly())
        assertEquals("network error", IOException().friendly())
    }

    @Test
    fun `friendly falls back to the throwable's own message, or its class name with none`() {
        assertEquals("odd failure", RuntimeException("odd failure").friendly())
        assertEquals("ArithmeticException", ArithmeticException().friendly())
    }

    // ---- encodePathSegment ----------------------------------------------------

    @Test
    fun `encodePathSegment leaves an ordinary id alone and percent-encodes a colon`() {
        assertEquals("tt0903747", encodePathSegment("tt0903747"))
        // A provider-qualified id ("cinemeta:tt0903747") must not read as two path segments.
        assertEquals("cinemeta%3Att0903747", encodePathSegment("cinemeta:tt0903747"))
    }

    @Test
    fun `encodePathSegment encodes a space as percent-20, not URLEncoder's plus`() {
        assertEquals("The%20Matrix", encodePathSegment("The Matrix"))
    }

    // ---- buildMoviesQuery / buildProgressQuery ---------------------------------

    @Test
    fun `buildMoviesQuery omits empty genres and exclude, and leaves out kind=tv for movies`() {
        val q = buildMoviesQuery(0, 30, "top", emptySet(), emptySet())
        assertEquals("/api/movies?offset=0&limit=30&sort=top&bias=1", q)
    }

    @Test
    fun `buildMoviesQuery includes genres and exclude joined with commas, kind=tv, and bias=0`() {
        val q = buildMoviesQuery(30, 30, "new", setOf("28", "12"), setOf("99"), kind = "tv", bias = false)
        assertEquals("/api/movies?offset=30&limit=30&sort=new&genres=28,12&exclude=99&kind=tv&bias=0", q)
    }

    @Test
    fun `buildProgressQuery mirrors buildMoviesQuery's flags without offset or limit`() {
        val q = buildProgressQuery("top", setOf("28"), emptySet(), "tv", true)
        assertEquals("/api/movies/progress?sort=top&genres=28&kind=tv&bias=1", q)
    }

    @Test
    fun `buildMoviesQuery and buildProgressQuery send kind=channel for the channels pool`() {
        assertEquals(
            "/api/movies?offset=0&limit=50&sort=top&kind=channel&bias=1",
            buildMoviesQuery(0, 50, "top", emptySet(), emptySet(), kind = "channel"),
        )
        assertEquals(
            "/api/movies/progress?sort=top&kind=channel&bias=1",
            buildProgressQuery("top", emptySet(), emptySet(), "channel", true),
        )
    }

    // ---- encodeQueryValue -------------------------------------------------------

    @Test
    fun `encodeQueryValue form-encodes a channel id's colon and space, unlike encodePathSegment`() {
        assertEquals("cinemeta%3AUC1234", encodeQueryValue("cinemeta:UC1234"))
        assertEquals("Some+Channel", encodeQueryValue("Some Channel"))
    }

    // ---- decodeSearchEvent ------------------------------------------------------

    @Test
    fun `decodeSearchEvent decodes each known event into its SearchEvent`() {
        assertEquals(CinematicaApi.SearchEvent.Found(SearchFound(12)), decodeSearchEvent("found", """{"found": 12}"""))
        assertEquals(
            CinematicaApi.SearchEvent.Movie(Movie(id = "tt1", title = "Heat")),
            decodeSearchEvent("movie", """{"id": "tt1", "title": "Heat"}"""),
        )
        assertEquals(
            CinematicaApi.SearchEvent.Done(SearchDone(1, 3, 3)),
            decodeSearchEvent("done", """{"playable": 1, "found": 3, "checked": 3}"""),
        )
    }

    @Test
    fun `decodeSearchEvent decodes a fail event's err field, including a missing one`() {
        assertEquals(CinematicaApi.SearchEvent.Fail("provider timed out"), decodeSearchEvent("fail", """{"err": "provider timed out"}"""))
        assertEquals(CinematicaApi.SearchEvent.Fail(null), decodeSearchEvent("fail", "{}"))
    }

    @Test
    fun `decodeSearchEvent returns null for an unknown event name or data that won't parse`() {
        assertNull(decodeSearchEvent("ping", "{}"))
        assertNull(decodeSearchEvent("movie", "not json at all"))
    }

    // ---- parseSseSearchEvents ---------------------------------------------------

    @Test
    fun `parseSseSearchEvents decodes a full stream in order and stops after done`() {
        val body = """
            event:found
            data:{"found": 3}

            event:movie
            data:{"id": "tt1", "title": "Heat"}

            event:done
            data:{"playable": 1, "found": 3, "checked": 3}

        """.trimIndent()
        val events = parseSseSearchEvents(body.lineSequence()).toList()

        assertEquals(
            listOf(
                CinematicaApi.SearchEvent.Found(SearchFound(3)),
                CinematicaApi.SearchEvent.Movie(Movie(id = "tt1", title = "Heat")),
                CinematicaApi.SearchEvent.Done(SearchDone(1, 3, 3)),
            ),
            events,
        )
    }

    @Test
    fun `parseSseSearchEvents stops at a fail event without reading what follows`() {
        val body = """
            event:fail
            data:{"err": "no providers configured"}

            event:movie
            data:{"id": "tt-never-seen"}

        """.trimIndent()
        val events = parseSseSearchEvents(body.lineSequence()).toList()

        assertEquals(listOf(CinematicaApi.SearchEvent.Fail("no providers configured")), events)
    }

    @Test
    fun `parseSseSearchEvents ignores an unknown event and a half-finished one at the end`() {
        val body = """
            event:ping
            data:{}

            event:found
            data:{"found": 1}

            event:movie
        """.trimIndent()
        val events = parseSseSearchEvents(body.lineSequence()).toList()

        assertEquals(listOf(CinematicaApi.SearchEvent.Found(SearchFound(1))), events)
    }

    @Test
    fun `parseSseSearchEvents is lazy, decoding only as many events as are consumed`() {
        var linesPulled = 0
        val lines = sequence {
            val raw = listOf(
                "event:found", """data:{"found": 1}""", "",
                // If the outer sequence were eager rather than lazy, this line would be reached and
                // decodeSearchEvent would blow up on it before .first() ever got its answer.
                "event:movie", "data:not json", "",
            )
            for (line in raw) { linesPulled++; yield(line) }
        }

        val first = parseSseSearchEvents(lines).first()

        assertEquals(CinematicaApi.SearchEvent.Found(SearchFound(1)), first)
        assertEquals(3, linesPulled)
    }
}
