package io.github.superthom196.cinematica

import android.app.Application
import android.view.KeyEvent
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import io.github.superthom196.cinematica.api.AppCmd
import io.github.superthom196.cinematica.api.CinematicaApi
import io.github.superthom196.cinematica.api.DISCOVERY_WINDOW_MS
import io.github.superthom196.cinematica.api.FoundServer
import io.github.superthom196.cinematica.api.Health
import io.github.superthom196.cinematica.api.HifiPlayer
import io.github.superthom196.cinematica.api.Movie
import io.github.superthom196.cinematica.api.MovieDetail
import io.github.superthom196.cinematica.api.NowPlaying
import io.github.superthom196.cinematica.api.Pick
import io.github.superthom196.cinematica.api.SeasonResp
import io.github.superthom196.cinematica.api.ShelfSnap
import io.github.superthom196.cinematica.api.StreamInfo
import io.github.superthom196.cinematica.api.HifiStatus
import io.github.superthom196.cinematica.api.SyncInfo
import io.github.superthom196.cinematica.api.TvDetail
import io.github.superthom196.cinematica.api.discover
import io.github.superthom196.cinematica.api.friendly
import io.github.superthom196.cinematica.api.normaliseBaseUrl
import io.github.superthom196.cinematica.browse.LibraryStore
import io.github.superthom196.cinematica.browse.NetStore
import io.github.superthom196.cinematica.browse.PlayStore
import io.github.superthom196.cinematica.browse.SearchStore
import io.github.superthom196.cinematica.data.PinLock
import io.github.superthom196.cinematica.data.Prefs
import io.github.superthom196.cinematica.player.PlayStatus
import io.github.superthom196.cinematica.player.AUDIO_LANGUAGES
import io.github.superthom196.cinematica.player.LipSync
import io.github.superthom196.cinematica.player.PlayerEngine
import io.github.superthom196.cinematica.player.PlayerState
import io.github.superthom196.cinematica.player.SubtitleMode
import io.github.superthom196.cinematica.player.SyncAction
import io.github.superthom196.cinematica.session.LinkState
import io.github.superthom196.cinematica.session.PlayerLink
import io.github.superthom196.cinematica.session.PlayerReport
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

sealed class Phase {
    data object Connecting : Phase()
    data class Connect(val msg: String? = null) : Phase()
    data object Library : Phase()
    /** `/api/health` says no provider is configured yet: [address] is where to go set one up. */
    data class SetupRequired(val address: String) : Phase()
    data object Search : Phase()
    /** One film, full screen. Carries the grid's own copy of it so the screen paints instantly. */
    data class Detail(val movie: Movie, val fromSearch: Boolean = false) : Phase()
    data object Settings : Phase()
    data object Player : Phase()
}

data class Toast(val text: String, val isError: Boolean = false, val urgent: Boolean = false)

data class UiState(
    val phase: Phase = Phase.Connecting,
    val host: String = Prefs.DEFAULT_HOST,
    val health: Health? = null,
    /** Short reason the last /api/health failed, or null while the server is answering. */
    val healthErr: String? = null,
    val nowPlaying: NowPlaying? = null,
    val error: String? = null,
    val toast: Toast? = null,
    /** What the server told us to play, for the status strip: tag, size, audio. */
    val playPick: Pick? = null,
    val playTitle: String? = null,
    /** Whether the film now playing was started with hifi (Sendspin) audio. */
    val hifi: Boolean = false,
    /** The PIN lock is on and has not been answered since the app was last opened. */
    val locked: Boolean = false,
    /** After too many wrong PINs, no attempt counts until this wall-clock time (ms); 0 when not throttled. */
    val pinRetryAt: Long = 0L,
)

class AppViewModel(application: Application) : AndroidViewModel(application) {

    private val prefs = Prefs(application)

    @Volatile private var hostCache: String = Prefs.DEFAULT_HOST
    private val api = CinematicaApi { normaliseBaseUrl(hostCache) }

    private val _ui = MutableStateFlow(UiState())
    val ui: StateFlow<UiState> = _ui.asStateFlow()

    /** The embedded VLC. Cheap to hold: nothing native is built until the first [startPlayback]. */
    private val engine = PlayerEngine(application)
    val player: StateFlow<PlayerState> = engine.state

    // ---- browsing state, each piece in its own small store -------------------
    val library = LibraryStore(viewModelScope, api, prefs)
    val search = SearchStore(viewModelScope, api)
    val net = NetStore(viewModelScope, api) { text, isError -> toast(text, isError) }
    val play = PlayStore(
        viewModelScope, api,
        toast = { text, isError -> toast(text, isError) },
        // Whatever ended the job, the budget the picker used may have moved: re-read it so the
        // chip is not quietly advertising a number from before the film.
        onJobEnded = { net.refresh() },
    )

    /** `/api/movie/{id}` is a live provider call; one lookup per film per session is enough. */
    private val detailCache = mutableMapOf<String, MovieDetail>()

    /** The resolved pick per film or episode, so opening it needs no round trip. Keyed "m:$id" for
     *  a film, "tv:$id:$s:$e" for an episode. */
    private val streamCache = mutableMapOf<String, StreamInfo>()

    // Polling only costs anything while a screen is actually on-screen; the activity flips this
    // in onStart/onStop.
    private val isForeground = MutableStateFlow(false)
    private var toastJob: Job? = null

    // What we are reporting to the server about the current job. `jobId` is also what makes the
    // difference between "idle" and "buffering" in the instant between the play command landing
    // and libVLC's first Opening event.
    private var jobId: String? = null
    private var jobTitle: String? = null
    private var endedReported = false
    private var errorReported = false

    // A resume the converter has not reached yet: the film is held on its first frame while
    // `resumeJob` waits for the converted length to clear the target. While that is true the
    // heartbeat must not report the 0:00 the player is sitting on, or the server would record it
    // as where the viewer got to and the resume point would be lost to the resume itself.
    private var resumeJob: Job? = null
    private var resumeHolding = false

    // Hifi (Sendspin) lip sync: the server streams audio out of band and periodically reports how
    // far the TV's picture has drifted from it. lipSync turns each verdict into a rate nudge or a
    // seek; lastSyncErrMs is the same verdict, RTT-corrected, for the OSD chip.
    private val lipSync = LipSync()
    private var userSeekSeq = 0L
    private val _lastSyncErrMs = MutableStateFlow<Long?>(null)
    val lastSyncErrMs: StateFlow<Long?> = _lastSyncErrMs.asStateFlow()
    // The server's word on the audio itself (starting, live, failed and why), for the OSD.
    private val _hifiStatus = MutableStateFlow<HifiStatus?>(null)
    val hifiStatus: StateFlow<HifiStatus?> = _hifiStatus.asStateFlow()

    private val link = PlayerLink(
        scope = viewModelScope,
        api = api,
        identity = { Triple(prefs.currentPlayerId(), prefs.currentPlayerName(), BuildConfig.VERSION_NAME) },
        report = ::buildReport,
        onReported = ::onReported,
        onPlay = ::startPlayback,
        onStop = { stopPlayback(tellServer = false) },
        onSync = ::onSync,
        onHifiStatus = { _hifiStatus.value = it },
    )
    val linkState: StateFlow<LinkState> = link.link

    /** Bumped by any transport key that the activity swallowed, so the OSD can re-show itself. */
    private val _osdPing = MutableStateFlow(0L)
    val osdPing: StateFlow<Long> = _osdPing.asStateFlow()

    // Settings-screen values: sourced straight from DataStore so the screen always shows the
    // persisted truth, with a sane default while the first read is still in flight.
    val passthrough: StateFlow<Boolean> = prefs.passthrough.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), true)
    val autoplayNext: StateFlow<Boolean> = prefs.autoplayNext.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), true)
    val ukUsBias: StateFlow<Boolean> = prefs.ukUsBias.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), true)
    val networkCachingMs: StateFlow<Int> = prefs.networkCachingMs.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), 3000)
    val verboseVlc: StateFlow<Boolean> = prefs.verboseVlc.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), false)
    // Eagerly, not WhileSubscribed: the heartbeat reads these with .value from a coroutine, not a
    // collector, so a WhileSubscribed flow never started on a fresh launch and every heartbeat
    // reported hifi off until the Settings screen happened to be opened.
    val hifiAudio: StateFlow<Boolean> = prefs.hifiAudio.stateIn(viewModelScope, SharingStarted.Eagerly, false)
    val hifiPlayerUrl: StateFlow<String?> = prefs.hifiPlayerUrl.stateIn(viewModelScope, SharingStarted.Eagerly, null)
    val hifiDelayMs: StateFlow<Int> = prefs.hifiDelayMs.stateIn(viewModelScope, SharingStarted.Eagerly, 0)
    private val _hifiPlayers = MutableStateFlow<List<HifiPlayer>>(emptyList())
    val hifiPlayers: StateFlow<List<HifiPlayer>> = _hifiPlayers.asStateFlow()
    val playerName: StateFlow<String> = prefs.playerName.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), android.os.Build.MODEL)
    val audioLanguage: StateFlow<String> = prefs.audioLanguage.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), Prefs.DEFAULT_AUDIO_LANGUAGE)
    val subtitleMode: StateFlow<SubtitleMode> = prefs.subtitleMode.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), SubtitleMode.Off)
    // Eager, not WhileSubscribed: setForeground() reads it with no screen composed.
    val pinEnabled: StateFlow<Boolean> = prefs.pinEnabled.stateIn(viewModelScope, SharingStarted.Eagerly, false)
    private var pinFailures = 0

    // ---- LAN discovery on the connect screen ---------------------------------
    private val discoveryClient = OkHttpClient()
    private var discoveryJob: Job? = null
    private val _discovered = MutableStateFlow<List<FoundServer>>(emptyList())
    val discovered: StateFlow<List<FoundServer>> = _discovered.asStateFlow()
    private val _discovering = MutableStateFlow(false)
    val discovering: StateFlow<Boolean> = _discovering.asStateFlow()

    /** The library has been populated once; a later host change must not wipe what is on screen. */
    private var browsingStarted = false
    /** Set by a host change: the next startBrowsing() must replace the grid, not append to it. */
    private var hostChanged = false

    init {
        viewModelScope.launch {
            hostCache = prefs.currentHost()
            // Cold start with the lock on: the keypad goes up first and the library loads behind it.
            val locked = prefs.currentPinRecord() != null
            _ui.update { it.copy(host = hostCache, locked = locked) }
            connect(hostCache)
        }
        // Playback settings are pushed into the engine as they change: passthrough takes effect at
        // once, caching and verbosity the next time the engine is built (it rebuilds itself when
        // nothing is on screen).
        viewModelScope.launch {
            combine(prefs.passthrough, prefs.networkCachingMs, prefs.verboseVlc) { p, c, v -> Triple(p, c, v) }
                .collect { (p, c, v) -> engine.applyPrefs(p, c, v) }
        }
        // Language preferences are read here and used by the engine when the next film opens.
        viewModelScope.launch {
            combine(prefs.audioLanguage, prefs.subtitleMode) { lang, mode -> lang to mode }
                .collect { (lang, mode) -> engine.applyTrackPrefs(lang, mode) }
        }
        startHealthPolling()
        startNowPlayingPolling()
    }

    fun setForeground(foreground: Boolean) {
        isForeground.value = foreground
        // The heartbeat is the app's whole claim on being the player, and it must not run while
        // the activity is stopped: a heartbeat older than APP_TTL (15s) is what "disconnected"
        // means to the server, and that is exactly the right answer when nobody is watching.
        if (foreground) {
            link.start()
            return
        }
        // The lock re-arms every time the app leaves the screen: coming back from Home, or from the
        // TV being switched off, is opening it again. Turning the lock on does not lock at once.
        if (pinEnabled.value) _ui.update { it.copy(locked = true) }
        // Home, mid-film. libVLC would carry on decoding with no surface to draw on, the server
        // would go on converting audio for a film nobody is watching, and the cache could never be
        // emptied. So the film ends here: one last heartbeat saying so, then /api/stop. Coming back
        // and playing it again is a fresh play, which is what the server's cache does anyway.
        val playing = engine.state.value.active || jobId != null
        if (!playing) {
            link.stop()
            return
        }
        val ended = buildReport().copy(state = "ended")
        stopPlayback(tellServer = false)
        link.stop(finalReport = ended, onSettled = ::tellServerToStop)
    }

    /**
     * Fire-and-forget `POST /api/stop`: it stands down any conversion still running for this film
     * and empties the torrent cache. Never sent for a stop the server itself asked for, and never
     * when a new play supersedes the current film — the server is already doing both of those.
     */
    private fun tellServerToStop() {
        viewModelScope.launch { runCatching { api.stop() } }
    }

    // ---- playback -----------------------------------------------------------

    /** A `play` command from the server: open its URL, on screen, now. */
    fun startPlayback(cmd: AppCmd) {
        val url = cmd.url ?: return
        // A locked TV does not start films. The phone page's launch times out with its own message,
        // which is the right outcome: the lock is the whole point, and playing behind the keypad
        // would put a film's audio in the room for whoever is holding the remote.
        if (_ui.value.locked) return
        // The player wins over the buffering overlay, always: whatever this app was polling about
        // has just happened, and a second surface for it would only be in the way.
        play.adopt()
        jobId = cmd.job
        jobTitle = cmd.title
        endedReported = false
        errorReported = false
        _ui.update { it.copy(phase = Phase.Player, playPick = cmd.pick, playTitle = cmd.title, hifi = cmd.hifi) }
        val startMs = ((cmd.start_s ?: 0.0) * 1000).toLong().coerceAtLeast(0L)
        engine.open(url, cmd.title, cmd.transcoded == true, hifi = cmd.hifi, startMs = startMs)
        lipSync.onOpen(android.os.SystemClock.uptimeMillis())
        // A film with no resume point must not inherit the last one's wait.
        if (startMs > 0L) startResume(startMs) else { resumeJob?.cancel(); resumeJob = null; endResume() }
    }

    /**
     * Get the film to [targetMs], once, and only ever through [seekTo] — the same path a viewer's
     * seek takes, so `seek_seq` moves and the hifi audio follows a resume as it follows a skip.
     *
     * Directly playable media: as soon as the player says it can seek. Media the server is still
     * converting: nothing plays until the converted length comfortably clears the target, because
     * a forward seek past the live edge leaves the picture hung — so the film waits, paused, and
     * says so. If the converter has not got there in [RESUME_WAIT_MS] the film starts from the
     * beginning rather than leaving the viewer looking at a still frame indefinitely.
     */
    private fun startResume(targetMs: Long) {
        resumeJob?.cancel()
        resumeHolding = false
        resumeJob = viewModelScope.launch {
            val at = io.github.superthom196.cinematica.ui.formatTime(targetMs / 1000.0)
            val giveUpAt = android.os.SystemClock.uptimeMillis() + RESUME_WAIT_MS
            var saidAt = 0L
            while (true) {
                val s = engine.state.value
                if (!s.active) break
                if (!s.isLive) {
                    // A trustworthy length, converted or not: an ordinary seek, the moment one works.
                    if (s.seekable) { endResume(); seekTo(targetMs); break }
                } else if (s.lengthMs > targetMs + RESUME_MARGIN_MS) {
                    endResume()
                    seekTo(targetMs, force = true)
                    engine.play()
                    break
                } else {
                    resumeHolding = true
                    val now = android.os.SystemClock.uptimeMillis()
                    if (now - saidAt > RESUME_SAY_EVERY_MS) {
                        saidAt = now
                        engine.say("Resuming at $at — still converting")
                    }
                    if (now > giveUpAt) {
                        endResume()
                        engine.play()
                        engine.say("Still converting — starting from the beginning")
                        break
                    }
                }
                delay(500)
            }
            resumeHolding = false
        }
    }

    /** Stop waiting on a resume point, whether it was reached, abandoned, or overtaken by a stop. */
    private fun endResume() {
        resumeHolding = false
        engine.clearResume()
    }

    /**
     * A `stop` command, a Back-Back, a media STOP key, or the end of a film.
     *
     * [tellServer] is false only when the server is the one that asked: it has already stood the
     * job down and emptied the cache, and posting `/api/stop` back at it would queue another stop
     * command for a player that is already idle.
     */
    fun stopPlayback(tellServer: Boolean = true) {
        jobId = null
        jobTitle = null
        endedReported = true
        errorReported = true
        resumeJob?.cancel()
        resumeJob = null
        resumeHolding = false
        val leavingPlayer = _ui.value.phase is Phase.Player
        engine.stop()
        engine.setRate(1f)
        lipSync.reset()
        _lastSyncErrMs.value = null
        _hifiStatus.value = null
        _ui.update {
            if (it.phase is Phase.Player) it.copy(phase = Phase.Library, playPick = null, playTitle = null, hifi = false)
            else it.copy(playPick = null, playTitle = null, hifi = false)
        }
        // Back on the wall, the film just watched should already show what it now knows about
        // itself. Not while the app is going to the background: there is nobody to show it to.
        if (leavingPlayer && isForeground.value) library.refreshShelf()
        if (tellServer) tellServerToStop()
    }

    /**
     * "Done with this" on the stop prompt. The drop goes first: after it the server ignores this
     * job's progress, so the stop that follows cannot write the position straight back.
     */
    fun dropAndStop() {
        val job = jobId
        if (job == null) { stopPlayback(); return }
        viewModelScope.launch {
            runCatching { api.drop(job) }
            stopPlayback()
        }
    }

    /** A `sync` verdict off the heartbeat, for a hifi film: nudge the engine, and the OSD chip. */
    private fun onSync(info: SyncInfo, rttMs: Long) {
        if (!_ui.value.hifi || engine.state.value.status !is PlayStatus.Playing) return
        val nowMs = android.os.SystemClock.uptimeMillis()
        val trueErr = info.err_s + rttMs / 2000.0
        _lastSyncErrMs.value = (trueErr * 1000).toLong()
        when (val action = lipSync.step(info.gen, info.err_s, rttMs, nowMs)) {
            is SyncAction.Rate -> {
                android.util.Log.i("LipSync", "rate ${action.rate} (err ${"%.3f".format(trueErr)} gen ${info.gen})")
                engine.setRate(action.rate)
            }
            is SyncAction.Seek -> {
                android.util.Log.i("LipSync", "seek ${action.deltaMs}ms (err ${"%.3f".format(trueErr)} gen ${info.gen})")
                engine.setRate(1f)
                engine.seekBy(action.deltaMs)
            }
            SyncAction.None -> Unit
        }
    }

    fun togglePause() {
        // Asking for the film outranks waiting for a resume point: the hold is given up, not fought.
        if (resumeHolding) { resumeJob?.cancel(); resumeJob = null; endResume() }
        engine.setRate(1f)
        lipSync.reset()
        if (engine.state.value.status is PlayStatus.Paused) lipSync.onResume(android.os.SystemClock.uptimeMillis())
        engine.togglePause()
    }
    fun seekBy(ms: Long) {
        userSeekSeq++
        engine.setRate(1f)
        lipSync.reset()
        lipSync.onUserSeek(android.os.SystemClock.uptimeMillis())
        engine.seekBy(ms)
    }
    /** [force] is the resume's, and only the resume's: see [PlayerEngine.seekTo]. */
    fun seekTo(ms: Long, force: Boolean = false) {
        userSeekSeq++
        engine.setRate(1f)
        lipSync.reset()
        lipSync.onUserSeek(android.os.SystemClock.uptimeMillis())
        engine.seekTo(ms, force)
    }
    fun setAudioTrack(id: Int) = engine.setAudioTrack(id)
    fun setSpuTrack(id: Int) = engine.setSpuTrack(id)
    fun attachSurface(layout: org.videolan.libvlc.util.VLCVideoLayout) = engine.attach(layout)
    fun detachSurface() = engine.detach()

    /** Media keys are intercepted by the activity before Compose sees them, from every screen. */
    fun onMediaKey(keyCode: Int) {
        if (_ui.value.phase !is Phase.Player) return
        when (keyCode) {
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> togglePause()
            KeyEvent.KEYCODE_MEDIA_PLAY -> {
                engine.setRate(1f)
                lipSync.reset()
                if (engine.state.value.status is PlayStatus.Paused) lipSync.onResume(android.os.SystemClock.uptimeMillis())
                engine.play()
            }
            KeyEvent.KEYCODE_MEDIA_PAUSE -> {
                engine.setRate(1f)
                lipSync.reset()
                engine.pause()
            }
            KeyEvent.KEYCODE_MEDIA_STOP -> stopPlayback()
            KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> seekBy(30_000L)
            KeyEvent.KEYCODE_MEDIA_REWIND -> seekBy(-30_000L)
        }
        _osdPing.value = _osdPing.value + 1
    }

    /** The state half of the heartbeat: what the server reads to decide the handoff worked. */
    private fun buildReport(): PlayerReport {
        val s = engine.state.value
        val pos = if (s.active) engine.livePositionMs() / 1000.0 else null
        val dur = if (s.active && s.lengthMs > 0L) s.lengthMs / 1000.0 else null
        val report = when (val st = s.status) {
            // Report `job` from the moment the stream starts opening, not once the first frame is
            // up: launch() waits up to APP_HANDOFF_SECS for exactly that and fails the play without it.
            PlayStatus.Opening, is PlayStatus.Buffering -> PlayerReport("buffering", jobId, jobTitle, pos, dur)
            // Held for a resume: "buffering" with no position, because the position it is holding
            // at is 0:00 and the server would record that as where the viewer got to.
            PlayStatus.Playing -> if (resumeHolding) PlayerReport("buffering", jobId, jobTitle) else PlayerReport("playing", jobId, jobTitle, pos, dur)
            PlayStatus.Paused -> if (resumeHolding) PlayerReport("buffering", jobId, jobTitle) else PlayerReport("paused", jobId, jobTitle, pos, dur)
            // "ended" is the falling edge the server's cache watcher looks for; it is said once and
            // then the app is simply idle again.
            PlayStatus.Ended -> if (endedReported) PlayerReport("idle") else PlayerReport("ended", jobId, jobTitle, pos, dur)
            is PlayStatus.Error ->
                if (errorReported) PlayerReport("idle")
                else PlayerReport("error", jobId, jobTitle, err = st.msg)
            PlayStatus.Idle -> if (jobId != null) PlayerReport("buffering", jobId, jobTitle) else PlayerReport("idle")
        }
        return report.copy(
            hifi = if (jobId != null) _ui.value.hifi else hifiAudio.value,
            hifiPlayer = hifiPlayerUrl.value, hifiDelayMs = hifiDelayMs.value, seekSeq = userSeekSeq,
        )
    }

    /** Called after a heartbeat carrying [state] was accepted, so the one-shot states can retire. */
    private fun onReported(state: String) {
        when (state) {
            "ended" -> {
                endedReported = true
                // The film is over: off the screen, back to browsing, engine reset for the next one.
                stopPlayback()
            }
            "error" -> {
                // The job has been failed with our own message; keep it on screen until the user
                // dismisses it, but stop repeating it to the server. The conversion and the cache
                // are still the server's to clear, and nothing else is going to ask it to.
                errorReported = true
                jobId = null
                tellServerToStop()
            }
        }
    }

    // ---- browsing -----------------------------------------------------------

    fun connectManual(host: String) {
        val trimmed = host.trim()
        if (trimmed.isBlank()) {
            _ui.update { it.copy(phase = Phase.Connect("Enter a server address.")) }
            return
        }
        stopDiscovery()
        viewModelScope.launch {
            // A different host has its own library: drop everything learned about the old one so
            // none of it shows up mixed in with the new server's films.
            if (trimmed != hostCache) {
                browsingStarted = false
                hostChanged = true
                detailCache.clear()
                streamCache.clear()
            }
            prefs.setHost(trimmed)
            hostCache = trimmed
            _ui.update { it.copy(host = trimmed) }
            connect(trimmed)
        }
    }

    fun openSettings() {
        if (_ui.value.phase is Phase.Library) _ui.update { it.copy(phase = Phase.Settings) }
    }

    fun openSearch() {
        search.open()
        _ui.update { it.copy(phase = Phase.Search) }
    }

    /** OK on a tile: the film opens with whatever the grid already knows about its stream. A
     *  series row carries its S01E01 pick (that is what the server checked to list it), so the
     *  first episode plays without a round trip. */
    fun openDetail(movie: Movie) {
        val id = movie.id
        val known = movie.stream
        if (id != null && known != null) {
            val key = if (movie.kind == "tv") "tv:$id:1:1" else "m:$id"
            if (!streamCache.containsKey(key)) streamCache[key] = known
        }
        val fromSearch = _ui.value.phase is Phase.Search
        _ui.update { it.copy(phase = Phase.Detail(movie, fromSearch)) }
    }

    fun toLibrary() {
        _ui.update { it.copy(phase = Phase.Library) }
    }

    // ---- the shelf ----------------------------------------------------------
    // Every one of these is optimistic: the grid's own copy changes at once and the request goes
    // after it. A server that has never heard of these routes simply never answers, which leaves
    // the app exactly where it was before the press.

    /** Long-press on a tile. */
    fun toggleWatched(movie: Movie) {
        val id = movie.id ?: return
        val on = movie.shelf?.watched != true
        library.patchShelf(id) { it.copy(watched = on, progress = if (on) null else it.progress) }
        viewModelScope.launch {
            runCatching { api.setWatched(id, on) }.getOrNull()?.shelf?.let { s -> library.patchShelf(id) { s } }
        }
    }

    /** The ♥ on a detail screen. [snap] is what the watchlist draws once the catalogue moves on. */
    fun setFav(id: String, on: Boolean, snap: ShelfSnap) {
        library.patchShelf(id) { it.copy(fav = on) }
        viewModelScope.launch {
            runCatching { api.setFav(id, on, snap) }.getOrNull()?.shelf?.let { s -> library.patchShelf(id) { s } }
        }
    }

    /** Long-press on an episode row; the series' own screen keeps the list it is showing in step. */
    fun setEpisodeWatched(id: String, s: Int, e: Int, on: Boolean) {
        viewModelScope.launch { runCatching { api.setWatched(id, on, s, e) } }
    }

    /** Back out of Detail: to Search with its results intact if that's where it was opened from,
     *  otherwise to the grid. No search.open() call here — that would wipe the results just restored. */
    fun backFromDetail() {
        val phase = _ui.value.phase
        val fromSearch = (phase as? Phase.Detail)?.fromSearch == true
        _ui.update { it.copy(phase = if (fromSearch) Phase.Search else Phase.Library) }
    }

    fun setPlayerName(name: String) {
        val trimmed = name.trim()
        if (trimmed.isBlank()) return
        viewModelScope.launch { prefs.setPlayerName(trimmed) }
    }

    suspend fun movieDetail(id: String): MovieDetail? {
        detailCache[id]?.let { return it }
        return runCatching { api.movie(id) }.getOrNull()?.also { detailCache[id] = it }
    }

    fun cachedStream(key: String): StreamInfo? = streamCache[key]

    /** `/api/stream/{id}`; `force` bypasses the server's 3h cache and re-resolves from scratch. */
    suspend fun fetchStream(id: String, force: Boolean = false): Result<StreamInfo> =
        runCatching { api.stream(id, force) }.onSuccess { streamCache["m:$id"] = it }

    /** `/api/stream/tv/{id}/{s}/{e}`; same cache, keyed per episode. */
    suspend fun fetchStreamTv(id: String, s: Int, e: Int, force: Boolean = false): Result<StreamInfo> =
        runCatching { api.streamTv(id, s, e, force) }.onSuccess { streamCache["tv:$id:$s:$e"] = it }

    /** `/api/tv/{id}`: series metadata and the season list. */
    suspend fun tvDetail(id: String): Result<TvDetail> = runCatching { api.tv(id) }

    /** `/api/tv/{id}/season/{n}`: one season's episodes. */
    suspend fun tvSeason(id: String, n: Int): Result<SeasonResp> = runCatching { api.tvSeason(id, n) }

    /** SeriesDetailScreen has no other way at the autoplay-next setting. */
    suspend fun currentAutoplayNext(): Boolean = prefs.currentAutoplayNext()

    /** "Server host" in Settings: re-run the connect flow without losing the settings entry point. */
    fun editServer() {
        _ui.update { it.copy(phase = Phase.Connect(null)) }
    }

    /** Settings -> Library. Library -> exit is handled by the caller (there is no VM state for it). */
    fun back() {
        if (_ui.value.phase is Phase.Settings) _ui.update { it.copy(phase = Phase.Library) }
    }

    fun togglePassthrough() {
        viewModelScope.launch { prefs.setPassthrough(!passthrough.value) }
    }

    fun toggleAutoplayNext() {
        viewModelScope.launch { prefs.setAutoplayNext(!autoplayNext.value) }
    }

    /** Settings: the library owns this one, since flipping it reloads the grid. */
    fun toggleUkUsBias() = library.setBias(!ukUsBias.value)

    fun cycleNetworkCaching() {
        val steps = listOf(1000, 2000, 3000, 5000)
        val next = steps.getOrElse(steps.indexOf(networkCachingMs.value) + 1) { steps.first() }
        viewModelScope.launch { prefs.setNetworkCachingMs(next) }
    }

    fun toggleVerboseVlc() {
        viewModelScope.launch { prefs.setVerboseVlc(!verboseVlc.value) }
    }

    /**
     * Nudge the lip-sync trim by [steps] × 25 ms, within −2 s … +5 s. Positive means the sound is
     * heard later. From Settings, and from the player's own lip-sync bar while a film is on.
     *
     * The server is told at once instead of on the next heartbeat, because this is a control the
     * viewer is holding down while watching the screen: the sound slides about a second and a half
     * later, when the audio already queued at the DAC runs out.
     */
    fun adjustHifiDelay(steps: Int) {
        val next = (hifiDelayMs.value + steps * 25).coerceIn(-2_000, 5_000)
        viewModelScope.launch {
            prefs.setHifiDelayMs(next)
            if (_ui.value.hifi) runCatching { api.hifiDelay(next) }
        }
    }

    fun toggleHifiAudio() {
        viewModelScope.launch { prefs.setHifiAudio(!hifiAudio.value) }
    }

    fun refreshHifiPlayers() {
        viewModelScope.launch {
            _hifiPlayers.value = runCatching { api.hifiPlayers() }.getOrNull()?.players ?: emptyList()
        }
    }

    /** Settings: step to the next hifi player by url, wrapping; unrecognised picks land on the first. */
    fun cycleHifiPlayer() {
        val urls = hifiPlayers.value.map { it.url }
        if (urls.isEmpty()) return
        val next = urls.getOrElse(urls.indexOf(hifiPlayerUrl.value) + 1) { urls.first() }
        viewModelScope.launch { prefs.setHifiPlayerUrl(next) }
    }

    /** Settings: step through the handful of languages the library actually carries. */
    fun cycleAudioLanguage() {
        val codes = AUDIO_LANGUAGES.map { it.code }
        val next = codes.getOrElse(codes.indexOf(audioLanguage.value) + 1) { codes.first() }
        viewModelScope.launch { prefs.setAudioLanguage(next) }
    }

    fun cycleSubtitleMode() {
        val modes = SubtitleMode.values().toList()
        val next = modes.getOrElse(modes.indexOf(subtitleMode.value) + 1) { modes.first() }
        viewModelScope.launch { prefs.setSubtitleMode(next) }
    }

    // ---- PIN lock -----------------------------------------------------------

    /**
     * One attempt at the stored PIN. Wrong answers are counted and, past [PIN_MAX_TRIES], nothing
     * counts for [PIN_WAIT_MS]: a D-pad cannot type ten thousand PINs, but a remote's number keys
     * and a patient child can, and thirty seconds per five guesses makes it a day's work.
     */
    suspend fun checkPin(pin: String): Boolean {
        if (System.currentTimeMillis() < _ui.value.pinRetryAt) return false
        val ok = PinLock.matches(pin, prefs.currentPinRecord())
        if (ok) {
            pinFailures = 0
            _ui.update { it.copy(pinRetryAt = 0L) }
        } else if (++pinFailures >= PIN_MAX_TRIES) {
            pinFailures = 0
            _ui.update { it.copy(pinRetryAt = System.currentTimeMillis() + PIN_WAIT_MS) }
        }
        return ok
    }

    /** The lock screen's answer: true unlocks, false leaves it up for another go. */
    suspend fun tryUnlock(pin: String): Boolean {
        val ok = checkPin(pin)
        if (ok) _ui.update { it.copy(locked = false) }
        return ok
    }

    /** Settings: set (or replace) the PIN, which is also what turns the lock on. */
    fun setPin(pin: String) {
        if (!PinLock.isValid(pin)) return
        viewModelScope.launch {
            prefs.setPinRecord(PinLock.record(pin))
            toast("PIN lock on — it will ask next time the app opens")
        }
    }

    /** Settings, after the current PIN was given: forget it, which turns the lock off. */
    fun clearPin() {
        viewModelScope.launch {
            prefs.clearPinRecord()
            toast("PIN lock off")
        }
    }

    fun toast(text: String, isError: Boolean = false, urgent: Boolean = false) {
        toastJob?.cancel()
        _ui.update { it.copy(toast = Toast(text, isError, urgent)) }
        toastJob = viewModelScope.launch {
            delay(if (urgent) 10_000 else 5_000)
            _ui.update { it.copy(toast = null) }
        }
    }

    /**
     * Sweeps the LAN for servers while the connect screen is up, the same way MATV's connect screen
     * does. `discover()` never stops on its own, so the window is closed from here.
     */
    fun startDiscovery() {
        if (discoveryJob?.isActive == true) return
        _discovered.value = emptyList()
        _discovering.value = true
        discoveryJob = viewModelScope.launch {
            val collector = launch {
                discover(getApplication(), hostCache, discoveryClient).collect { _discovered.value = it }
            }
            delay(DISCOVERY_WINDOW_MS)
            collector.cancel()
            _discovering.value = false
        }
    }

    fun stopDiscovery() {
        discoveryJob?.cancel()
        discoveryJob = null
        _discovering.value = false
    }

    private suspend fun connect(host: String) {
        _ui.update { it.copy(phase = Phase.Connecting) }
        runCatching { api.health() }
            .onSuccess { health ->
                if (health.providers?.configured == false) {
                    _ui.update {
                        it.copy(phase = Phase.SetupRequired(normaliseBaseUrl(host)), health = health, healthErr = null, error = null)
                    }
                    return@onSuccess
                }
                _ui.update { it.copy(phase = Phase.Library, health = health, healthErr = null, error = null) }
                startBrowsing()
            }
            .onFailure { e ->
                val short = e.friendly()
                val msg = "Couldn't reach $host — $short"
                _ui.update { it.copy(phase = Phase.Connect(msg), error = msg, healthErr = short) }
            }
    }

    /** First successful connection: restore the saved view, then ask for the first row of films. */
    private suspend fun startBrowsing() {
        if (browsingStarted) return
        browsingStarted = true
        library.restore()
        library.loadGenres()
        // pump() appends; after a host change the rows on screen belong to the other server.
        if (hostChanged) { hostChanged = false; library.reload() } else library.pump()
        net.refresh()
    }

    private fun startHealthPolling() {
        viewModelScope.launch {
            while (true) {
                delay(15_000)
                if (isForeground.value && _ui.value.phase !is Phase.Connecting && _ui.value.phase !is Phase.Player) {
                    runCatching { api.health() }
                        .onSuccess { h ->
                            _ui.update { it.copy(health = h, healthErr = null) }
                            // Setup can be finished from the web UI while the TV app sits here — move
                            // it along without asking for a restart, in either direction.
                            val setupRequired = h.providers?.configured == false
                            if (setupRequired && _ui.value.phase !is Phase.SetupRequired) {
                                _ui.update { it.copy(phase = Phase.SetupRequired(normaliseBaseUrl(hostCache))) }
                            } else if (!setupRequired && _ui.value.phase is Phase.SetupRequired) {
                                _ui.update { it.copy(phase = Phase.Library) }
                                startBrowsing()
                            }
                        }
                        .onFailure { e -> _ui.update { it.copy(healthErr = e.friendly()) } }
                }
            }
        }
    }

    private fun startNowPlayingPolling() {
        viewModelScope.launch {
            while (true) {
                delay(6_000)
                if (isForeground.value && _ui.value.phase !is Phase.Connecting && _ui.value.phase !is Phase.Player) {
                    runCatching { api.nowplaying() }.onSuccess { np -> _ui.update { it.copy(nowPlaying = np) } }
                }
            }
        }
    }

    override fun onCleared() {
        link.stop()
        engine.release()
        super.onCleared()
    }

    private companion object {
        const val PIN_MAX_TRIES = 5
        const val PIN_WAIT_MS = 30_000L

        /** How long a resume waits for the converter before giving up and starting from the top. */
        const val RESUME_WAIT_MS = 180_000L
        /** How far past the resume point the conversion must be before the seek is safe. */
        const val RESUME_MARGIN_MS = 10_000L
        /** A held film says why it is holding this often, so the wait is never unexplained. */
        const val RESUME_SAY_EVERY_MS = 30_000L
    }
}
