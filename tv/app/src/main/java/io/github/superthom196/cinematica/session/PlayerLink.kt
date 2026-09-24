package io.github.superthom196.cinematica.session

import android.os.SystemClock
import io.github.superthom196.cinematica.api.AppCmd
import io.github.superthom196.cinematica.api.CinematicaApi
import io.github.superthom196.cinematica.api.HeartbeatBody
import io.github.superthom196.cinematica.api.HifiStatus
import io.github.superthom196.cinematica.api.SyncInfo
import io.github.superthom196.cinematica.api.friendly
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** What the heartbeat link is doing, for the status strip. */
sealed interface LinkState {
    data object Off : LinkState
    data object Connected : LinkState
    /** [fails] counts consecutive failed heartbeats, so the UI can wait out a single blip. */
    data class Retrying(val err: String, val fails: Int) : LinkState
}

/** Everything the app reports about itself in one heartbeat. */
data class PlayerReport(
    val state: String,
    val job: String? = null,
    val title: String? = null,
    val positionS: Double? = null,
    val durationS: Double? = null,
    val err: String? = null,
    val hifi: Boolean = false,
    val hifiPlayer: String? = null,
    val hifiDelayMs: Int? = null,
    val seekSeq: Long = 0,
)

/**
 * The one connection this app lives on: `POST /api/player/heartbeat`, our state up and the next
 * command down. The server owns no route to the TV, so the app dials out and keeps a request open.
 *
 * Runs only while the activity is STARTED — [start]/[stop] come from the ViewModel, which the
 * activity drives from onStart/onStop.
 */
class PlayerLink(
    private val scope: CoroutineScope,
    private val api: CinematicaApi,
    private val identity: suspend () -> Triple<String, String, String>,
    private val report: () -> PlayerReport,
    private val onReported: (String) -> Unit,
    private val onPlay: (AppCmd) -> Unit,
    private val onStop: () -> Unit,
    private val onSync: (SyncInfo, Long) -> Unit = { _, _ -> },
    private val onHifiStatus: (HifiStatus?) -> Unit = {},
) {
    private val _link = MutableStateFlow<LinkState>(LinkState.Off)
    val link: StateFlow<LinkState> = _link.asStateFlow()

    private var loop: Job? = null

    /** The seq we are echoing back until the server stops redelivering it. */
    private var pendingAck: Long? = null

    /**
     * The last seq actually acted on, so a redelivery never starts the same film twice.
     *
     * Compared for equality, never for order: the server's seq counter restarts from 1 when the
     * service is restarted, so "only act on a higher seq" quietly ignored every play after a
     * deploy until the counter climbed back past where it had been. Only one command is ever
     * pending, so "not the one I just did" is the whole test that is needed.
     */
    private var lastHandledSeq: Long? = null

    fun start() {
        if (loop?.isActive == true) return
        loop = scope.launch { run() }
    }

    /**
     * Stand the link down. [finalReport], when given, is sent first — with `idle` behind it — so a
     * film torn down as the activity stops still tells the server where it got to rather than
     * leaving the job looking alive until the heartbeat ages out.
     */
    fun stop(finalReport: PlayerReport? = null, onSettled: (() -> Unit)? = null) {
        loop?.cancel()
        loop = null
        _link.value = LinkState.Off
        if (finalReport == null) return
        // Runs on the ViewModel's scope, which outlives onStop; two quick posts, no long poll.
        scope.launch {
            val (id, name, version) = identity()
            val ack = pendingAck
            runCatching {
                api.heartbeat(
                    HeartbeatBody(
                        id = id, name = name, version = version,
                        state = finalReport.state, title = finalReport.title, job = finalReport.job,
                        position_s = finalReport.positionS, duration_s = finalReport.durationS,
                        ack = ack, wait = 0.0, hifi_player = finalReport.hifiPlayer,
                        hifi_delay_ms = finalReport.hifiDelayMs,
                    ),
                )
            }
            runCatching {
                api.heartbeat(HeartbeatBody(id = id, name = name, version = version, state = "idle", wait = 0.0))
            }
            onSettled?.invoke()
        }
    }

    private suspend fun run() {
        val (id, name, version) = identity()
        var backoffMs = 1_000L
        var fails = 0
        while (currentCoroutineContext().isActive) {
            val r = report()
            // While a film is on screen the server wants a position every few seconds, so the
            // request returns at once (wait=0) and the loop paces itself. While idle the request
            // itself is the wait: an 8s long poll, back to back, so a play lands in well under a
            // second without polling all night.
            val busy = r.state == "playing" || r.state == "paused" || r.state == "buffering"
            val wait = if (busy) 0.0 else 8.0
            val body = HeartbeatBody(
                id = id, name = name, version = version,
                state = r.state, title = r.title, job = r.job,
                position_s = r.positionS, duration_s = r.durationS,
                ack = pendingAck, err = r.err, wait = wait, hifi = r.hifi, hifi_player = r.hifiPlayer,
                hifi_delay_ms = r.hifiDelayMs, seek_seq = r.seekSeq,
            )
            val sentAt = SystemClock.uptimeMillis()
            val resp = runCatching { api.heartbeat(body) }
            val rttMs = SystemClock.uptimeMillis() - sentAt
            val value = resp.getOrNull()
            if (value == null) {
                val e = resp.exceptionOrNull()
                fails++
                _link.value = LinkState.Retrying(e?.friendly() ?: "no answer", fails)
                delay(backoffMs)
                backoffMs = (backoffMs * 2).coerceAtMost(8_000L)
                continue
            }
            backoffMs = 1_000L
            fails = 0
            _link.value = LinkState.Connected
            onReported(r.state)

            val cmd = value.cmd
            val seq = cmd?.seq
            // Ack whatever arrived, every time, including a redelivery of something already done:
            // the server clears its pending command the moment it sees the matching ack, and an
            // un-acked command is resent on every heartbeat for ever.
            pendingAck = seq

            val last = lastHandledSeq
            if (cmd == null) {
                // Nothing pending: the server has seen our ack, so there is nothing left to dedupe
                // against. Forget it, or a restarted server counting from 1 again would collide.
                lastHandledSeq = null
            } else if (seq != null && last != null && seq < last) {
                // A restart can hand us a lower seq before any cmd == null heartbeat clears the old
                // one above; a seq going backwards is itself proof of a restart.
                lastHandledSeq = null
            }

            if (cmd != null && seq != null && seq != lastHandledSeq) {
                lastHandledSeq = seq
                when (cmd.type) {
                    // Ack first, then act: the server only trusts our state for a job once it has
                    // seen the ack for that command's seq, so the very next heartbeat must carry
                    // both the ack and state=buffering with the job set. Hence `continue` — no
                    // pacing delay before it goes out.
                    "play" -> if (!cmd.url.isNullOrBlank()) onPlay(cmd) else onStop()
                    "stop" -> onStop()
                }
                continue
            }
            value.sync?.let { onSync(it, rttMs) }
            onHifiStatus(value.hifi_status)
            delay(if (r.hifi && r.state == "playing") 1_000L else if (busy) 3_000L else 200L)
        }
    }
}
