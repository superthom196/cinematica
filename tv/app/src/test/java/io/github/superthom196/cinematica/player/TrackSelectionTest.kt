package io.github.superthom196.cinematica.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The language matcher and the automatic track choice. Both are pure functions on purpose: the
 * failure this fixes (Spanish audio under Russian subtitles, because the file said so) is a
 * decision, and a decision can be tested without a TV in the room.
 */
class TrackSelectionTest {

    // ---- the matcher --------------------------------------------------------

    @Test
    fun `every spelling of English matches the preference en`() {
        assertTrue(matchesLanguage("en", "en"))
        assertTrue(matchesLanguage("en", "eng"))
        assertTrue(matchesLanguage("en", "English"))
        assertTrue(matchesLanguage("en", "english"))
        assertTrue(matchesLanguage("en", "Track 2 - [English]"))
        assertTrue(matchesLanguage("en", "[eng] (stereo)"))
        assertTrue(matchesLanguage("en", "Track 1 - [English] (5.1)"))
    }

    @Test
    fun `other languages do not match en`() {
        assertFalse(matchesLanguage("en", "spa"))
        assertFalse(matchesLanguage("en", "Spanish"))
        assertFalse(matchesLanguage("en", "Track 1 - [Spanish]"))
        assertFalse(matchesLanguage("en", "rus"))
        assertFalse(matchesLanguage("en", "Track 3 - [Russian]"))
        assertFalse(matchesLanguage("en", null))
        assertFalse(matchesLanguage("en", ""))
    }

    @Test
    fun `three-letter codes and native names are the same language`() {
        assertTrue(matchesLanguage("de", "ger"))      // 639-2/B
        assertTrue(matchesLanguage("de", "deu"))      // 639-2/T
        assertTrue(matchesLanguage("de", "Deutsch"))
        assertTrue(matchesLanguage("fr", "Français")) // accents stripped
        assertTrue(matchesLanguage("nl", "dut"))
        assertTrue(matchesLanguage("pt", "pt-BR"))
        assertTrue(matchesLanguage("es", "Track 1 - [Castellano]"))
    }

    @Test
    fun `a track matches on either its ISO language or its label`() {
        assertTrue(matchesLanguage("en", TrackInfo(1, "Track 1 - [English]", null)))
        assertTrue(matchesLanguage("en", TrackInfo(1, "Commentary", "eng")))
        assertFalse(matchesLanguage("en", TrackInfo(1, "Commentary", "spa")))
    }

    @Test
    fun `display names come back from codes`() {
        assertEquals("English", displayLanguage("en"))
        assertEquals("Swedish", displayLanguage("sv"))
        assertEquals("zz", displayLanguage("zz"))
    }

    // ---- the decision -------------------------------------------------------

    /** The film that started all this: Spanish audio and Russian subtitles, both flagged default. */
    private val spanishDefault = listOf(
        TrackInfo(1, "Track 1 - [Spanish]", "spa"),
        TrackInfo(2, "Track 2 - [English]", "eng"),
    )
    private val russianSubs = listOf(
        TrackInfo(4, "Track 1 - [Russian]", "rus"),
        TrackInfo(5, "Track 2 - [English]", "eng"),
    )

    @Test
    fun `auto switches to English audio and leaves subtitles off`() {
        val d = chooseTracks(spanishDefault, russianSubs, "en", SubtitleMode.Auto, currentAudioId = 1)
        assertEquals(2, d.audioId)
        assertEquals(-1, d.spuId)
        assertEquals("English", d.audioLabel)
        assertEquals("off", d.spuLabel)
    }

    @Test
    fun `auto turns English subtitles on when the audio cannot be English`() {
        val d = chooseTracks(listOf(spanishDefault[0]), russianSubs, "en", SubtitleMode.Auto, currentAudioId = 1)
        assertNull("no English audio exists, so libVLC's choice stands", d.audioId)
        assertEquals(5, d.spuId)
        assertEquals("Spanish", d.audioLabel)
        assertEquals("English", d.spuLabel)
    }

    @Test
    fun `auto falls back to the first subtitle track when none is English`() {
        val d = chooseTracks(listOf(spanishDefault[0]), listOf(russianSubs[0]), "en", SubtitleMode.Auto, currentAudioId = 1)
        assertEquals(4, d.spuId)
        assertEquals("Russian", d.spuLabel)
    }

    @Test
    fun `off never turns subtitles on, whatever the audio is`() {
        val d = chooseTracks(spanishDefault, russianSubs, "en", SubtitleMode.Off, currentAudioId = 1)
        assertEquals(2, d.audioId)
        assertEquals(-1, d.spuId)
        assertEquals("off", d.spuLabel)
    }

    @Test
    fun `on shows English subtitles even under English audio`() {
        val d = chooseTracks(spanishDefault, russianSubs, "en", SubtitleMode.On, currentAudioId = 1)
        assertEquals(2, d.audioId)
        assertEquals(5, d.spuId)
        assertEquals("English", d.spuLabel)
    }

    @Test
    fun `nothing is switched when the file is already right`() {
        val d = chooseTracks(
            listOf(TrackInfo(1, "Track 1 - [English]", "eng")),
            emptyList(),
            "en",
            SubtitleMode.Auto,
            currentAudioId = 1,
        )
        assertNull(d.audioId)
        assertEquals(-1, d.spuId)
        assertEquals("English", d.audioLabel)
    }

    @Test
    fun `VLC's Disable pseudo-track is never chosen`() {
        val d = chooseTracks(
            listOf(TrackInfo(-1, "Disable"), TrackInfo(3, "Track 1 - [English]", "eng")),
            listOf(TrackInfo(-1, "Disable"), TrackInfo(7, "Track 1 - [French]", "fre")),
            "en",
            SubtitleMode.On,
            currentAudioId = -1,
        )
        assertEquals(3, d.audioId)
        assertEquals(7, d.spuId)
    }

    @Test
    fun `an unlabelled audio track is treated as not the preferred language`() {
        val d = chooseTracks(
            listOf(TrackInfo(1, "Track 1")),
            listOf(TrackInfo(4, "Track 1 - [English]", "eng")),
            "en",
            SubtitleMode.Auto,
            currentAudioId = 1,
        )
        assertNull(d.audioId)
        assertEquals(4, d.spuId)
    }

    @Test
    fun `a preference other than English works the same way`() {
        val d = chooseTracks(spanishDefault, russianSubs, "es", SubtitleMode.Auto, currentAudioId = 2)
        assertEquals(1, d.audioId)
        assertEquals(-1, d.spuId)
        assertEquals("Spanish", d.audioLabel)
    }

    @Test
    fun `subtitle modes survive the round trip through prefs`() {
        assertEquals(SubtitleMode.Off, SubtitleMode.fromKey("off"))
        assertEquals(SubtitleMode.Auto, SubtitleMode.fromKey("auto"))
        assertEquals(SubtitleMode.On, SubtitleMode.fromKey("on"))
        assertEquals("off is the default", SubtitleMode.Off, SubtitleMode.fromKey(null))
        assertEquals(SubtitleMode.Off, SubtitleMode.fromKey("nonsense"))
    }

    @Test
    fun `an unknown or unset stored mode means Off`() {
        assertEquals(SubtitleMode.Off, SubtitleMode.fromKey(null))
        assertEquals(SubtitleMode.Off, SubtitleMode.fromKey("banana"))
        assertEquals(SubtitleMode.Auto, SubtitleMode.fromKey("auto"))
    }

    @Test
    fun `hifi film with no TV audio track still decides subtitles from the file's audio`() {
        // Hifi: the TV's audio is off (current id -1) and the server plays the English track out of
        // band. Auto must not read "no audio playing" as "not English" and switch subtitles on.
        val audio = listOf(TrackInfo(1, "Track 1", "eng"))
        val spu = listOf(TrackInfo(3, "GalaxySubs", "eng"))
        val auto = chooseTracks(audio, spu, "en", SubtitleMode.Auto, currentAudioId = -1)
        assertEquals(-1, auto.spuId)
        val french = chooseTracks(listOf(TrackInfo(1, "Track 1", "fra")), spu, "en", SubtitleMode.Auto, currentAudioId = -1)
        assertEquals(3, french.spuId)
        val off = chooseTracks(audio, spu, "en", SubtitleMode.Off, currentAudioId = -1)
        assertEquals(-1, off.spuId)
    }
}
