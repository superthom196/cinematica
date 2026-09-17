package io.github.superthom196.cinematica.ui

import android.app.Activity
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.Phase
import io.github.superthom196.cinematica.session.LinkState

@Composable
fun AppRoot(vm: AppViewModel) {
    val ui by vm.ui.collectAsStateWithLifecycle()
    val link by vm.linkState.collectAsStateWithLifecycle()
    val playJob by vm.play.job.collectAsStateWithLifecycle()
    val netState by vm.net.state.collectAsStateWithLifecycle()
    val activity = LocalContext.current as? Activity

    BackHandler {
        // The lock screen has nowhere to go but out.
        if (ui.locked) { activity?.finish(); return@BackHandler }
        // Each screen that has somewhere of its own to go handles Back itself (the grid scrolls to
        // the top, the detail screen cancels a buffering job); this is the fallback beneath them.
        when (ui.phase) {
            is Phase.Settings -> vm.back()
            is Phase.Search -> { vm.search.cancel(); vm.toLibrary() }
            is Phase.Detail -> vm.backFromDetail()
            is Phase.Player -> vm.stopPlayback()
            // Reached from Settings rather than from a cold start: there is a library behind it.
            is Phase.Connect -> if (ui.health != null) vm.toLibrary() else activity?.finish()
            // Nowhere else to go until setup is done elsewhere, same as the top of the library.
            is Phase.SetupRequired -> activity?.finish()
            // Back at the top of the library leaves the app, as it does in every TV launcher.
            else -> activity?.finish()
        }
    }

    Box(Modifier.fillMaxSize().background(CinematicaColors.Background)) {
        // Locked: the keypad and nothing else. No screen beneath it to take focus, no bars, no
        // toasts — the library goes on loading behind it and is there the moment the PIN is right.
        if (ui.locked) {
            PinScreen(vm, ui)
            return@Box
        }
        when (val phase = ui.phase) {
            Phase.Connecting -> LoadingScreen(ui)
            is Phase.Connect -> ConnectScreen(vm, ui)
            Phase.Library -> LibraryScreen(vm, ui)
            is Phase.SetupRequired -> SetupRequiredScreen(phase.address)
            Phase.Search -> SearchScreen(vm)
            is Phase.Detail -> DetailScreen(vm, ui, phase.movie)
            Phase.Settings -> SettingsScreen(vm, ui)
            Phase.Player -> PlayerScreen(vm)
        }
        // The only permanent thing at the bottom of a browsing screen: the server has stopped
        // answering and nothing this app does will work until that is fixed. One blip is not worth
        // a bar, so it waits for two heartbeats in a row (~6s) and clears on the next good one.
        val lost = link as? LinkState.Retrying
        if (lost != null && lost.fails >= 2 && ui.phase !is Phase.Player && ui.phase !is Phase.Connecting && ui.phase !is Phase.Connect) {
            ServerLostBar(ui.host, lost.err, onChangeAddress = { vm.editServer() }, modifier = Modifier.align(Alignment.BottomCenter))
        }
        // Everything else down here is transient by design: a film being prepared, and toasts.
        // No connection status, no now-playing line.
        playJob?.takeIf { ui.phase !is Phase.Player }?.let { job ->
            Row(
                Modifier.align(Alignment.BottomCenter).fillMaxWidth()
                    .background(CinematicaColors.Surface)
                    .padding(horizontal = 24.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                ProgressBar(job.pct, Modifier.width(280.dp), height = 5.dp)
                HSpace(16.dp)
                Text(
                    job.msg.orEmpty(),
                    style = MaterialTheme.typography.bodyMedium,
                    color = CinematicaColors.Text,
                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                )
            }
        }
        // A toast must never paint over the film.
        ui.toast?.takeIf { ui.phase !is Phase.Player }?.let { toast ->
            // Plain (non-focusable) box: a toast must never join the D-pad focus order.
            Box(
                Modifier.align(Alignment.BottomCenter).padding(bottom = 28.dp)
                    .background(if (toast.isError) CinematicaColors.Danger else CinematicaColors.SurfaceHigh, RoundedCornerShape(10.dp))
                    .padding(horizontal = 20.dp, vertical = 12.dp),
            ) {
                Text(toast.text, style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Text)
            }
        }
        // Calibration saturates the link: anything started while it runs would stall and look
        // broken, so it covers everything until the server says it is done.
        if (netState.overlay) CalibrationOverlay(netState.net)
    }
}

/** "The server is gone" — the one persistent message a browsing screen ever shows. */
@Composable
private fun ServerLostBar(host: String, err: String, onChangeAddress: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        modifier.fillMaxWidth().background(CinematicaColors.Surface).padding(horizontal = 32.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        StatusDot(CinematicaColors.Danger)
        HSpace(12.dp)
        Text(
            "Can't reach the server at $host — $err",
            style = MaterialTheme.typography.bodyMedium,
            color = CinematicaColors.Text,
            maxLines = 1,
            modifier = Modifier.weight(1f),
        )
        HSpace(16.dp)
        PillButton("Change address", onClick = onChangeAddress, dense = true)
    }
}

/** The whole screen, while the server measures the link. Indeterminate until it can count streams. */
@Composable
private fun CalibrationOverlay(net: io.github.superthom196.cinematica.api.NetCheck?) {
    val want = net?.cal_want ?: 0
    val done = net?.cal_done ?: 0
    val focusRequester = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { focusRequester.requestFocus() } }
    Box(
        Modifier.fillMaxSize().background(Color(0xE6000000))
            .focusRequester(focusRequester).focusable()
            // Plain Box beneath still owns focus, so the D-pad would drive the hidden screen
            // unless every key but Back (which must still be able to leave the app) is eaten here.
            .onPreviewKeyEvent { it.key != Key.Back },
        contentAlignment = Alignment.Center,
    ) {
        Column(
            Modifier.width(520.dp).background(CinematicaColors.Surface, RoundedCornerShape(16.dp)).padding(28.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text("Calibrating internet connection", style = MaterialTheme.typography.titleLarge)
            VSpace(16.dp)
            if (want > 0) {
                ProgressBar(done * 100.0 / want, Modifier.fillMaxWidth(), height = 5.dp)
                VSpace(10.dp)
                Text("$done of $want streams measured", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
            } else {
                IndeterminateBar(Modifier.fillMaxWidth(), height = 5.dp)
                VSpace(10.dp)
                Text("Testing connection speed", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
            }
        }
    }
}
