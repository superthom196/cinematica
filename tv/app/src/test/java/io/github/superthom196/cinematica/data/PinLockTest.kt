package io.github.superthom196.cinematica.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PinLockTest {

    @Test
    fun `a record matches the PIN it was made from and nothing else`() {
        val record = PinLock.record("1234")
        assertTrue(PinLock.matches("1234", record))
        assertFalse(PinLock.matches("1235", record))
        assertFalse(PinLock.matches("", record))
        assertFalse(PinLock.matches("12345", record))
    }

    @Test
    fun `the PIN is not in the record and the salt is fresh each time`() {
        val a = PinLock.record("0000")
        val b = PinLock.record("0000")
        assertFalse(a.contains("0000"))
        assertNotEquals(a, b)
        assertTrue(PinLock.matches("0000", a))
        assertTrue(PinLock.matches("0000", b))
    }

    @Test
    fun `a fixed salt gives a stable record`() {
        assertEquals(PinLock.record("4321", "abc"), PinLock.record("4321", "abc"))
    }

    @Test
    fun `no record or a broken record never unlocks`() {
        assertFalse(PinLock.matches("1234", null))
        assertFalse(PinLock.matches("1234", ""))
        assertFalse(PinLock.matches("1234", "nocolon"))
        assertFalse(PinLock.matches("1234", ":deadbeef"))
        assertFalse(PinLock.matches("1234", "salt:"))
    }

    @Test
    fun `only four digits are a PIN`() {
        assertTrue(PinLock.isValid("0000"))
        assertTrue(PinLock.isValid("9876"))
        assertFalse(PinLock.isValid("123"))
        assertFalse(PinLock.isValid("12345"))
        assertFalse(PinLock.isValid("12a4"))
        assertFalse(PinLock.isValid(""))
    }
}
