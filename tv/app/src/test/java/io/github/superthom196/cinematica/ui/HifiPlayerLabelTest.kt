package io.github.superthom196.cinematica.ui

import io.github.superthom196.cinematica.api.HifiPlayer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class HifiPlayerLabelTest {

    private val loft = HifiPlayer(id = "loft", name = "Loft", url = "ws://192.0.2.10:8928/sendspin")
    private val den = HifiPlayer(id = "den", name = "Den", url = "ws://192.0.2.11:8928/sendspin")

    @Test
    fun `an empty list never shows the saved player's address`() {
        val label = hifiPlayerLabel(loft.url, emptyList())
        assertEquals("No Sendspin players found...", label)
        assertFalse(label.contains("192.0.2.10"))
        assertEquals("No Sendspin players found...", hifiPlayerLabel(null, emptyList()))
    }

    @Test
    fun `a saved player that is advertised shows its name`() {
        assertEquals("Loft", hifiPlayerLabel(loft.url, listOf(den, loft)))
    }

    @Test
    fun `no pick with players around is the server's default`() {
        assertEquals("Server default", hifiPlayerLabel(null, listOf(den)))
    }

    @Test
    fun `a saved player missing from the list is said to be missing, not addressed`() {
        assertEquals("Saved player not found", hifiPlayerLabel(loft.url, listOf(den)))
    }
}
