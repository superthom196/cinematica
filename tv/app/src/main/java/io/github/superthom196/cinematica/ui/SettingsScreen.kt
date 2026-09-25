package io.github.superthom196.cinematica.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.BuildConfig
import io.github.superthom196.cinematica.UiState
import io.github.superthom196.cinematica.api.HifiPlayer
import io.github.superthom196.cinematica.data.PIN_LENGTH
import io.github.superthom196.cinematica.player.displayLanguage

@Composable
fun SettingsScreen(vm: AppViewModel, ui: UiState) {
    val passthrough by vm.passthrough.collectAsStateWithLifecycle()
    val networkCachingMs by vm.networkCachingMs.collectAsStateWithLifecycle()
    val verboseVlc by vm.verboseVlc.collectAsStateWithLifecycle()
    val hifiAudio by vm.hifiAudio.collectAsStateWithLifecycle()
    val hifiPlayerUrl by vm.hifiPlayerUrl.collectAsStateWithLifecycle()
    val hifiDelayMs by vm.hifiDelayMs.collectAsStateWithLifecycle()
    val hifiCentre by vm.hifiCentre.collectAsStateWithLifecycle()
    val hifiPlayers by vm.hifiPlayers.collectAsStateWithLifecycle()
    val audioLanguage by vm.audioLanguage.collectAsStateWithLifecycle()
    val subtitleMode by vm.subtitleMode.collectAsStateWithLifecycle()
    val playerName by vm.playerName.collectAsStateWithLifecycle()
    val autoplayNext by vm.autoplayNext.collectAsStateWithLifecycle()
    val ukUsBias by vm.ukUsBias.collectAsStateWithLifecycle()
    val pinEnabled by vm.pinEnabled.collectAsStateWithLifecycle()
    val health = ui.health
    val first = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
    LaunchedEffect(Unit) { vm.refreshHifiPlayers() }

    var editingName by remember { mutableStateOf(false) }
    if (editingName) {
        TextEntryDialog(
            title = "Player name",
            initial = playerName,
            placeholder = "What the server calls this TV",
            onSave = { vm.setPlayerName(it) },
            onDismiss = { editingName = false },
        )
    }

    var pinSetup by remember { mutableStateOf<PinSetup?>(null) }
    pinSetup?.let { mode -> PinSetupDialog(vm, ui, mode, onDismiss = { pinSetup = null }) }

    Column(Modifier.fillMaxSize().padding(start = 28.dp, end = 28.dp, top = 18.dp)) {
        Text("Settings", style = MaterialTheme.typography.headlineSmall)
        VSpace(12.dp)
        Column(
            Modifier.width(760.dp).fillMaxSize().verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            SectionLabel("Server")
            SettingRow("Server host", ui.host, modifier = Modifier.focusRequester(first)) { vm.editServer() }
            VSpace(8.dp)
            SectionLabel("Playback")
            SettingRow("Audio passthrough", if (passthrough) "On" else "Off") { vm.togglePassthrough() }
            // Which track a film opens with. The file's own "default" flags are routinely wrong —
            // a Matroska can mark Spanish audio and Russian subtitles default — so these two are
            // what actually decide it.
            SettingRow("Audio language", displayLanguage(audioLanguage)) { vm.cycleAudioLanguage() }
            SettingRow("Subtitles", subtitleMode.label) { vm.cycleSubtitleMode() }
            SettingRow("Network caching", "$networkCachingMs ms") { vm.cycleNetworkCaching() }
            SettingRow("Verbose VLC log", if (verboseVlc) "On" else "Off") { vm.toggleVerboseVlc() }
            SettingRow("Network audio", if (hifiAudio) "On" else "Off") { vm.toggleHifiAudio() }
            if (hifiAudio) {
                SettingRow("Network audio player", hifiPlayerLabel(hifiPlayerUrl, hifiPlayers)) { vm.cycleHifiPlayer() }
                // On: the TV's speakers play the centre channel, the player the rest. Next film on.
                SettingRow("TV speakers", if (hifiCentre) "Centre channel" else "Off") { vm.toggleHifiCentre() }
                // Left/right trims by 25 ms; OK steps up. Positive = the sound plays later.
                SettingRow(
                    "Lip sync (audio delay)",
                    (if (hifiDelayMs > 0) "+" else "") + "$hifiDelayMs ms  ◀ ▶",
                    onAdjust = { vm.adjustHifiDelay(it) },
                ) { vm.adjustHifiDelay(1) }
            }
            SettingRow("Player name", playerName) { editingName = true }
            SettingRow("Autoplay next episode", if (autoplayNext) "On" else "Off") { vm.toggleAutoplayNext() }
            VSpace(8.dp)
            SectionLabel("Browse")
            SettingRow("UK/US bias", if (ukUsBias) "On" else "Off") { vm.toggleUkUsBias() }
            Text(
                "Films and series in English only, British ones first, the widely watched ones from anywhere alongside. Off is the provider's own rating order. Genres and search work the same either way.",
                style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
            )
            VSpace(8.dp)
            SectionLabel("Lock")
            // Off until someone chooses a PIN. Turning it off, or changing it, asks for the current
            // one first; the dialog walks through whichever steps the mode needs.
            SettingRow("PIN lock", if (pinEnabled) "On" else "Off") {
                pinSetup = if (pinEnabled) PinSetup.Disable else PinSetup.Enable
            }
            if (pinEnabled) SettingRow("Change PIN", "••••") { pinSetup = PinSetup.Change }
            Text(
                "Asks for a $PIN_LENGTH-digit PIN whenever the app opens, including on the way back from the Home screen.",
                style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
            )
            VSpace(8.dp)
            SectionLabel("About")
            SettingRow("Cinematica", "v${BuildConfig.VERSION_NAME}") { }
            val serverLine = if (health != null) {
                "${health.movies ?: "?"} movies · ${health.imdb_titles ?: "?"} IMDb titles · budget ${num(health.sustain_mbps)} Mbps (${health.net_source ?: "unknown"})"
            } else "Not connected"
            SettingRow("Server", serverLine) { }
            // Sort and genres are not here on purpose: they are one press away in the header, where
            // the grid they change is on screen to see the change.
            VSpace(24.dp)
        }
    }
}

/**
 * What the "Network audio player" row says. Never the saved player's address:
 * an empty list means the server found nobody to play to (or its audio bridge
 * is down), and the host of a url nobody answers at reads as a chosen, working
 * player when it is neither.
 */
internal fun hifiPlayerLabel(savedUrl: String?, players: List<HifiPlayer>): String = when {
    players.isEmpty() -> "No Sendspin players found..."
    savedUrl == null -> "Server default"
    else -> players.firstOrNull { it.url == savedUrl }?.name ?: "Saved player not found"
}
