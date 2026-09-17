package io.github.superthom196.cinematica.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.Phase
import io.github.superthom196.cinematica.UiState

@Composable
fun LoadingScreen(ui: UiState) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Wordmark(logoSize = 48.dp)
            VSpace(16.dp)
            Text("Connecting to ${ui.host}…", style = MaterialTheme.typography.bodyLarge, color = CinematicaColors.Muted)
        }
    }
}

/**
 * The server answered, but `/api/health` says no provider is configured yet — a fresh install with
 * nothing chosen for a catalogue or a stream source. The library has nothing to show and nothing to
 * poll for here, so this replaces it outright rather than spinning or looking broken; health polling
 * moves the app on to [Phase.Library] on its own once setup is done, from wherever it was left.
 */
@Composable
fun SetupRequiredScreen(address: String) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Wordmark(logoSize = 48.dp)
            VSpace(16.dp)
            Text("Cinematica needs setting up", style = MaterialTheme.typography.titleLarge)
            VSpace(12.dp)
            Text("Open $address in a browser", style = MaterialTheme.typography.bodyLarge, color = CinematicaColors.Muted)
            Text(
                "to choose a catalogue and a stream source.",
                style = MaterialTheme.typography.bodyLarge, color = CinematicaColors.Muted,
            )
        }
    }
}

/**
 * Type the address, or pick a server the TV found for itself.
 *
 * There is no mDNS advertisement to listen for, so discovery is the saved host and the two
 * well-known hostnames in `ServerDiscovery.kt`, re-probed on a timer, plus one sweep of the local
 * /24 — all of it collected here for as long as this screen is up.
 */
@Composable
fun ConnectScreen(vm: AppViewModel, ui: UiState) {
    var host by rememberSaveable { mutableStateOf(ui.host) }
    val errorMsg = (ui.phase as? Phase.Connect)?.msg
    val found by vm.discovered.collectAsStateWithLifecycle()
    val scanning by vm.discovering.collectAsStateWithLifecycle()
    val firstServer = remember { FocusRequester() }

    DisposableEffect(Unit) {
        vm.startDiscovery()
        onDispose { vm.stopDiscovery() }
    }
    LaunchedEffect(found.isNotEmpty()) { if (found.isNotEmpty()) runCatching { firstServer.requestFocus() } }

    Row(Modifier.fillMaxSize().padding(horizontal = 40.dp, vertical = 28.dp)) {
        Column(Modifier.width(520.dp)) {
            WordmarkLockup(logoSize = 44.dp)
            VSpace(32.dp)
            SectionLabel("Server address")
            VSpace(10.dp)
            TvTextField(
                host, { host = it },
                placeholder = "e.g. cinematica.lan:8090 or 192.168.1.50",
                imeAction = ImeAction.Done,
                onDone = { vm.connectManual(host) },
                modifier = Modifier.fillMaxWidth(),
            )
            VSpace(14.dp)
            PillButton("Connect", onClick = { vm.connectManual(host) }, primary = true)
            if (errorMsg != null) {
                VSpace(20.dp)
                Text(errorMsg, style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Danger)
            }
        }
        HSpace(48.dp)
        Column(Modifier.fillMaxWidth()) {
            SectionLabel(if (scanning) "Scanning…" else "Servers found")
            VSpace(10.dp)
            if (found.isEmpty() && !scanning) {
                Text(
                    "Nothing found. Check the TV and the server are on the same network, or type the address on the left.",
                    style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted,
                )
            }
            LazyColumn(
                verticalArrangement = Arrangement.spacedBy(8.dp),
                contentPadding = PaddingValues(top = 4.dp, bottom = 24.dp),
            ) {
                // Two discovery paths can surface the same host; index it into the key so duplicates don't collide.
                itemsIndexed(found, key = { i, s -> "$i:${s.host}" }) { _, server ->
                    val mod = if (server === found.first()) Modifier.focusRequester(firstServer) else Modifier
                    FocusSurface(onClick = { vm.connectManual(server.host) }, modifier = mod.fillMaxWidth()) {
                        Row(Modifier.padding(horizontal = 16.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                            StatusDot(CinematicaColors.Good)
                            HSpace(14.dp)
                            Column {
                                Text(server.host, style = MaterialTheme.typography.titleMedium)
                                val detail = listOfNotNull(
                                    server.movies?.let { "$it movies" },
                                    "via ${server.via}",
                                ).joinToString("  ·  ")
                                Text(detail, style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
                            }
                        }
                    }
                }
            }
        }
    }
}
