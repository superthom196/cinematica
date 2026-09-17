package io.github.superthom196.cinematica.player

import java.text.Normalizer

/**
 * Picking the audio and subtitle track a viewer actually wants.
 *
 * A Matroska file says which of its tracks are "default", and libVLC believes it — which is how a
 * film ends up playing Spanish audio under Russian subtitles on an English speaker's TV. The fix
 * is to decide for ourselves from the track languages, once per media, and then get out of the way.
 *
 * Everything here is pure Kotlin with no Android or VLC types in it, so the whole decision is
 * covered by a plain JUnit test (TrackSelectionTest).
 */

/** What the user wants subtitles to do. Stored in [io.github.superthom196.cinematica.data.Prefs] as [key]. */
enum class SubtitleMode(val key: String, val label: String) {
    /** Never turned on for them; the picker still works. */
    Off("off", "Off"),

    /** Only when the audio is in a language they did not ask for. */
    Auto("auto", "Auto"),

    /** Always, in their language if the file has one. */
    On("on", "On");

    companion object {
        fun fromKey(key: String?): SubtitleMode {
            val k = key?.trim()?.lowercase()
            // Off until the viewer says otherwise: subtitles nobody asked for are the worse default.
            return values().firstOrNull { it.key == k } ?: Off
        }
    }
}

/** One elementary stream as the engine sees it: VLC's track id, its label, and its ISO language. */
data class TrackInfo(val id: Int, val name: String, val language: String? = null)

/**
 * The outcome of [chooseTracks]. A null id means "leave libVLC's choice alone"; [spuId] of -1 is
 * VLC's own "disable subtitles" track.
 */
data class AutoTracks(
    val audioId: Int?,
    val spuId: Int?,
    /** For the OSD: "English", or the raw track name when the language is anybody's guess. */
    val audioLabel: String,
    /** For the OSD: "English", or "off". */
    val spuLabel: String,
)

/**
 * The languages the settings screen offers. The ISO 639-1 code is what gets stored; [aliases] are
 * every other spelling a file might use for the same language — the 639-2/B and /T three-letter
 * codes, the English name, and the language's own name, because Matroska muxers use all of them.
 */
data class LanguageOption(val code: String, val label: String, val aliases: List<String>)

val AUDIO_LANGUAGES: List<LanguageOption> = listOf(
    LanguageOption("en", "English", listOf("eng", "english")),
    LanguageOption("fr", "French", listOf("fra", "fre", "french", "francais")),
    LanguageOption("de", "German", listOf("deu", "ger", "german", "deutsch")),
    LanguageOption("es", "Spanish", listOf("spa", "esp", "spanish", "espanol", "castellano")),
    LanguageOption("it", "Italian", listOf("ita", "italian", "italiano")),
    LanguageOption("ja", "Japanese", listOf("jpn", "japanese", "nihongo")),
    LanguageOption("ko", "Korean", listOf("kor", "korean", "hangul")),
    LanguageOption("pt", "Portuguese", listOf("por", "portuguese", "portugues", "brazilian")),
    LanguageOption("ru", "Russian", listOf("rus", "russian", "russkiy")),
    LanguageOption("sv", "Swedish", listOf("swe", "swedish", "svenska")),
    LanguageOption("nl", "Dutch", listOf("nld", "dut", "dutch", "nederlands", "flemish", "vlaams")),
)

private val BY_CODE: Map<String, LanguageOption> = AUDIO_LANGUAGES.associateBy { it.code }

/** Every spelling that counts as this language, the 639-1 code included. */
private fun spellings(code: String): Set<String> {
    val known = BY_CODE[normalise(code)]
    if (known != null) return (known.aliases + known.code).toSet()
    // An unlisted preference still works, it just has no alias list of its own.
    return setOf(normalise(code))
}

/** Lower case, accents stripped: "Français" and "francais" are the same word to the matcher. */
private fun normalise(s: String): String =
    Normalizer.normalize(s.trim().lowercase(), Normalizer.Form.NFD)
        .replace(Regex("\\p{Mn}+"), "")

/**
 * The words in a track label. `"Track 2 - [English]"`, `"[eng] (stereo)"` and `"pt-BR"` all reduce
 * to their parts, so a language can be recognised wherever the muxer decided to put it.
 */
private fun tokens(s: String): List<String> =
    normalise(s).split(Regex("[^a-z0-9]+")).filter { it.isNotEmpty() }

/**
 * Does [candidate] name the language [preference]? [candidate] may be an ISO code of either length,
 * an English or native language name, or a whole VLC track description containing one.
 */
fun matchesLanguage(preference: String, candidate: String?): Boolean {
    if (candidate.isNullOrBlank() || preference.isBlank()) return false
    val wanted = spellings(preference)
    return tokens(candidate).any { it in wanted }
}

/** True when either the ISO language of the stream or its label says [preference]. */
fun matchesLanguage(preference: String, track: TrackInfo): Boolean =
    matchesLanguage(preference, track.language) || matchesLanguage(preference, track.name)

/** "en" -> "English". An unknown code is shown as it was stored. */
fun displayLanguage(code: String): String = BY_CODE[normalise(code)]?.label ?: code

/** The best human name for a track: its language if we can tell, otherwise VLC's own label. */
fun trackLabel(track: TrackInfo?): String {
    if (track == null) return "default"
    val named = AUDIO_LANGUAGES.firstOrNull { matchesLanguage(it.code, track) }
    if (named != null) return named.label
    return track.name.ifBlank { "Track ${track.id}" }
}

/**
 * The whole decision, for one freshly opened media.
 *
 * Audio: the first track in the file's own order whose language is [preference]. If none is, the
 * file's choice stands — a film with no English audio is not improved by switching it to Korean.
 *
 * Subtitles: see [SubtitleMode]. `Auto` is the interesting one: subtitles appear only when the
 * audio that is actually going to play is *not* in the preferred language, which is exactly the
 * case where the viewer needs them.
 *
 * [currentAudioId] is whatever libVLC has already selected, so the label is right even when no
 * switch is needed.
 */
fun chooseTracks(
    audio: List<TrackInfo>,
    spu: List<TrackInfo>,
    preference: String,
    mode: SubtitleMode,
    currentAudioId: Int = -1,
): AutoTracks {
    // VLC puts a "Disable" entry with id -1 in both lists; it is an action, not a track.
    val audioTracks = audio.filter { it.id != -1 }
    val spuTracks = spu.filter { it.id != -1 }

    val wantedAudio = audioTracks.firstOrNull { matchesLanguage(preference, it) }
    val effectiveAudio = wantedAudio ?: audioTracks.firstOrNull { it.id == currentAudioId }
    // Unknown audio counts as "not the preferred language": if we cannot tell what is being
    // spoken, subtitles in Auto are the safer side to err on.
    val audioIsPreferred = effectiveAudio != null && matchesLanguage(preference, effectiveAudio)

    val preferredSpu = spuTracks.firstOrNull { matchesLanguage(preference, it) }
    val anySpu = preferredSpu ?: spuTracks.firstOrNull()
    val chosenSpu = when (mode) {
        SubtitleMode.Off -> null
        SubtitleMode.On -> anySpu
        SubtitleMode.Auto -> if (audioIsPreferred) null else anySpu
    }

    return AutoTracks(
        // Only say "switch" when there is something to switch to and it is not already playing.
        audioId = wantedAudio?.id?.takeIf { it != currentAudioId },
        spuId = chosenSpu?.id ?: -1,
        audioLabel = trackLabel(effectiveAudio),
        spuLabel = if (chosenSpu == null) "off" else trackLabel(chosenSpu),
    )
}
