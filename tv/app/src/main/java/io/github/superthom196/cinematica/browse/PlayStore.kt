package io.github.superthom196.cinematica.browse

import android.os.SystemClock
import io.github.superthom196.cinematica.api.CinematicaApi
import io.github.superthom196.cinematica.api.Movie
import io.github.superthom196.cinematica.api.PlayResp
import io.github.superthom196.cinematica.api.friendly
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** A play job the server is working on, as the buffering overlay needs to see it. */
data class PlayJob(
    val id: String,
    val title: String,
    val stage: String? = null,
    val pct: Double = 0.0,
    /** The server's own status line. Shown verbatim: it is written for a human. */
    val msg: String? = null,
    val attempt: Int? = null,
    val attempts: Int? = null,
) {
    /** "Buffering 62%" / "Converting 8%" / "Starting…", by stage. */
    val label: String
        get() = when (stage) {
            "buffering" -> "Buffering ${pct.toInt()}%"
            "encoding" -> "Converting ${pct.toInt()}%"
            "launching" -> "Starting…"
            else -> "Starting…"
        }

    val attemptLabel: String? get() = if (attempt != null && attempts != null) "Trying $attempt/$attempts" else null
}

/**
 * What to POST and what to poll for. [jobId] is the fallback progress key — used only until the
 * server's own reply names the real job (a film's job id has always just been its catalogue id; an
 * episode's play call names its own).
 */
data class PlayTarget(val path: String, val jobId: String, val title: String?)

/**
 * `POST /api/play/{id}` and the progress poll that narrates it.
 *
 * The play command itself does not come back through this call — it arrives over the heartbeat link
 * and `AppViewModel.startPlayback` acts on it, which is what puts the film on screen. This only
 * follows the job so the user can watch it happen and back out of it; [adopt] retires the overlay
 * the moment the player wins, so there is never a second one.
 */
class PlayStore(
    private val scope: CoroutineScope,
    private val api: CinematicaApi,
    private val toast: (text: String, isError: Boolean) -> Unit,
    /** Anything that should be re-read once a job stops, whatever ended it. */
    private val onJobEnded: () -> Unit,
) {
    private val _job = MutableStateFlow<PlayJob?>(null)
    val job: StateFlow<PlayJob?> = _job.asStateFlow()

    private var playGen = 0
    private var pollJob: Job? = null

    /** Buffering legitimately takes minutes on this link (HARD_CAP_SECS is 600, per candidate). */
    private val deadlineMs = 10 * 60 * 1000L
    private val maxMisses = 10

    /** A film from the grid or a search result: same job, its old shape. */
    fun play(movie: Movie) {
        val id = movie.id ?: return
        play(PlayTarget("/api/play/$id", "$id", movie.title))
    }

    fun play(target: PlayTarget) {
        // Two simultaneous plays are not a tested path server-side; one job at a time, always.
        if (_job.value != null) return
        val gen = ++playGen
        _job.value = PlayJob(target.jobId, target.title.orEmpty())
        pollJob = scope.launch {
            val resp = runCatching { postPlay(target.path) }
            if (gen != playGen) return@launch
            val d = resp.getOrNull()
            if (d == null || d.ok != true) {
                val why = d?.msg ?: resp.exceptionOrNull()?.friendly() ?: "unknown"
                finish(gen, "Failed: $why", true)
                return@launch
            }
            // Poll by whatever job id the server actually gave back; target.jobId is only a
            // fallback for a reply that (like a film's today) doesn't name one of its own.
            val pollId = d.job ?: target.jobId
            var misses = 0
            val deadline = SystemClock.elapsedRealtime() + deadlineMs
            while (isActive && gen == playGen) {
                delay(700)
                if (gen != playGen) return@launch
                if (SystemClock.elapsedRealtime() > deadline || misses >= maxMisses) {
                    finish(gen, "Lost track of playback progress — the server may have restarted.", true)
                    return@launch
                }
                val p = runCatching { api.progress(pollId) }.getOrNull()
                // A job the server does not know about answers `{}`; that counts as a miss too.
                if (p?.stage == null) { misses++; continue }
                misses = 0
                _job.update {
                    it?.copy(stage = p.stage, pct = p.pct ?: 0.0, msg = p.msg, attempt = p.attempt, attempts = p.attempts)
                }
                when (p.stage) {
                    // The player is already up: the play command reached us over the heartbeat
                    // before this poll did. Nothing to say, just get out of the way.
                    "playing" -> { finish(gen, null, false); return@launch }
                    "error" -> {
                        val m = p.msg ?: "unknown"
                        // Routine, not a failure: someone started a different film from the phone.
                        if (m.startsWith("Superseded")) finish(gen, "Another movie was started", false)
                        else finish(gen, "Failed: $m", true)
                        return@launch
                    }
                }
            }
        }
    }

    /**
     * CinematicaApi has no generic POST, only one typed call per kind. [PlayTarget.path] is decoded
     * back into whichever call built it rather than adding a `playPath` to the API just for this —
     * the two shapes below (a film, an episode) are the only ones there are.
     */
    private suspend fun postPlay(path: String): PlayResp {
        MOVIE_PATH.find(path)?.let { return api.play(it.groupValues[1]) }
        TV_PATH.find(path)?.let { m ->
            val (id, s, e, auto) = m.destructured
            return api.playTv(id, s.toInt(), e.toInt(), auto == "1")
        }
        error("PlayStore: unrecognised play path $path")
    }

    /** Back on the overlay: stand the job down rather than leave it buffering for nobody. */
    fun cancel() {
        if (_job.value == null) return
        playGen++
        pollJob?.cancel()
        _job.value = null
        scope.launch { runCatching { api.cancel() } }
        onJobEnded()
    }

    /** The play command landed over the heartbeat link: the player wins, the overlay goes. */
    fun adopt() {
        if (_job.value == null) return
        playGen++
        pollJob?.cancel()
        _job.value = null
        onJobEnded()
    }

    private fun finish(gen: Int, message: String?, isError: Boolean) {
        if (gen != playGen) return
        _job.value = null
        if (message != null) toast(message, isError)
        onJobEnded()
    }

    private companion object {
        // The id is opaque and provider-qualified ("cinemeta:tt0903747"), never numeric — only the
        // season and episode segments are. No "/" can appear in an id, so it is safe to match up to
        // the next one.
        val MOVIE_PATH = Regex("""^/api/play/([^/]+)$""")
        val TV_PATH = Regex("""^/api/play/tv/([^/]+)/(\d+)/(\d+)\?autoplay=(\d)$""")
    }
}
