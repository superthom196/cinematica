package io.github.superthom196.cinematica.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.api.Genre
import io.github.superthom196.cinematica.api.NetCheck
import io.github.superthom196.cinematica.browse.SORTS

/**
 * The app's one dialog shell.
 *
 * The OK press that opened a dialog has its key-down before the dialog exists, so its stray key-up
 * would otherwise land on whatever row took focus first and choose it. [sawKeyDown] is the fix MATV
 * uses: a row only counts once this dialog has seen a fresh OK go down inside it. A timer cannot do
 * the job — hold the button for a second and any window passes.
 */
@Composable
fun TvDialog(
    onDismiss: () -> Unit,
    width: Dp = 420.dp,
    content: @Composable (armed: () -> Boolean) -> Unit,
) {
    // Read at click time rather than captured into the row's lambda at composition: the key-down
    // and the key-up of one press can land in the same frame, and then a captured `false` would
    // still be swallowing a press the dialog has already seen the start of.
    val sawKeyDown = remember { mutableStateOf(false) }
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Column(
            Modifier
                .width(width)
                .onPreviewKeyEvent { ev ->
                    val isOk = ev.key == Key.DirectionCenter || ev.key == Key.Enter || ev.key == Key.NumPadEnter
                    when {
                        ev.type == KeyEventType.KeyDown && ev.nativeKeyEvent.repeatCount == 0 -> { sawKeyDown.value = true; false }
                        ev.type == KeyEventType.KeyDown -> true
                        isOk && !sawKeyDown.value -> true
                        else -> false
                    }
                }
                .background(CinematicaColors.Surface, RoundedCornerShape(14.dp))
                .padding(16.dp),
        ) {
            content { sawKeyDown.value }
        }
    }
}

@Composable
private fun DialogTitle(text: String) {
    Text(text, style = MaterialTheme.typography.titleLarge)
    VSpace(10.dp)
}

/** Sort: three named views of the same pool, each with the phone page's one-line explanation. */
@Composable
fun SortDialog(current: String, onPick: (String) -> Unit, onDismiss: () -> Unit) {
    val first = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
    TvDialog(onDismiss = onDismiss, width = 380.dp) { armed ->
        DialogTitle("Sort")
        SORTS.forEachIndexed { i, (key, name, description) ->
            val selected = key == current
            FocusSurface(
                onClick = { if (armed()) { onDismiss(); onPick(key) } },
                modifier = (if (selected || (i == 0 && SORTS.none { it.first == current })) Modifier.focusRequester(first) else Modifier)
                    .fillMaxWidth(),
                shape = RoundedCornerShape(8.dp),
                container = if (selected) CinematicaColors.SurfaceHigh else CinematicaColors.Surface,
                scale = 1.0f,
            ) {
                Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
                    Text(name, style = MaterialTheme.typography.titleMedium, color = if (selected) CinematicaColors.Accent else CinematicaColors.Text)
                    Text(description, style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
                }
            }
            VSpace(4.dp)
        }
    }
}

/**
 * Genres: three states per row, cycled by OK — off, included (✓, green), excluded (✕, red, struck
 * through). Toggling is saved as it happens so the header count follows, but nothing reloads the
 * grid until Apply or Clear: every reload is a cold `/api/movies` and they are not free.
 */
@Composable
fun GenreDialog(
    genres: List<Genre>,
    include: Set<String>,
    exclude: Set<String>,
    onToggle: (include: Set<String>, exclude: Set<String>) -> Unit,
    onApply: () -> Unit,
    onClear: () -> Unit,
    onDismiss: () -> Unit,
) {
    val first = remember { FocusRequester() }
    LaunchedEffect(genres.isNotEmpty()) { if (genres.isNotEmpty()) runCatching { first.requestFocus() } }
    TvDialog(onDismiss = onDismiss, width = 520.dp) { armed ->
        DialogTitle("Genres")
        if (genres.isEmpty()) {
            Text(
                "No genres — the server did not answer /api/genres. The grid still works.",
                style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted,
            )
            VSpace(12.dp)
        } else {
            val half = (genres.size + 1) / 2
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                listOf(genres.take(half), genres.drop(half)).forEach { column ->
                    Column(Modifier.weight(1f)) {
                        column.forEachIndexed { row, g ->
                            val id = g.id.toString()
                            val included = id in include
                            val excluded = id in exclude
                            FocusSurface(
                                onClick = {
                                    if (!armed()) return@FocusSurface
                                    when {
                                        included -> onToggle(include - id, exclude + id)
                                        excluded -> onToggle(include, exclude - id)
                                        else -> onToggle(include + id, exclude)
                                    }
                                },
                                modifier = (if (row == 0 && g === genres.first()) Modifier.focusRequester(first) else Modifier).fillMaxWidth(),
                                shape = RoundedCornerShape(6.dp),
                                container = CinematicaColors.Surface,
                                scale = 1.0f,
                            ) {
                                Row(Modifier.padding(horizontal = 10.dp, vertical = 5.dp), verticalAlignment = Alignment.CenterVertically) {
                                    Text(
                                        if (included) "✓" else if (excluded) "✕" else " ",
                                        style = MaterialTheme.typography.labelMedium,
                                        color = if (included) CinematicaColors.Good else CinematicaColors.Danger,
                                        modifier = Modifier.width(18.dp),
                                    )
                                    Text(
                                        g.name.orEmpty(),
                                        style = MaterialTheme.typography.bodyMedium,
                                        color = when {
                                            included -> CinematicaColors.Good
                                            excluded -> CinematicaColors.Danger
                                            else -> CinematicaColors.Text
                                        },
                                        textDecoration = if (excluded) TextDecoration.LineThrough else null,
                                        maxLines = 1,
                                    )
                                }
                            }
                        }
                    }
                }
            }
            VSpace(12.dp)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            PillButton("Clear", onClick = { if (armed()) onClear() }, dense = true)
            PillButton("Apply", onClick = { if (armed()) { onDismiss(); onApply() } }, primary = true, dense = true)
        }
    }
}

/** What the ⚡ chip actually means, and the one button that changes it. */
@Composable
fun NetCheckDialog(net: NetCheck?, onRemeasure: () -> Unit, onDismiss: () -> Unit) {
    val first = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
    TvDialog(onDismiss = onDismiss, width = 560.dp) { armed ->
        DialogTitle("Connection")
        SettingRow("Stream budget", "${num(net?.sustain_live)} Mbps") { }
        SettingRow("Peer connections", "${net?.conns ?: "?"}") { }
        SettingRow("Link tested at", "${num(net?.http_mbps)} Mbps") { }
        SettingRow("Source", net?.source ?: "default") { }
        SettingRow("Ordinary swarms measured", "${net?.n_ordinary ?: "?"}") { }
        SettingRow("More needed before it stops estimating", "${net?.needs ?: "?"}") { }
        SettingRow("Measured", net?.age_h?.let { "${num(it)}h ago" } ?: "never") { }
        VSpace(10.dp)
        Text(
            "Re-measuring downloads from real swarms for several minutes and uses the whole link, " +
                "so nothing else will play while it runs.",
            style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted,
        )
        VSpace(8.dp)
        PillButton(
            "Re-measure now",
            onClick = { if (armed()) { onDismiss(); onRemeasure() } },
            modifier = Modifier.focusRequester(first),
            dense = true,
        )
    }
}

/** A one-field editor: used for the player name in Settings. */
@Composable
fun TextEntryDialog(
    title: String,
    initial: String,
    placeholder: String,
    onSave: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    var value by remember { mutableStateOf(initial) }
    val field = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { field.requestFocus() } }
    TvDialog(onDismiss = onDismiss, width = 520.dp) { armed ->
        DialogTitle(title)
        TvTextField(
            value, { value = it },
            placeholder = placeholder,
            imeAction = ImeAction.Done,
            onDone = { onDismiss(); onSave(value) },
            modifier = Modifier.fillMaxWidth().focusRequester(field),
        )
        VSpace(12.dp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            PillButton("Save", onClick = { if (armed()) { onDismiss(); onSave(value) } }, primary = true, dense = true)
            PillButton("Cancel", onClick = { if (armed()) onDismiss() }, dense = true)
        }
    }
}
