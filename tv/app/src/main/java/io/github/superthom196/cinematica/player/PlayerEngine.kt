package io.github.superthom196.cinematica.player

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.View
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.videolan.libvlc.LibVLC
import org.videolan.libvlc.Media
import org.videolan.libvlc.MediaPlayer
import org.videolan.libvlc.interfaces.IMedia
import org.videolan.libvlc.util.VLCVideoLayout
import java.io.File

/** What the engine is doing right now. [Buffering] carries libVLC's own percentage. */
sealed interface PlayStatus {
    data object Idle : PlayStatus
    data object Opening : PlayStatus
    data class Buffering(val pct: Float) : PlayStatus
    data object Playing : PlayStatus
    data object Paused : PlayStatus
    data object Ended : PlayStatus
    data class Error(val msg: String) : PlayStatus
}

/** One entry of libVLC's `TrackDescription` list, flattened so the UI never touches a VLC type. */
data class TrackOption(val id: Int, val name: String)

data class PlayerState(
    val status: PlayStatus = PlayStatus.Idle,
    val timeMs: Long = 0L,
    val lengthMs: Long = 0L,
    val seekable: Boolean = false,
    val audioTracks: List<TrackOption> = emptyList(),
    val spuTracks: List<TrackOption> = emptyList(),
    val audioTrack: Int = -1,
    val spuTrack: Int = -1,
    val url: String? = null,
    val title: String? = null,
    val transcoded: Boolean = false,
    /** The length is still growing: see [isLive]. */
    val lengthProvisional: Boolean = false,
    /** A one-line refusal or hint for the OSD ("seeking isn't available yet"). */
    val notice: String? = null,
    /** Bumped with every new [notice] so the UI can re-show one it has already dismissed. */
    val noticeSeq: Long = 0L,
    /** What the automatic track choice settled on, e.g. "Audio: English · Subtitles: off". */
    val trackNote: String? = null,
    /** Bumped once per automatic choice, so the OSD shows the line for one film only. */
    val trackNoteSeq: Long = 0L,
) {
    /**
     * True while there is no trustworthy total: either no length at all, or a length that is still
     * climbing because ffmpeg is still writing the file. A growing `/audio/` TS reports "the part
     * converted so far", which is a real number and a wrong one — 1:02 into a two-hour film.
     */
    val isLive: Boolean get() = transcoded && (lengthMs <= 0L || lengthProvisional)
    val active: Boolean
        get() = status is PlayStatus.Opening || status is PlayStatus.Buffering ||
            status is PlayStatus.Playing || status is PlayStatus.Paused
}

/**
 * The embedded VLC. One [LibVLC] and one [MediaPlayer], created lazily and torn down together.
 *
 * Every public method must be called from the main thread: libVLC's own event callbacks arrive
 * there (VLCObject posts them to the main looper) and the surface attach/detach has to be on the
 * UI thread anyway, so keeping one thread for the whole engine removes the need for any locking.
 */
class PlayerEngine(private val context: Context) {

    private val _state = MutableStateFlow(PlayerState())
    val state: StateFlow<PlayerState> = _state.asStateFlow()

    private var libVlc: LibVLC? = null
    private var player: MediaPlayer? = null
    private var attached: VLCVideoLayout? = null

    /** A media that is ready to go but has no window yet. See [open]. */
    private var pending: Media? = null

    // Options that are baked into LibVLC at creation (caching, verbosity) versus ones the player
    // can be told about at any time (passthrough). A change to the former only takes effect on the
    // next engine, so it is recorded and acted on the next time the engine is built.
    private var passthrough = true
    private var networkCachingMs = 3000
    private var verbose = false
    private var optionsStale = false

    // The published state is assembled from plain fields rather than repeatedly copying a data
    // class: TimeChanged fires several times a second and most of those updates are thrown away by
    // the throttle below.
    private var status: PlayStatus = PlayStatus.Idle
    private var timeMs = 0L
    private var timeAt = 0L // uptimeMillis when timeMs last came from a TimeChanged event
    private var lengthMs = 0L
    private var seekable = false
    private var audioTracks: List<TrackOption> = emptyList()
    private var spuTracks: List<TrackOption> = emptyList()
    private var audioTrack = -1
    private var spuTrack = -1

    // Debug-only playback controls (Phase 0 spike). `rate` is remembered so a later open() can put
    // the player back at normal speed; `audioDisabled` is checked by the autoselect logic so it
    // never re-enables audio once the spike has turned it off.
    private var rate = 1.0f
    private var audioDisabled = false
    // Hifi centre mode: the film's audio stream (ffmpeg's `0:a:N`, the one the server decodes for
    // the Sendspin player) whose centre channel the TV plays; null when the TV stays silent.
    // `builtCentre` is whether the LibVLC instance carries the centre-only filter, which lives on
    // the instance and so is rebuilt when the next film wants the other one.
    private var centreTrack: Int? = null
    private var builtCentre = false
    private var url: String? = null
    private var title: String? = null
    private var transcoded = false
    /** Where this film should start, while that has not been dealt with yet. See [open]. */
    private var resumeMs = 0L
    private var notice: String? = null
    private var noticeSeq = 0L
    private var trackNote: String? = null
    private var trackNoteSeq = 0L
    private var lastPublish = 0L
    private var lengthGrewAt = 0L

    // The viewer's language preferences, and the per-media bookkeeping that keeps the automatic
    // choice to exactly one act: made once, and never made again over the top of a choice the
    // user has made by hand in the picker.
    private var audioLanguage = "en"
    private var subtitleMode = SubtitleMode.Off
    private var userChoseAudio = false
    private var userChoseSpu = false
    private var autoApplied = false
    private var autoAppliedAt = 0L
    private var spuSeen = 0

    // SPIKE: remove after Phase 0 -- lets `adb shell am broadcast -a
    // io.github.superthom196.cinematica.RATECYCLE` trigger debugRateCycle() without any UI.
    private var rateCycleHandler: Handler? = null
    private var rateCycleRunnable: Runnable? = null
    private val rateCycleReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            debugRateCycle(true)
        }
    }
    // Unregistering a receiver twice throws, so release() has to know whether it still is.
    private var rateCycleRegistered = false

    init {
        // SPIKE: remove after Phase 0
        val filter = IntentFilter(ACTION_RATECYCLE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(rateCycleReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            context.registerReceiver(rateCycleReceiver, filter)
        }
        rateCycleRegistered = true
    }

    /** Settings the user can change while the app is up. Returns true if the engine was rebuilt. */
    fun applyPrefs(passthrough: Boolean, networkCachingMs: Int, verboseVlc: Boolean) {
        if (this.networkCachingMs != networkCachingMs || this.verbose != verboseVlc) {
            this.networkCachingMs = networkCachingMs
            this.verbose = verboseVlc
            optionsStale = true
        }
        applyAudioPrefs(passthrough)
    }

    /**
     * Language preferences. They apply to the *next* film: changing them mid-film would fight the
     * choice the viewer has just made in the track picker.
     */
    fun applyTrackPrefs(audioLanguage: String, subtitleMode: SubtitleMode) {
        this.audioLanguage = audioLanguage
        this.subtitleMode = subtitleMode
    }

    /** Audio output prefs are cheap and live: "digital output" is exactly the VLC app's passthrough. */
    fun applyAudioPrefs(passthrough: Boolean) {
        this.passthrough = passthrough
        player?.let { applyAudio(it) }
    }

    /**
     * Audio output: AudioTrack, with digital output (passthrough) as the user asked for it.
     *
     * Measured on the Bravia 2026-09-14, at `-vv`: with this on, VLC does ask for the encoded
     * track (`audio output: VLC is looking for: 'a52 '` / `'eac3'`) and the android_audiotrack
     * module answers `module not functional`, so it falls back to decoding to PCM and downmixing
     * to stereo. **The standalone VLC app on this same TV, with its own digital-output preference
     * on, does exactly the same thing** — so this is the panel refusing an encoded AudioTrack, not
     * something this app is doing differently. Naming the encoded sink explicitly
     * (`forceAudioDigitalEncodings`, or its `"encoded:<mask>"` device string) changes nothing:
     * both were tried and the module still refuses. What remains to try is on the TV itself —
     * its Sound / digital-audio-out setting and which output is live.
     */
    private fun applyAudio(mp: MediaPlayer) {
        mp.setAudioOutput(AUDIO_OUTPUT)
        // A passed-through bitstream never reaches the centre-only filter.
        mp.setAudioDigitalOutputEnabled(passthrough && centreTrack == null)
    }

    fun attach(layout: VLCVideoLayout) {
        val mp = ensurePlayer()
        if (attached !== layout) {
            if (attached != null) mp.detachViews()
            // A SurfaceView with subtitles, never a TextureView: the Bravia's HDR path only exists
            // on the display output, and a TextureView would also cost a GPU copy this SoC cannot
            // spare.
            mp.attachViews(layout, null, true, false)
            attached = layout
        }
        // ...and not merely composed: a SurfaceView has no Surface until it is in the window, and
        // android_display blocks waiting for one. Start on the attach, a frame later.
        if (layout.isAttachedToWindow) layout.post { startPending() } else {
            layout.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
                override fun onViewAttachedToWindow(v: View) {
                    v.removeOnAttachStateChangeListener(this)
                    v.post { startPending() }
                }
                override fun onViewDetachedFromWindow(v: View) = Unit
            })
        }
    }

    fun detach() {
        if (attached == null) return
        player?.detachViews()
        attached = null
    }

    /**
     * Point the engine at a URL and start it. Replaces whatever was playing.
     *
     * [hifi] is Sendspin: the server streams lossless audio out of band, so the TV's own decode
     * must stay silent for this film — otherwise the viewer hears both tracks, out of step.
     * [centreTrack] is the exception: the TV as centre speaker, playing only that stream's centre
     * channel while the server plays the rest of it, so nothing is heard twice.
     *
     * [startMs] is a resume point. The engine only *holds* for it: a film the server is still
     * converting is paused on its first frame rather than played from zero while the converter
     * catches up. The seek itself belongs to the ViewModel, so the server's seek counter — and the
     * hifi audio that follows it — sees a resume exactly as it sees a viewer's seek.
     */
    fun open(
        url: String, title: String?, transcoded: Boolean, hifi: Boolean = false,
        centreTrack: Int? = null, startMs: Long = 0L,
    ) {
        this.centreTrack = if (hifi) centreTrack else null
        val mp = ensurePlayer()
        this.url = url
        this.title = title
        this.transcoded = transcoded
        this.resumeMs = startMs
        setAudioEnabled(!hifi)
        status = PlayStatus.Opening
        timeMs = 0L
        lengthMs = 0L
        lengthGrewAt = 0L
        seekable = false
        audioTracks = emptyList()
        spuTracks = emptyList()
        audioTrack = -1
        spuTrack = -1
        userChoseAudio = false
        userChoseSpu = false
        autoApplied = false
        autoAppliedAt = 0L
        spuSeen = 0
        trackNote = null
        // SPIKE: remove after Phase 0 -- a debugRateCycle() run must not carry a non-default rate
        // into the next film.
        if (rate != 1.0f) {
            rate = 1.0f
            mp.setRate(1.0f)
        }
        publish(force = true)

        val media = Media(libVlc!!, Uri.parse(url))
        // MediaCodec with direct rendering. Required for the panel's HDR10 path — without the
        // second flag libVLC copies frames back through software and HDR is lost (and a 4K HEVC
        // stream is far beyond what this CPU could carry anyway).
        media.setHWDecoderEnabled(true, true)
        media.addOption(":network-caching=$networkCachingMs")
        if (transcoded || url.contains("/audio/")) {
            // The server's growing MPEG-TS closes the connection per response and blocks rather
            // than EOF-ing while the converter is suspended; reconnecting is how the demuxer gets
            // past both without treating a short read as the end of the film.
            media.addOption(":http-reconnect")
        }
        applyAudio(mp)
        // Held until there is a window to draw into. The command arrives before the player screen
        // has composed, and android_display waits on a Surface that is not coming yet: VLC sits in
        // Opening for ever, having already parsed the file (it reports a length) but never
        // producing a first frame. The attach is one frame away.
        pending = media
        startPending()
    }

    private fun startPending() {
        val media = pending ?: return
        val mp = player ?: return
        if (attached == null) return
        pending = null
        mp.media = media
        media.release()
        mp.play()
    }

    fun play() {
        val mp = player ?: return
        if (!mp.isPlaying) mp.play()
    }

    fun pause() {
        val mp = player ?: return
        if (mp.isPlaying) mp.pause()
    }

    fun togglePause() {
        val mp = player ?: return
        if (mp.isPlaying) mp.pause() else mp.play()
    }

    /** Stop playback and go back to Idle, keeping the engine alive for the next film. */
    fun stop() {
        pending?.release()
        pending = null
        val mp = player
        if (mp != null && (mp.isPlaying || status !is PlayStatus.Idle)) mp.stop()
        status = PlayStatus.Idle
        timeMs = 0L
        lengthMs = 0L
        seekable = false
        url = null
        title = null
        transcoded = false
        resumeMs = 0L
        audioTracks = emptyList()
        spuTracks = emptyList()
        publish(force = true)
    }

    /** The resume point has been reached, or given up on: stop holding the first frame. */
    fun clearResume() {
        resumeMs = 0L
    }

    /**
     * [force] is for a resume whose caller has already proved the converted length clears the
     * target: the refusal below is about seeking past the live edge, and there is no live edge
     * between here and bytes ffmpeg has already written.
     */
    fun seekTo(ms: Long, force: Boolean = false) {
        val mp = player ?: return
        if (status is PlayStatus.Idle || status is PlayStatus.Ended || status is PlayStatus.Error) return
        val len = mp.length
        val at = mp.time
        if (transcoded && !force && (len <= 0L || lengthProvisional())) {
            // A growing TS has no trustworthy total: the length VLC reports is whatever ffmpeg has
            // written so far, which can be *behind* the playhead — clamping a forward seek to it
            // would jump backwards — and a seek past the real live edge leaves the server's
            // /audio/ loop holding a socket open with nothing to send, which reads as a hung
            // player. Backwards, into bytes that certainly exist, is fine.
            if (ms >= at) {
                say("Still converting the audio — you can't skip ahead yet")
                return
            }
            mp.setTime(ms.coerceAtLeast(0L))
            timeMs = ms.coerceAtLeast(0L)
            publish(force = true)
            return
        }
        if (!mp.isSeekable) {
            say("This stream can't be seeked")
            return
        }
        val ceiling = if (len > 0L) len - 1_000L else 0L
        val target = ms.coerceIn(0L, maxOf(0L, ceiling))
        mp.setTime(target)
        timeMs = target
        publish(force = true)
    }

    /** The length is still climbing, so it is the converted-so-far mark and not the film's. */
    private fun lengthProvisional(): Boolean =
        transcoded && lengthMs > 0L && SystemClock.uptimeMillis() - lengthGrewAt < LENGTH_SETTLE_MS

    fun seekBy(deltaMs: Long) = seekTo((player?.time ?: 0L) + deltaMs)

    /**
     * Where the picture is right now, not where it was at the last TimeChanged event: those fire
     * at their own pace and the published state is throttled on top, so the raw value is up to a
     * few hundred ms stale, and a sync loop fed with it chases that staleness. While playing, the
     * last event is extrapolated at the current rate, capped so a stalled decoder does not read as
     * still advancing.
     */
    fun livePositionMs(): Long {
        if (status != PlayStatus.Playing || timeAt == 0L) return timeMs
        val since = (SystemClock.uptimeMillis() - timeAt).coerceIn(0L, 2_000L)
        return timeMs + (since * rate).toLong()
    }

    /** A track the user chose by hand. From here on this film's tracks are theirs, not ours. */
    fun setAudioTrack(id: Int) {
        userChoseAudio = true
        player?.let { if (it.setAudioTrack(id)) { audioTrack = id; refreshTracks(); publish(force = true) } }
    }

    fun setSpuTrack(id: Int) {
        userChoseSpu = true
        player?.let { if (it.setSpuTrack(id)) { spuTrack = id; refreshTracks(); publish(force = true) } }
    }

    // SPIKE: remove after Phase 0 -- these two methods themselves are kept; see debugRateCycle below.

    /** Debug-only playback speed. No-op if unchanged; reset to 1.0 on the next [open]. */
    fun setRate(rate: Float) {
        if (this.rate == rate) return
        this.rate = rate
        player?.setRate(rate)
    }

    /**
     * Debug-only audio kill switch. When disabled, [autoSelectTracks] refuses to (re-)apply an
     * audio track, so libVLC re-selecting one on an ES event does not undo this.
     */
    fun setAudioEnabled(enabled: Boolean) {
        audioDisabled = !enabled
        if (audioDisabled) player?.let { if (it.audioTrack != -1) it.setAudioTrack(-1) }
    }

    // SPIKE: remove after Phase 0
    /**
     * Disables audio, then every 10s steps through a fixed rate list, logging position at each
     * step; finishes with one seekBy(-400) so the visual cost of a small backward seek can be
     * watched on the panel. Triggered by [rateCycleReceiver] (`adb shell am broadcast -a
     * io.github.superthom196.cinematica.RATECYCLE`); only acts while `verbose` (verboseVlc) is on.
     */
    fun debugRateCycle(start: Boolean) {
        if (!verbose) return
        val handler = rateCycleHandler ?: Handler(Looper.getMainLooper()).also { rateCycleHandler = it }
        rateCycleRunnable?.let { handler.removeCallbacks(it) }
        rateCycleRunnable = null
        if (!start) return

        setAudioEnabled(false)
        val rates = listOf(1.005f, 0.995f, 1.02f, 1.0f)
        var index = 0
        val runnable = object : Runnable {
            override fun run() {
                if (index < rates.size) {
                    val r = rates[index]
                    setRate(r)
                    Log.i(TAG, "ratecycle rate=$r time=${player?.time}")
                    index++
                    rateCycleHandler?.postDelayed(this, 10_000L)
                } else {
                    val before = player?.time
                    seekBy(-400)
                    Log.i(TAG, "ratecycle seek before=$before after=${player?.time}")
                }
            }
        }
        rateCycleRunnable = runnable
        handler.postDelayed(runnable, 10_000L)
    }

    /** Full teardown. The engine is unusable afterwards; build a new one. */
    fun release() {
        // SPIKE: remove after Phase 0
        rateCycleRunnable?.let { rateCycleHandler?.removeCallbacks(it) }
        rateCycleRunnable = null
        if (rateCycleRegistered) {
            context.unregisterReceiver(rateCycleReceiver)
            rateCycleRegistered = false
        }
        releasePlayer()
    }

    /**
     * The player and its LibVLC, and nothing else: what a settings rebuild throws away. The engine
     * itself carries on, so the receiver registered in init stays — ensurePlayer() used to call
     * release() for this, which unregistered it, and the real release() at teardown then threw.
     */
    private fun releasePlayer() {
        pending?.release()
        pending = null
        val mp = player
        player = null
        if (mp != null) {
            mp.setEventListener(null)
            detach()
            if (!mp.isReleased) {
                mp.stop()
                mp.release()
            }
        }
        libVlc?.let { if (!it.isReleased) it.release() }
        libVlc = null
        attached = null
        status = PlayStatus.Idle
        publish(force = true)
    }

    // ---- internals ----------------------------------------------------------

    private fun ensurePlayer(): MediaPlayer {
        // The centre filter only ever changes from open(), which replaces the film anyway; the
        // player screen may already be up (autoplay-next), so its view goes onto the new player.
        var reattach: VLCVideoLayout? = null
        if (player != null && builtCentre != (centreTrack != null)) {
            reattach = attached
            detach()
            releasePlayer()
        }
        if (optionsStale && player != null && !(_state.value.active)) {
            // Caching and verbosity live on the LibVLC instance, so a settings change is honoured
            // by rebuilding — but never underneath a film that is on screen.
            releasePlayer()
            optionsStale = false
        }
        player?.let { return it }
        builtCentre = centreTrack != null
        val vlc = LibVLC(context, vlcOptions())
        libVlc = vlc
        val mp = MediaPlayer(vlc)
        applyAudio(mp)
        mp.setEventListener { ev -> onEvent(ev) }
        player = mp
        optionsStale = false
        reattach?.let {
            mp.attachViews(it, null, true, false)
            attached = it
        }
        return mp
    }

    /** VLC-for-Android's own option list (VLCOptions.kt), trimmed to what this app needs. */
    private fun vlcOptions(): ArrayList<String> {
        val keystore = File(context.getDir("keystore", Context.MODE_PRIVATE), "file_key_store")
        return arrayListOf(
            "--audio-time-stretch",
            "--avcodec-skiploopfilter", "0",
            "--avcodec-skip-frame", "0",
            "--avcodec-skip-idct", "0",
            "--subsdec-encoding", "",
            "--stats",
            "--network-caching=$networkCachingMs",
            "--audio-resampler", "soxr",
            "--freetype-rel-fontsize=16",
            "--freetype-bold",
            "--freetype-color=16777215",
            "--freetype-opacity=255",
            "--freetype-background-opacity=0",
            "--freetype-shadow-opacity=128",
            "--freetype-outline-thickness=4",
            "--freetype-outline-color=0",
            "--freetype-outline-opacity=255",
            "--vout=android_display,none",
            "--keystore", "file_crypt,none",
            "--keystore-file", keystore.absolutePath,
            "--preferred-resolution=-1",
            if (verbose) "-vv" else "-v",
        ).apply { if (builtCentre) addAll(CENTRE_ONLY) }
    }

    /**
     * The libVLC stream id of [centreTrack], when the TV should be playing it: only a stream that
     * has a centre of its own (3+ channels). Stereo and mono stay whole on the Sendspin player —
     * the remap filter would leave nothing of them to play.
     */
    private fun centreAudioId(): Int? {
        val n = centreTrack ?: return null
        val media = player?.media ?: return null
        return try {
            val audio = (0 until media.trackCount).mapNotNull { media.getTrack(it) }
                .filter { it.type == IMedia.Track.Type.Audio }
            val track = audio.getOrNull(n) as? IMedia.AudioTrack
            track?.takeIf { it.channels >= 3 }?.id
        } catch (e: Exception) {
            Log.i(TAG, "centre: could not read media tracks (${e.javaClass.simpleName})")
            null
        } finally {
            media.release()
        }
    }

    private fun onEvent(ev: MediaPlayer.Event) {
        when (ev.type) {
            MediaPlayer.Event.Opening -> {
                status = PlayStatus.Opening
                publish(force = true)
            }
            MediaPlayer.Event.Buffering -> {
                val pct = ev.buffering
                // 100% buffering arrives immediately before (and during) normal playback; only
                // treat a partial fill as "buffering", or the OSD spinner never goes away.
                if (pct < 100f) {
                    status = PlayStatus.Buffering(pct)
                    publish()
                } else if (status is PlayStatus.Buffering) {
                    status = if (player?.isPlaying == true) PlayStatus.Playing else PlayStatus.Buffering(100f)
                    publish(force = true)
                }
            }
            MediaPlayer.Event.Playing -> {
                status = PlayStatus.Playing
                seekable = player?.isSeekable ?: false
                lengthMs = player?.length ?: lengthMs
                // Resuming into a film the converter has not reached yet: hold on the first frame.
                // Playing from the beginning would be the one thing a resume must not do, and the
                // ViewModel releases this the moment the seek can actually land.
                if (resumeMs > 0L && transcoded && (lengthMs <= 0L || lengthProvisional())) player?.pause()
                refreshTracks()
                autoSelectTracks()
                publish(force = true)
            }
            MediaPlayer.Event.Paused -> {
                status = PlayStatus.Paused
                publish(force = true)
            }
            MediaPlayer.Event.Stopped -> {
                if (status !is PlayStatus.Error && status !is PlayStatus.Ended) status = PlayStatus.Idle
                publish(force = true)
            }
            MediaPlayer.Event.EndReached -> {
                status = PlayStatus.Ended
                publish(force = true)
            }
            MediaPlayer.Event.EncounteredError -> {
                status = PlayStatus.Error("could not open stream")
                publish(force = true)
            }
            MediaPlayer.Event.TimeChanged -> {
                timeMs = ev.timeChanged
                timeAt = SystemClock.uptimeMillis()
                publish()
            }
            MediaPlayer.Event.LengthChanged -> {
                if (ev.lengthChanged > lengthMs) lengthGrewAt = SystemClock.uptimeMillis()
                lengthMs = ev.lengthChanged
                seekable = player?.isSeekable ?: seekable
                publish(force = true)
            }
            MediaPlayer.Event.SeekableChanged -> {
                seekable = ev.seekable
                publish(force = true)
            }
            MediaPlayer.Event.ESAdded, MediaPlayer.Event.ESSelected, MediaPlayer.Event.ESDeleted -> {
                refreshTracks()
                autoSelectTracks()
                publish(force = true)
            }
        }
    }

    private fun refreshTracks() {
        val mp = player ?: return
        audioTracks = mp.audioTracks?.map { TrackOption(it.id, it.name ?: "Track ${it.id}") } ?: emptyList()
        spuTracks = mp.spuTracks?.map { TrackOption(it.id, it.name ?: "Track ${it.id}") } ?: emptyList()
        audioTrack = mp.audioTrack
        spuTrack = mp.spuTrack
    }

    /**
     * The one automatic choice per film. See [chooseTracks] for the rules; everything here is
     * about *when* to ask and where the languages come from.
     *
     * The languages come from `IMedia.getTrack(i).language` — the ISO code libVLC reads out of the
     * container per elementary stream — matched onto `MediaPlayer.getAudioTracks()` /
     * `getSpuTracks()` by track id, because those `TrackDescription`s carry only an id and a
     * display name ("Track 1 - [Spanish]"), and that name is the muxer's prose, not data. Where
     * the ids do not line up, the media tracks pair with the descriptions in file order, and the
     * description name is the last resort.
     *
     * Nothing is decided before Playing: the ES list is still filling until then. Subtitle streams
     * can land a moment later still, so the subtitle half gets a second look inside a short window
     * — never over a choice the user has made themselves.
     */
    private fun autoSelectTracks() {
        val mp = player ?: return
        // Hifi: the TV's own audio stays off whatever libVLC reselects on a later ES event. The
        // subtitle decision still runs below -- skipping it left the file's own "default" subtitle
        // track on for an English film -- judged on the file's tracks, since the server plays its
        // preferred-language track out of band.
        // Centre mode is the one exception: the TV plays the server's stream, filtered to its centre.
        if (audioDisabled) {
            val want = centreAudioId() ?: -1
            if (mp.audioTrack != want && !mp.setAudioTrack(want)) Log.i(TAG, "centre: audio track $want was refused")
        }
        if (status !is PlayStatus.Playing && status !is PlayStatus.Paused) return
        if (userChoseAudio && userChoseSpu) return
        val audio = infos(audioTracks, IMedia.Track.Type.Audio)
        if (audio.isEmpty()) return
        val spu = infos(spuTracks, IMedia.Track.Type.Text)
        if (autoApplied) {
            val late = spu.size > spuSeen && !userChoseSpu &&
                SystemClock.uptimeMillis() - autoAppliedAt < AUTO_SETTLE_MS
            if (!late) return
        }
        autoApplied = true
        autoAppliedAt = SystemClock.uptimeMillis()
        spuSeen = spu.size

        val decision = chooseTracks(audio, spu, audioLanguage, subtitleMode, mp.audioTrack)
        Log.i(TAG, "tracks: audio=[${audio.joinToString { describe(it) }}] subs=[${spu.joinToString { describe(it) }}]")
        Log.i(
            TAG,
            "auto-select: prefer '$audioLanguage', subtitles ${subtitleMode.key} -> " +
                "audio ${decision.audioLabel} (id ${decision.audioId ?: mp.audioTrack}" +
                "${if (decision.audioId == null) ", unchanged" else ""}), " +
                "subtitles ${decision.spuLabel} (id ${decision.spuId})",
        )
        if (!userChoseAudio && !audioDisabled) decision.audioId?.let { id ->
            if (id != mp.audioTrack && !mp.setAudioTrack(id)) Log.i(TAG, "auto-select: audio track $id was refused")
        }
        if (!userChoseSpu) decision.spuId?.let { id ->
            if (id != mp.spuTrack && !mp.setSpuTrack(id)) Log.i(TAG, "auto-select: subtitle track $id was refused")
        }
        refreshTracks()
        trackNote = "Audio: ${decision.audioLabel} · Subtitles: ${decision.spuLabel}"
        trackNoteSeq++
    }

    private fun describe(t: TrackInfo): String = "#${t.id} ${t.name} <${t.language ?: "?"}>"

    /**
     * The player's track descriptions, each given the ISO language of the matching elementary
     * stream. [type] is an `IMedia.Track.Type`.
     */
    private fun infos(descriptions: List<TrackOption>, type: Int): List<TrackInfo> {
        val real = descriptions.filter { it.id != -1 }
        if (real.isEmpty()) return emptyList()
        val (byId, inOrder) = mediaLanguages(type)
        return real.mapIndexed { i, d ->
            TrackInfo(d.id, d.name, byId[d.id] ?: inOrder.getOrNull(i).takeIf { inOrder.size == real.size })
        }
    }

    /** ISO languages of this media's streams of [type], by track id and in file order. */
    private fun mediaLanguages(type: Int): Pair<Map<Int, String>, List<String?>> {
        val media = player?.media ?: return emptyMap<Int, String>() to emptyList()
        return try {
            val byId = HashMap<Int, String>()
            val inOrder = ArrayList<String?>()
            for (i in 0 until media.trackCount) {
                val track = media.getTrack(i) ?: continue
                if (track.type != type) continue
                // "und" is the container saying it does not know, which is not a language.
                val lang = track.language?.trim()?.takeIf { it.isNotEmpty() && !it.equals("und", true) }
                inOrder.add(lang)
                if (lang != null) byId[track.id] = lang
            }
            byId to inOrder
        } catch (e: Exception) {
            // A media released underneath us while the film was being torn down; not worth a crash.
            Log.i(TAG, "tracks: could not read media languages (${e.javaClass.simpleName})")
            emptyMap<Int, String>() to emptyList()
        } finally {
            // getMedia() hands back a retained reference.
            media.release()
        }
    }

    /** One line on the OSD. Also how the ViewModel says why a resume is waiting. */
    fun say(msg: String) {
        notice = msg
        noticeSeq++
        publish(force = true)
    }

    /**
     * TimeChanged fires several times a second; anything the UI can see at a glance is throttled to
     * four updates a second. State changes pass [force] and always go out.
     */
    private fun publish(force: Boolean = false) {
        val now = SystemClock.uptimeMillis()
        if (!force && now - lastPublish < 250L) return
        lastPublish = now
        _state.value = PlayerState(
            status = status,
            timeMs = timeMs,
            lengthMs = lengthMs,
            seekable = seekable,
            audioTracks = audioTracks,
            spuTracks = spuTracks,
            audioTrack = audioTrack,
            spuTrack = spuTrack,
            url = url,
            title = title,
            transcoded = transcoded,
            // Long enough that a slow converter is not mistaken for a finished one: VLC only
            // revises the length when the demuxer next learns something.
            lengthProvisional = lengthProvisional(),
            notice = notice,
            noticeSeq = noticeSeq,
            trackNote = trackNote,
            trackNoteSeq = trackNoteSeq,
        )
    }

    private companion object {
        /** AudioTrack, not OpenSL ES: passthrough (`setAudioDigitalOutputEnabled`) only exists here. */
        const val AUDIO_OUTPUT = "audiotrack"

        /**
         * VLC's remap filter with every input channel but the centre sent nowhere (-1), so the
         * TV's speakers carry the centre alone. Each option names an *input* channel and says
         * which output it goes to; 1 is Center (modules/audio_filter/channel_mixer/remap.c).
         */
        val CENTRE_ONLY = listOf(
            "--audio-filter=remap",
            "--aout-remap-channel-center=1",
            "--aout-remap-channel-left=-1",
            "--aout-remap-channel-right=-1",
            "--aout-remap-channel-middleleft=-1",
            "--aout-remap-channel-middleright=-1",
            "--aout-remap-channel-rearleft=-1",
            "--aout-remap-channel-rearright=-1",
            "--aout-remap-channel-rearcenter=-1",
            "--aout-remap-channel-lfe=-1",
        )

        /** How long a length must hold still before it is believed to be the film's real total. */
        const val LENGTH_SETTLE_MS = 45_000L

        /** How long after the first frame a late-arriving subtitle stream still counts as "the film's". */
        const val AUTO_SETTLE_MS = 10_000L

        const val TAG = "Cinematica"

        // SPIKE: remove after Phase 0
        const val ACTION_RATECYCLE = "io.github.superthom196.cinematica.RATECYCLE"
    }
}
