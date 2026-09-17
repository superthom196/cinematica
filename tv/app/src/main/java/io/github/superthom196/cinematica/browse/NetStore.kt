package io.github.superthom196.cinematica.browse

import io.github.superthom196.cinematica.api.CinematicaApi
import io.github.superthom196.cinematica.api.NetCheck
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

data class NetState(
    val net: NetCheck? = null,
    /** Calibration saturates the link, so it blocks the whole UI while it runs. */
    val overlay: Boolean = false,
)

/**
 * The ⚡ chip and the calibration overlay.
 *
 * `GET /api/netcheck` is read on launch, whenever the chip is opened and after a play job ends, so
 * the number on the chip is never older than the last thing that could have changed it. While the
 * server is calibrating the read repeats every 2s and the overlay stays up; a *failed* poll re-arms
 * the same 2s timer rather than ending the chain — a poll is likeliest to fail exactly while the
 * calibration is saturating the link it is polled over.
 */
class NetStore(
    private val scope: CoroutineScope,
    private val api: CinematicaApi,
    private val toast: (text: String, isError: Boolean) -> Unit,
) {
    private val _state = MutableStateFlow(NetState())
    val state: StateFlow<NetState> = _state.asStateFlow()

    private var pollJob: Job? = null
    private var sawCalibration = false

    fun refresh() {
        if (pollJob?.isActive == true) return
        pollJob = scope.launch {
            // Consecutive failed polls while the overlay is up: if the server dies mid-calibration
            // it never answers again, so give up on the overlay after ~20s rather than hanging forever.
            var misses = 0
            while (isActive) {
                val d = runCatching { api.netcheck() }.getOrNull()
                if (d == null) {
                    if (!_state.value.overlay) return@launch
                    if (++misses >= 10) {
                        _state.update { it.copy(overlay = false) }
                        toast("Lost the server during calibration", true)
                        return@launch
                    }
                    delay(2_000)
                    continue
                }
                misses = 0
                _state.update { it.copy(net = d) }
                if (d.calibrating == true || d.busy == true) {
                    sawCalibration = true
                    _state.update { it.copy(overlay = true) }
                    delay(2_000)
                    continue
                }
                _state.update { it.copy(overlay = false) }
                if (sawCalibration) {
                    sawCalibration = false
                    toast(
                        "Link ${num(d.http_mbps)} Mbps · budget ${num(d.mbps)} Mbps " +
                            "(${d.source ?: "?"}) · ${d.conns ?: "?"} connections",
                        false,
                    )
                }
                return@launch
            }
        }
    }

    /** "Re-measure now": discards every learned sample and starts again from nothing. */
    fun remeasure() {
        scope.launch {
            val r = runCatching { api.netcheckStart() }.getOrNull()
            if (r == null) {
                toast("Link test failed", true)
                return@launch
            }
            // 409 while a film is playing, or "already running": the server's own words, which are
            // written for a human and say more than anything this app could add.
            if (r.ok != true || r.msg == "already running") {
                toast(r.msg ?: "Link test failed", r.ok != true)
            } else {
                toast("Optimising your connection… measuring real download speeds.", false)
            }
            refresh()
        }
    }

    /** The chip's label: the live budget, or how far through a calibration the server is. */
    // Takes the caller's collected state rather than reading the flow itself:
    // a StateFlow read is invisible to Compose, so a label built from it only
    // refreshed when something else in the same scope happened to change --
    // the chip sat on "Optimising… 0/4" long after the calibration finished.
    fun chipLabel(d: NetCheck? = _state.value.net): String {
        if (d?.calibrating == true) {
            val want = d.cal_want ?: 0
            return "⚡ Optimising…" + if (want > 0) " ${d.cal_done ?: 0}/$want" else ""
        }
        return "⚡ ${num(d?.sustain_live)} Mbps"
    }

    private fun num(v: Double?): String {
        if (v == null) return "?"
        return if (v == v.toLong().toDouble()) v.toLong().toString() else v.toString()
    }
}
