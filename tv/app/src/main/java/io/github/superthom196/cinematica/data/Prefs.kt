package io.github.superthom196.cinematica.data

import android.content.Context
import android.os.Build
import androidx.datastore.core.DataStore
import androidx.datastore.core.handlers.ReplaceFileCorruptionHandler
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.emptyPreferences
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.core.stringSetPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import io.github.superthom196.cinematica.player.SubtitleMode
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import java.util.UUID

// A prefs file corrupted by a power cut must not be a crash loop: fall back to empty prefs instead.
private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(
    name = "cinematica",
    corruptionHandler = ReplaceFileCorruptionHandler { emptyPreferences() },
)

/** Every setting the TV app remembers. Flows for the UI, suspend setters for writes. */
class Prefs(private val context: Context) {
    private object K {
        val host = stringPreferencesKey("host")
        val sort = stringPreferencesKey("sort")
        val kind = stringPreferencesKey("kind")
        val genresInclude = stringSetPreferencesKey("genres_include")
        val genresExclude = stringSetPreferencesKey("genres_exclude")
        val genresIncludeTv = stringSetPreferencesKey("genres_include_tv")
        val genresExcludeTv = stringSetPreferencesKey("genres_exclude_tv")
        val ukUsBias = booleanPreferencesKey("uk_us_bias")
        val passthrough = booleanPreferencesKey("passthrough")
        val autoplayNext = booleanPreferencesKey("autoplay_next")
        val networkCachingMs = intPreferencesKey("network_caching_ms")
        val verboseVlc = booleanPreferencesKey("verbose_vlc")
        val hifiAudio = booleanPreferencesKey("hifi_audio")
        val hifiPlayerUrl = stringPreferencesKey("hifi_player_url")
        val hifiDelayMs = intPreferencesKey("hifi_delay_ms")
        val audioLanguage = stringPreferencesKey("audio_language")
        val subtitleMode = stringPreferencesKey("subtitle_mode")
        val playerId = stringPreferencesKey("player_id")
        val playerName = stringPreferencesKey("player_name")
        val pinRecord = stringPreferencesKey("pin_record")
    }

    val host: Flow<String> = context.dataStore.data.map { it[K.host] ?: DEFAULT_HOST }
    suspend fun setHost(value: String) { context.dataStore.edit { it[K.host] = value } }
    suspend fun currentHost(): String = context.dataStore.data.first()[K.host] ?: DEFAULT_HOST

    val sort: Flow<String> = context.dataStore.data.map { it[K.sort] ?: "top" }
    suspend fun setSort(value: String) { context.dataStore.edit { it[K.sort] = value } }

    /** Films vs series: "movie" or "tv". */
    val kind: Flow<String> = context.dataStore.data.map { it[K.kind] ?: "movie" }
    suspend fun setKind(value: String) { context.dataStore.edit { it[K.kind] = value } }

    val genresInclude: Flow<Set<String>> = context.dataStore.data.map { it[K.genresInclude] ?: emptySet() }
    suspend fun setGenresInclude(value: Set<String>) { context.dataStore.edit { it[K.genresInclude] = value } }

    val genresExclude: Flow<Set<String>> = context.dataStore.data.map { it[K.genresExclude] ?: emptySet() }
    suspend fun setGenresExclude(value: Set<String>) { context.dataStore.edit { it[K.genresExclude] = value } }

    /** Series keep their own genre selection, separate from films'. */
    val genresIncludeTv: Flow<Set<String>> = context.dataStore.data.map { it[K.genresIncludeTv] ?: emptySet() }
    suspend fun setGenresIncludeTv(value: Set<String>) { context.dataStore.edit { it[K.genresIncludeTv] = value } }

    val genresExcludeTv: Flow<Set<String>> = context.dataStore.data.map { it[K.genresExcludeTv] ?: emptySet() }
    suspend fun setGenresExcludeTv(value: Set<String>) { context.dataStore.edit { it[K.genresExcludeTv] = value } }

    /**
     * Browsing, both kinds: English-language only, British titles first, the widely watched ones
     * alongside (the server's two-tier pool, `?bias=1`). Off is the provider's own rating order.
     */
    val ukUsBias: Flow<Boolean> = context.dataStore.data.map { it[K.ukUsBias] ?: true }
    suspend fun setUkUsBias(value: Boolean) { context.dataStore.edit { it[K.ukUsBias] = value } }

    val passthrough: Flow<Boolean> = context.dataStore.data.map { it[K.passthrough] ?: true }
    suspend fun setPassthrough(value: Boolean) { context.dataStore.edit { it[K.passthrough] = value } }

    val autoplayNext: Flow<Boolean> = context.dataStore.data.map { it[K.autoplayNext] ?: true }
    suspend fun setAutoplayNext(value: Boolean) { context.dataStore.edit { it[K.autoplayNext] = value } }
    suspend fun currentAutoplayNext(): Boolean = context.dataStore.data.first()[K.autoplayNext] ?: true

    val networkCachingMs: Flow<Int> = context.dataStore.data.map { it[K.networkCachingMs] ?: 3000 }
    suspend fun setNetworkCachingMs(value: Int) { context.dataStore.edit { it[K.networkCachingMs] = value } }

    val verboseVlc: Flow<Boolean> = context.dataStore.data.map { it[K.verboseVlc] ?: false }
    suspend fun setVerboseVlc(value: Boolean) { context.dataStore.edit { it[K.verboseVlc] = value } }

    /** Sendspin hifi audio: the server sends lossless audio out of band and the TV mutes its own. */
    val hifiAudio: Flow<Boolean> = context.dataStore.data.map { it[K.hifiAudio] ?: false }
    suspend fun setHifiAudio(value: Boolean) { context.dataStore.edit { it[K.hifiAudio] = value } }

    /**
     * Lip-sync trim for hifi audio, in ms, the "audio delay" of an AV receiver: positive when the
     * sound was arriving before the picture. Sent to the server on every heartbeat; it shifts
     * where the picture is held on the audio timeline.
     */
    val hifiDelayMs: Flow<Int> = context.dataStore.data.map { it[K.hifiDelayMs] ?: 0 }
    suspend fun setHifiDelayMs(value: Int) { context.dataStore.edit { it[K.hifiDelayMs] = value } }

    /** The chosen hifi player's url, or null to leave the pick to the server's default. */
    val hifiPlayerUrl: Flow<String?> = context.dataStore.data.map { it[K.hifiPlayerUrl] }
    suspend fun setHifiPlayerUrl(value: String?) {
        context.dataStore.edit {
            if (value == null) it.remove(K.hifiPlayerUrl) else it[K.hifiPlayerUrl] = value
        }
    }

    /**
     * The language the viewer wants to hear, as an ISO 639-1 code. Files spell languages every
     * which way ("en", "eng", "English"); the matcher in TrackSelection.kt treats them as one.
     */
    val audioLanguage: Flow<String> = context.dataStore.data.map { it[K.audioLanguage] ?: DEFAULT_AUDIO_LANGUAGE }
    suspend fun setAudioLanguage(value: String) { context.dataStore.edit { it[K.audioLanguage] = value } }

    /** One of [SubtitleMode]'s keys: off | auto | on. */
    val subtitleMode: Flow<SubtitleMode> = context.dataStore.data.map { SubtitleMode.fromKey(it[K.subtitleMode]) }
    suspend fun setSubtitleMode(value: SubtitleMode) { context.dataStore.edit { it[K.subtitleMode] = value.key } }

    /** Generated once on first read and persisted from then on: this install's stable identity. */
    val playerId: Flow<String> = context.dataStore.data.map { it[K.playerId] ?: "" }
    suspend fun currentPlayerId(): String {
        val existing = context.dataStore.data.first()[K.playerId]
        if (!existing.isNullOrBlank()) return existing
        val fresh = UUID.randomUUID().toString()
        context.dataStore.edit { it[K.playerId] = fresh }
        return fresh
    }

    val playerName: Flow<String> = context.dataStore.data.map { it[K.playerName] ?: Build.MODEL }
    suspend fun setPlayerName(value: String) { context.dataStore.edit { it[K.playerName] = value } }
    suspend fun currentPlayerName(): String = context.dataStore.data.first()[K.playerName] ?: Build.MODEL

    /**
     * The PIN lock. Off by default: it is on exactly when a record (see [PinLock.record]) is stored,
     * so there is no separate switch that could be left on with no PIN behind it.
     */
    val pinEnabled: Flow<Boolean> = context.dataStore.data.map { !it[K.pinRecord].isNullOrBlank() }
    suspend fun currentPinRecord(): String? = context.dataStore.data.first()[K.pinRecord]?.takeIf { it.isNotBlank() }
    suspend fun setPinRecord(record: String) { context.dataStore.edit { it[K.pinRecord] = record } }
    suspend fun clearPinRecord() { context.dataStore.edit { it.remove(K.pinRecord) } }

    companion object {
        // Not any particular stranger's box -- a placeholder a first-run install might plausibly
        // answer to (see ServerDiscovery's KNOWN_NAMED_HOSTS). It fails fast on a name nobody has,
        // which is what sends a first run straight to the Connect screen, where the LAN sweep and
        // the same known-host probe actually find the real server.
        const val DEFAULT_HOST = "cinematica.local:8090"
        const val DEFAULT_AUDIO_LANGUAGE = "en"
    }
}
