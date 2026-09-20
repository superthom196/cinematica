package io.github.superthom196.cinematica.ui

import android.app.Activity
import android.view.WindowManager
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.focusable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.api.HifiStatus
import io.github.superthom196.cinematica.api.Pick
import io.github.superthom196.cinematica.player.PlayStatus
import io.github.superthom196.cinematica.player.PlayerState
import io.github.superthom196.cinematica.player.TrackOption
import org.videolan.libvlc.util.VLCVideoLayout

/** Mirrors the phone's `n.gb+'GB'`: whole numbers with no decimal, otherwise trimmed. */
private fun formatGb(gb: Double): String {
    if (gb == gb.toLong().toDouble()) return gb.toLong().toString()
    return "%.2f".format(gb).trimEnd('0').trimEnd('.')
}

private const val OSD_MS = 4_000L
private const val SEEK_STEP_MS = 10_000L
private const val SEEK_COMMIT_MS = 350L

// Mirrors server/shelf.py. Below PIN_FROM a stop is an early bail and nothing is kept; at DONE_AT
// the film counts as watched. Between the two the server is about to pin it and go on offering it,
// which is the only moment worth asking the viewer about.
private const val PIN_FROM = 0.20f
private const val DONE_AT = 0.90f

/** Whether a stop here is the kind the server would pin. An unknown or still-growing total is not. */
private fun inPinBand(st: PlayerState): Boolean {
    if (st.lengthMs <= 0L || st.isLive) return false
    val at = st.timeMs.toFloat() / st.lengthMs
    return at >= PIN_FROM && at < DONE_AT
}

/**
 * The film, full screen, with an OSD that comes up on any key and goes away again.
 *
 * Every key is handled here rather than through focusable widgets: a player has one focus target
 * and a fixed key map, and routing D-pad presses through Compose focus would put the seek bar and
 * the track list in the same navigation order as the transport.
 */
@Composable
fun PlayerScreen(vm: AppViewModel) {
    val st by vm.player.collectAsStateWithLifecycle()
    val ui by vm.ui.collectAsStateWithLifecycle()
    // Whatever the server picked, for the OSD's second line.
    val pick = ui.playPick
    val hifi = ui.hifi
    val syncErrMs by vm.lastSyncErrMs.collectAsStateWithLifecycle()
    val hifiStatus by vm.hifiStatus.collectAsStateWithLifecycle()
    val hifiDelayMs by vm.hifiDelayMs.collectAsStateWithLifecycle()
    val ping by vm.osdPing.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val activity = context as? Activity

    var keyTick by remember { mutableLongStateOf(0L) }
    var osdVisible by remember { mutableStateOf(true) }
    // The lip-sync bar takes over left/right while it is up, so it is only ever opened for a hifi
    // film, and it closes itself once the viewer stops adjusting.
    var lipSync by remember { mutableStateOf(false) }
    var picker by remember { mutableStateOf(false) }
    var pickerIndex by remember { mutableIntStateOf(0) }
    var backArmed by remember { mutableStateOf(false) }
    // The two-row prompt the Back that would stop playback opens, part-way through a film.
    var stopPrompt by remember { mutableStateOf(false) }
    var stopIndex by remember { mutableIntStateOf(0) }
    var seekTarget by remember { mutableStateOf<Long?>(null) }
    var noticeVisible by remember { mutableStateOf(false) }
    var trackNoteVisible by remember { mutableStateOf(false) }

    val focus = remember { FocusRequester() }
    LaunchedEffect(Unit) { focus.requestFocus() }

    // The OSD is a 4s window that any key press (including the media keys the activity swallowed)
    // restarts. The picker keeps it up for as long as it is open.
    LaunchedEffect(keyTick, ping, picker) {
        osdVisible = true
        if (!picker) {
            kotlinx.coroutines.delay(OSD_MS)
            osdVisible = false
        }
    }
    LaunchedEffect(backArmed) {
        if (backArmed) {
            kotlinx.coroutines.delay(3_000L)
            backArmed = false
        }
    }
    LaunchedEffect(st.noticeSeq) {
        if (st.noticeSeq > 0L) {
            noticeVisible = true
            osdVisible = true
            kotlinx.coroutines.delay(3_000L)
            noticeVisible = false
        }
    }
    LaunchedEffect(lipSync, keyTick) {
        if (lipSync) {
            kotlinx.coroutines.delay(6_000L)
            lipSync = false
        }
    }
    // What the automatic track choice did, said once and briefly: the viewer should know why they
    // are hearing English and reading nothing, without having to open the picker to find out.
    LaunchedEffect(st.trackNoteSeq) {
        if (st.trackNoteSeq > 0L) {
            trackNoteVisible = true
            kotlinx.coroutines.delay(3_000L)
            trackNoteVisible = false
        }
    }
    // Key repeat on LEFT/RIGHT stacks up ±10s a press and commits once the user stops, so holding
    // the key scrubs instead of firing a seek per repeat.
    LaunchedEffect(seekTarget) {
        val target = seekTarget ?: return@LaunchedEffect
        kotlinx.coroutines.delay(SEEK_COMMIT_MS)
        vm.seekTo(target)
        seekTarget = null
    }
    // A film on screen must not let the TV dim or the screensaver in.
    LaunchedEffect(st.active) {
        val window = activity?.window ?: return@LaunchedEffect
        if (st.active) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }
    // Leaving the screen while active never sees st.active flip to false first, so the LaunchedEffect
    // above never clears the flag; clear it unconditionally on dispose.
    DisposableEffect(Unit) {
        onDispose { activity?.window?.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) }
    }

    fun nudge(delta: Long) {
        if (st.isLive) {
            // Unknown length: let the engine answer with why, rather than previewing a seek that
            // is about to be refused.
            vm.seekBy(delta)
            return
        }
        val from = seekTarget ?: st.timeMs
        val max = if (st.lengthMs > 0L) st.lengthMs else from + delta
        seekTarget = (from + delta).coerceIn(0L, maxOf(0L, max))
    }

    val tracks = remember(st.audioTracks, st.spuTracks, st.audioTrack, st.spuTrack, hifi) { pickerRows(st, hifi) }

    Box(
        Modifier
            .fillMaxSize()
            .background(Color.Black)
            .focusRequester(focus)
            .focusable()
            .onPreviewKeyEvent { ev ->
                if (ev.type != KeyEventType.KeyDown) {
                    // Back's key-up must be swallowed too, or the activity's BackHandler acts on it
                    // after this screen has already dealt with the press.
                    return@onPreviewKeyEvent ev.key == Key.Back
                }
                keyTick++
                if (stopPrompt) {
                    when (ev.key) {
                        Key.DirectionUp -> { stopIndex = 0; true }
                        Key.DirectionDown -> { stopIndex = 1; true }
                        Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> {
                            stopPrompt = false
                            if (stopIndex == 1) vm.dropAndStop() else vm.stopPlayback()
                            true
                        }
                        // Backing out of the question is not an answer: the film carries on.
                        Key.Back -> { stopPrompt = false; true }
                        else -> true
                    }
                } else if (picker) {
                    when (ev.key) {
                        Key.DirectionUp -> { pickerIndex = (pickerIndex - 1).coerceAtLeast(0); true }
                        Key.DirectionDown -> { pickerIndex = (pickerIndex + 1).coerceAtMost(maxOf(0, tracks.lastIndex)); true }
                        Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> {
                            tracks.getOrNull(pickerIndex)?.let { row ->
                                if (row.audio) vm.setAudioTrack(row.id) else vm.setSpuTrack(row.id)
                            }
                            true
                        }
                        Key.Back -> { picker = false; true }
                        else -> true
                    }
                } else if (lipSync) {
                    when (ev.key) {
                        // Left and right trim the sound instead of seeking, for as long as the bar
                        // is up: dialling lip sync in means pressing these many times over.
                        Key.DirectionLeft -> { vm.adjustHifiDelay(-1); true }
                        Key.DirectionRight -> { vm.adjustHifiDelay(1); true }
                        else -> { lipSync = false; ev.key == Key.Back || ev.key == Key.DirectionCenter }
                    }
                } else when (ev.key) {
                    Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> { vm.togglePause(); true }
                    Key.DirectionLeft -> { nudge(-SEEK_STEP_MS); true }
                    Key.DirectionRight -> { nudge(SEEK_STEP_MS); true }
                    Key.DirectionUp -> {
                        if (tracks.isNotEmpty()) {
                            pickerIndex = tracks.indexOfFirst { it.selected }.coerceAtLeast(0)
                            picker = true
                        }
                        true
                    }
                    Key.DirectionDown -> { if (hifi) lipSync = true; true }
                    Key.Back -> {
                        val finished = st.status is PlayStatus.Ended || st.status is PlayStatus.Error ||
                            st.status is PlayStatus.Idle
                        when {
                            // Stopping part-way through is the one stop the server acts on, so it
                            // is the one worth a question. Everywhere else Back is what it was.
                            (finished || backArmed) && inPinBand(st) -> { backArmed = false; stopIndex = 0; stopPrompt = true }
                            finished || backArmed -> vm.stopPlayback()
                            else -> backArmed = true
                        }
                        true
                    }
                    else -> false
                }
            },
    ) {
        AndroidView(
            factory = { ctx -> VLCVideoLayout(ctx).also { vm.attachSurface(it) } },
            onRelease = { vm.detachSurface() },
            modifier = Modifier.fillMaxSize(),
        )

        (st.status as? PlayStatus.Buffering)?.let { Spinner(it.pct) }
        if (st.status is PlayStatus.Opening) Spinner(null)

        (st.status as? PlayStatus.Error)?.let { err ->
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text("Couldn't play this movie", style = MaterialTheme.typography.headlineSmall, color = CinematicaColors.Danger)
                    VSpace(8.dp)
                    Text(err.msg, style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
                    VSpace(16.dp)
                    Text("Press Back to return", style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
                }
            }
        }

        if (osdVisible || picker) Osd(st, pick, seekTarget, backArmed, noticeVisible, hifi, syncErrMs, hifiStatus)
        if (lipSync) LipSyncBar(hifiDelayMs, Modifier.align(Alignment.BottomCenter))
        if (picker) TrackPicker(tracks, pickerIndex, Modifier.align(Alignment.CenterEnd))
        if (stopPrompt) StopPrompt(stopIndex, Modifier.align(Alignment.Center))
        // Audio that failed to start is said even with the OSD down: a silent film must never
        // pass for a quiet one.
        val failed = hifiStatus?.takeIf { hifi && it.state == "failed" }
        if (failed != null && !osdVisible && !picker) {
            Box(Modifier.align(Alignment.TopEnd).padding(24.dp)) { Chip(hifiChipText(failed), CinematicaColors.Warn) }
        }
        // Above the transport band, so it reads whether or not the OSD is up.
        val trackNote = st.trackNote
        if (trackNoteVisible && trackNote != null) {
            Box(
                Modifier.align(Alignment.BottomCenter).padding(bottom = 132.dp)
                    .background(Color(0xCC000000), RoundedCornerShape(10.dp))
                    .padding(horizontal = 18.dp, vertical = 10.dp),
            ) {
                Text(trackNote, style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.AccentBright)
            }
        }
    }
}

/** Title, clock, seek bar and the state chips, over a gradient-free translucent band. */
@Composable
private fun Osd(
    st: PlayerState,
    pick: Pick?,
    seekTarget: Long?,
    backArmed: Boolean,
    noticeVisible: Boolean,
    hifi: Boolean,
    syncErrMs: Long?,
    hifiStatus: HifiStatus?,
) {
    val shown = seekTarget ?: st.timeMs
    // No bar at all while the total is provisional: a bar that reads 95% of a film you are one
    // minute into is worse than no bar.
    val fraction = if (st.lengthMs > 0L && !st.isLive) (shown.toDouble() / st.lengthMs).coerceIn(0.0, 1.0).toFloat() else 0f
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().background(Color(0xCC000000)).padding(horizontal = 40.dp, vertical = 18.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(
                    st.title ?: "Playing",
                    style = MaterialTheme.typography.titleLarge,
                    color = CinematicaColors.Text,
                    maxLines = 1,
                )
                // What the server picked, in the server's own words.
                val detail = buildString {
                    pick?.tag?.let { append(it) }
                    pick?.gb?.let { if (isNotEmpty()) append(" · "); append(formatGb(it)); append("GB") }
                    pick?.audio?.takeIf { it != "?" }?.let {
                        if (isNotEmpty()) append(" · ")
                        append(it.uppercase())
                        if (st.transcoded) append("→AC3")
                    }
                }
                if (detail.isNotEmpty()) {
                    Text(detail, style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted, maxLines = 1)
                }
            }
            if (st.isLive && !hifi) Chip("Converting audio…", CinematicaColors.Accent)
            if (hifi) {
                when (hifiStatus?.state) {
                    "failed" -> { HSpace(8.dp); Chip(hifiChipText(hifiStatus), CinematicaColors.Warn) }
                    "starting" -> { HSpace(8.dp); Chip("Network audio · starting…", CinematicaColors.Accent) }
                    else -> if (syncErrMs != null) { HSpace(8.dp); Chip("Network audio · Δ $syncErrMs ms", CinematicaColors.Accent) }
                }
            }
            if (st.status is PlayStatus.Paused) { HSpace(8.dp); Chip("Paused", CinematicaColors.Warn) }
        }
        Box(Modifier.fillMaxWidth().weight(1f)) {
            if (backArmed) {
                Box(Modifier.align(Alignment.Center).background(Color(0xCC000000), RoundedCornerShape(10.dp)).padding(horizontal = 22.dp, vertical = 14.dp)) {
                    Text("Press Back again to stop", style = MaterialTheme.typography.titleMedium, color = CinematicaColors.Text)
                }
            }
            if (noticeVisible && st.notice != null) {
                Box(
                    Modifier.align(Alignment.BottomCenter).background(Color(0xCC000000), RoundedCornerShape(10.dp))
                        .padding(horizontal = 18.dp, vertical = 10.dp),
                ) {
                    Text(st.notice, style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Warn)
                }
            }
        }
        Column(Modifier.fillMaxWidth().background(Color(0xCC000000)).padding(horizontal = 40.dp, vertical = 18.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    // A growing /audio/ file has no total until ffmpeg finishes, so there is
                    // genuinely nothing to count down to: say "live" rather than invent a duration.
                    "${formatTime(shown / 1000.0)} / ${if (st.isLive) "live" else formatTime(st.lengthMs / 1000.0)}",
                    style = MaterialTheme.typography.bodyMedium,
                    color = CinematicaColors.Text,
                )
                if (seekTarget != null) {
                    HSpace(10.dp)
                    val delta = ((seekTarget - st.timeMs) / 1000.0).toInt()
                    Text(if (delta >= 0) "+${delta}s" else "${delta}s", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.AccentBright)
                }
                androidx.compose.foundation.layout.Spacer(Modifier.weight(1f))
                Text(
                    if (hifi) "OK pause · ◀▶ 10s · ▲ tracks · ▼ lip sync · Back stop"
                    else "OK pause · ◀▶ 10s · ▲ tracks · Back stop",
                    style = MaterialTheme.typography.bodySmall,
                    color = CinematicaColors.Muted,
                )
            }
            VSpace(10.dp)
            Box(Modifier.fillMaxWidth().height(4.dp).clip(RoundedCornerShape(2.dp)).background(Color(0x44FFFFFF))) {
                if (fraction > 0f) {
                    Box(Modifier.fillMaxWidth(fraction).fillMaxHeight().background(CinematicaColors.Accent))
                }
            }
        }
    }
}

/**
 * The lip-sync trim, while the viewer is setting it. Positive means the sound is heard later, the
 * sign an AV receiver uses for "audio delay"; the change follows about a second and a half behind
 * the press, which is how long the audio already at the DAC lasts.
 */
@Composable
private fun LipSyncBar(delayMs: Int, modifier: Modifier = Modifier) {
    Column(
        modifier.padding(bottom = 132.dp)
            .background(Color(0xE6000000), RoundedCornerShape(12.dp))
            .padding(horizontal = 28.dp, vertical = 16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("Lip sync", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
        VSpace(4.dp)
        Text(
            (if (delayMs > 0) "+" else "") + "$delayMs ms",
            style = MaterialTheme.typography.headlineSmall,
            color = CinematicaColors.AccentBright,
        )
        VSpace(6.dp)
        Text(
            if (delayMs == 0) "◀ sound earlier · sound later ▶"
            else if (delayMs > 0) "sound is held back $delayMs ms" else "sound runs ${-delayMs} ms early",
            style = MaterialTheme.typography.bodySmall,
            color = CinematicaColors.Muted,
        )
    }
}

private fun hifiChipText(status: HifiStatus): String =
    "Network audio failed" + (status.msg?.let { " · $it" } ?: "")

@Composable
private fun Chip(text: String, color: Color) {
    Box(Modifier.background(color.copy(alpha = 0.22f), RoundedCornerShape(50)).border(1.dp, color, RoundedCornerShape(50)).padding(horizontal = 12.dp, vertical = 5.dp)) {
        Text(text, style = MaterialTheme.typography.labelMedium, color = color)
    }
}

/** One row of the track picker: audio tracks first, then subtitles (including "off"). */
private data class PickRow(val id: Int, val label: String, val audio: Boolean, val selected: Boolean, val header: String? = null)

private fun pickerRows(st: PlayerState, hifi: Boolean = false): List<PickRow> {
    val rows = ArrayList<PickRow>(st.audioTracks.size + st.spuTracks.size + 1)
    // Hifi audio comes from the server out of band, muted on the TV's own decode, so there is
    // nothing here for the viewer to pick between.
    if (!hifi) {
        st.audioTracks.forEachIndexed { i, t: TrackOption ->
            rows.add(PickRow(t.id, t.name, audio = true, selected = t.id == st.audioTrack, header = if (i == 0) "Audio" else null))
        }
    }
    val spu = st.spuTracks.filter { it.id != -1 }
    rows.add(PickRow(-1, "Off", audio = false, selected = st.spuTrack == -1, header = "Subtitles"))
    spu.forEach { t -> rows.add(PickRow(t.id, t.name, audio = false, selected = t.id == st.spuTrack)) }
    return rows
}

@Composable
private fun TrackPicker(rows: List<PickRow>, index: Int, modifier: Modifier = Modifier) {
    Box(modifier.padding(end = 40.dp).width(340.dp).background(Color(0xEE16161C), RoundedCornerShape(14.dp)).padding(vertical = 12.dp)) {
        Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            rows.forEachIndexed { i, row ->
                row.header?.let {
                    SectionLabel(it, Modifier.padding(start = 16.dp, top = if (i == 0) 0.dp else 12.dp, bottom = 4.dp))
                }
                Row(
                    Modifier.fillMaxWidth()
                        .then(if (i == index) Modifier.background(CinematicaColors.SurfaceHigh) else Modifier)
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        row.label,
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (i == index) CinematicaColors.Text else CinematicaColors.Muted,
                        maxLines = 1,
                        modifier = Modifier.weight(1f),
                    )
                    if (row.selected) Text("●", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.AccentBright)
                }
            }
        }
    }
}

/**
 * Stopping part-way through: keep the place, or be done with it. Same panel as the track picker,
 * centred, and the safe answer is the one already under the cursor.
 */
@Composable
private fun StopPrompt(index: Int, modifier: Modifier = Modifier) {
    Box(modifier.width(340.dp).background(Color(0xEE16161C), RoundedCornerShape(14.dp)).padding(vertical = 12.dp)) {
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            SectionLabel("Stopping", Modifier.padding(start = 16.dp, bottom = 4.dp))
            listOf("Keep my place", "Done with this").forEachIndexed { i, label ->
                Row(
                    Modifier.fillMaxWidth()
                        .then(if (i == index) Modifier.background(CinematicaColors.SurfaceHigh) else Modifier)
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        label,
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (i == index) CinematicaColors.Text else CinematicaColors.Muted,
                        maxLines = 1,
                    )
                }
            }
        }
    }
}

/** A plain rotating arc: tv-material has no progress indicator and material3 is not a dependency. */
@Composable
private fun Spinner(pct: Float?) {
    val spin = rememberInfiniteTransition(label = "spin")
    val angle by spin.animateFloat(
        initialValue = 0f,
        targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(900, easing = LinearEasing), RepeatMode.Restart),
        label = "angle",
    )
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Canvas(Modifier.size(52.dp).rotate(angle)) {
                drawArc(
                    color = CinematicaColors.AccentBright,
                    startAngle = 0f,
                    sweepAngle = 100f,
                    useCenter = false,
                    size = Size(size.width, size.height),
                    style = Stroke(width = 5f),
                )
            }
            VSpace(14.dp)
            Text(
                if (pct != null && pct > 0f) "Buffering ${pct.toInt()}%" else "Opening stream…",
                style = MaterialTheme.typography.bodyLarge,
                color = CinematicaColors.Text,
            )
        }
    }
}
