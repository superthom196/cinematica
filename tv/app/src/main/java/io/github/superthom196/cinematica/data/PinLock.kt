package io.github.superthom196.cinematica.data

import java.security.MessageDigest
import java.security.SecureRandom

const val PIN_LENGTH = 4

/**
 * The PIN lock's arithmetic, kept out of the view model so it can be tested without a TV.
 *
 * The PIN itself is never stored: DataStore holds `salt:sha256(salt, pin)`, and a lock that is
 * "on" is simply a record being present. This is a courtesy lock against other people in the
 * house, not a cryptographic one — four digits are four digits — but it costs nothing to avoid
 * leaving the number itself in a preferences file, and the view model throttles guesses.
 */
object PinLock {
    fun isValid(pin: String): Boolean = pin.length == PIN_LENGTH && pin.all { it in '0'..'9' }

    /** What to persist for [pin]. A fresh salt every time, so changing to the same PIN still changes the record. */
    fun record(pin: String, salt: String = newSalt()): String = "$salt:${digest(salt, pin)}"

    /** True only for a well-formed record whose digest matches [pin]. Null or garbage never matches. */
    fun matches(pin: String, record: String?): Boolean {
        if (record.isNullOrBlank()) return false
        val parts = record.split(':', limit = 2)
        if (parts.size != 2 || parts[0].isEmpty() || parts[1].isEmpty()) return false
        return MessageDigest.isEqual(digest(parts[0], pin).toByteArray(), parts[1].toByteArray())
    }

    private fun digest(salt: String, pin: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest("cinematica-pin:$salt:$pin".toByteArray())
            .joinToString("") { "%02x".format(it) }

    private fun newSalt(): String = ByteArray(16).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }
}
