package io.github.superthom196.cinematica.ui

import android.os.SystemClock
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameMillis
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AutoNext
import kotlin.math.ceil

/**
 * The countdown after an episode ends: a ring running down to the next one, which the server is
 * finding and buffering meanwhile. Play now skips the wait; Cancel or Back stops it. A dialog, so
 * the series page underneath cannot take the remote's focus while it loads.
 */
@Composable
fun AutoNextDialog(next: AutoNext, onPlayNow: () -> Unit, onCancel: () -> Unit) {
    var now by remember(next.job) { mutableLongStateOf(SystemClock.uptimeMillis()) }
    LaunchedEffect(next.job) {
        while (true) withFrameMillis { now = SystemClock.uptimeMillis() }
    }
    val leftMs = (next.endsAt - now).coerceIn(0L, next.totalMs)
    val fraction = leftMs.toFloat() / next.totalMs
    val playFocus = remember { FocusRequester() }
    LaunchedEffect(next.job) { runCatching { playFocus.requestFocus() } }

    TvDialog(onDismiss = onCancel, width = 480.dp) { armed ->
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(72.dp), contentAlignment = Alignment.Center) {
                Canvas(Modifier.fillMaxSize().rotate(-90f)) {
                    val stroke = 5.dp.toPx()
                    drawArc(
                        color = CinematicaColors.Muted, startAngle = 0f, sweepAngle = 360f, useCenter = false,
                        style = Stroke(width = stroke), alpha = 0.35f,
                    )
                    drawArc(
                        color = CinematicaColors.AccentBright, startAngle = 0f, sweepAngle = 360f * fraction,
                        useCenter = false, style = Stroke(width = stroke, cap = StrokeCap.Round),
                    )
                }
                Text("${ceil(leftMs / 1000.0).toInt()}", style = MaterialTheme.typography.titleLarge)
            }
            HSpace(18.dp)
            Column {
                Text("Up next", style = MaterialTheme.typography.labelMedium, color = CinematicaColors.Muted)
                VSpace(2.dp)
                Text(
                    next.label, style = MaterialTheme.typography.titleMedium,
                    maxLines = 2, overflow = TextOverflow.Ellipsis,
                )
            }
        }
        VSpace(16.dp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            PillButton(
                "Play now", onClick = { if (armed()) onPlayNow() }, primary = true, dense = true,
                modifier = Modifier.focusRequester(playFocus),
            )
            PillButton("Cancel", onClick = { if (armed()) onCancel() }, dense = true)
        }
    }
}
