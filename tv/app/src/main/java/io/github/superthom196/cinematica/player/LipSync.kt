package io.github.superthom196.cinematica.player

import kotlin.math.abs

/**
 * What the player should do about lip sync, decided once per heartbeat's sync verdict.
 *
 * Sign convention (matches the server): `errS` = tv_position_s − audio_pos_s. Positive means the
 * picture is ahead of the audio the server is streaming out of band, so the TV must slow down (or
 * seek backwards) to let the audio catch up; negative means the picture is behind, so the TV must
 * speed up (or seek forwards).
 */
sealed class SyncAction {
    object None : SyncAction()
    data class Rate(val rate: Float) : SyncAction()
    data class Seek(val deltaMs: Long) : SyncAction()
}

/**
 * Turns the server's periodic hifi (Sendspin) sync verdict into a playback nudge. Pure Kotlin, no
 * Android imports, so it can be unit tested without a device.
 *
 * The server computes `err_s` from the TV position we last sent it, which is already stale by
 * about half a round trip by the time the verdict comes back, so [step] corrects for that first:
 * `trueErr = errS + rttMs / 2000.0`.
 *
 * Single verdicts are noisy (a few hundred ms either way from one second to the next), and a 4K
 * hardware decoder visibly stutters on every rate change, so decisions are made on the median of
 * the last [WINDOW] verdicts, inside a dead band, and a rate once applied is held for at least
 * [HOLD_MS] before it can change again — the picture drifting by tens of ms for a few seconds is
 * invisible; the decoder hiccuping every second is not.
 *
 * Nothing is corrected for [SUPPRESS_MS] after [onOpen], [onUserSeek], [onResume], or a change of
 * `gen` (the server bumps `gen` whenever it restarts the audio stream) — in every one of those
 * cases the verdict that arrives next was computed against a position that no longer means
 * anything, so it is discarded rather than acted on. A seek of our own suppresses for longer
 * ([SEEK_SETTLE_MS]), because the position reported while the decoder works forward from the
 * keyframe it landed on is a few seconds behind where the picture will settle.
 */
class LipSync {
    private var suppressUntil = 0L
    private var lastGen: Int? = null
    private var currentRate = 1f
    private var rateSince = 0L
    private val recent = ArrayDeque<Double>()

    private fun suppressFrom(nowMs: Long, forMs: Long = SUPPRESS_MS) {
        suppressUntil = nowMs + forMs
        recent.clear()
    }

    /** A film just started (or restarted): the next verdict or two will be against stale data. */
    fun onOpen(nowMs: Long) {
        suppressFrom(nowMs)
        currentRate = 1f
        rateSince = 0L
    }

    /** The viewer scrubbed by hand: whatever the server was tracking no longer applies. */
    fun onUserSeek(nowMs: Long) = suppressFrom(nowMs)

    /** Playback resumed from pause: the audio side needs a moment to catch back up. */
    fun onResume(nowMs: Long) = suppressFrom(nowMs)

    /** Back to a clean slate, e.g. when playback stops. */
    fun reset() {
        suppressUntil = 0L
        lastGen = null
        currentRate = 1f
        rateSince = 0L
        recent.clear()
    }

    /**
     * @param gen the server's audio-stream generation, from [io.github.superthom196.cinematica.api.SyncInfo].
     * @param errS the raw `err_s` from the server, before the RTT correction.
     * @param rttMs the round trip time of the heartbeat this verdict rode in on.
     * @param nowMs [android.os.SystemClock.uptimeMillis] at the call site.
     */
    fun step(gen: Int, errS: Double, rttMs: Long, nowMs: Long): SyncAction {
        if (!errS.isFinite() || rttMs !in 0..2_000) return SyncAction.None
        if (gen != lastGen) {
            // First-seen gen counts as a change too: we have no history for it yet either.
            lastGen = gen
            suppressFrom(nowMs)
            val changed = currentRate != 1f
            currentRate = 1f
            rateSince = 0L
            return if (changed) SyncAction.Rate(1f) else SyncAction.None
        }
        if (nowMs < suppressUntil) return SyncAction.None

        val trueErr = errS + rttMs / 2000.0
        recent.addLast(trueErr)
        while (recent.size > WINDOW) recent.removeFirst()

        // Far out: a single reading is enough, waiting for a window only prolongs the gap.
        val e1 = abs(trueErr)
        if (e1 >= MIN_SEEK_S) {
            suppressFrom(nowMs, SEEK_SETTLE_MS)
            currentRate = 1f
            rateSince = 0L
            return SyncAction.Seek((-trueErr * 1000).toLong())
        }

        if (recent.size < WINDOW) return SyncAction.None
        val med = recent.sorted()[WINDOW / 2]
        val e = abs(med)
        val slower = med > 0
        val wanted = when {
            e >= 0.25 -> if (slower) 1f - 0.02f else 1f + 0.02f
            e >= DEAD_BAND_S -> if (slower) 1f - 0.01f else 1f + 0.01f
            else -> 1f
        }
        if (wanted == currentRate) return SyncAction.None
        // Back to normal speed is cheap and means we are nearly there; a new correction waits.
        val hold = if (wanted == 1f) RELEASE_MS else HOLD_MS
        if (rateSince != 0L && nowMs - rateSince < hold) return SyncAction.None
        currentRate = wanted
        rateSince = nowMs
        return SyncAction.Rate(wanted)
    }

    private companion object {
        const val SUPPRESS_MS = 3_000L
        const val SEEK_SETTLE_MS = 6_000L
        const val WINDOW = 5
        const val HOLD_MS = 8_000L
        const val RELEASE_MS = 3_000L
        const val DEAD_BAND_S = 0.04
        const val MIN_SEEK_S = 1.0
    }
}
