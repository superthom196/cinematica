package io.github.superthom196.cinematica.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.unit.dp
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.UiState
import io.github.superthom196.cinematica.data.PIN_LENGTH
import kotlinx.coroutines.delay

/**
 * The whole screen, when the PIN lock is on and the app has just been opened. Nothing else is
 * composed beneath it, so there is nothing for focus to wander into; Back leaves the app.
 */
@Composable
fun PinScreen(vm: AppViewModel, ui: UiState) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Wordmark(logoSize = 48.dp)
            VSpace(28.dp)
            PinPad(
                title = "Enter your PIN",
                retryAt = ui.pinRetryAt,
                onSubmit = { pin -> if (vm.tryUnlock(pin)) null else "Wrong PIN" },
            )
            VSpace(18.dp)
            Text("Press Back to leave", style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
        }
    }
}

/** What the Settings row is trying to do, which decides which steps the dialog walks through. */
enum class PinSetup { Enable, Change, Disable }

private enum class PinStep { Verify, Enter, Confirm }

/**
 * Turning the lock on asks for a new PIN twice; changing or turning it off asks for the current
 * one first. Every step is the same keypad with a different title, and a wrong answer at any step
 * clears the dots and says why.
 */
@Composable
fun PinSetupDialog(vm: AppViewModel, ui: UiState, mode: PinSetup, onDismiss: () -> Unit) {
    var step by remember { mutableStateOf(if (mode == PinSetup.Enable) PinStep.Enter else PinStep.Verify) }
    var first by remember { mutableStateOf("") }
    TvDialog(onDismiss = onDismiss, width = 360.dp) { armed ->
        PinPad(
            title = when (step) {
                PinStep.Verify -> "Enter your current PIN"
                PinStep.Enter -> "Choose a $PIN_LENGTH-digit PIN"
                PinStep.Confirm -> "Enter it again"
            },
            retryAt = ui.pinRetryAt,
            armed = armed,
            modifier = Modifier.fillMaxWidth(),
            onSubmit = { pin ->
                when (step) {
                    PinStep.Verify ->
                        if (!vm.checkPin(pin)) "Wrong PIN"
                        else if (mode == PinSetup.Disable) { vm.clearPin(); onDismiss(); null }
                        else { step = PinStep.Enter; null }
                    PinStep.Enter -> { first = pin; step = PinStep.Confirm; null }
                    PinStep.Confirm ->
                        if (pin == first) { vm.setPin(pin); onDismiss(); null }
                        else { first = ""; step = PinStep.Enter; "They didn't match — choose it again" }
                }
            },
        )
        VSpace(6.dp)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
            PillButton("Cancel", onClick = { if (armed()) onDismiss() }, dense = true)
        }
    }
}

/**
 * Four dots and a keypad. Digits come from the on-screen keys under the D-pad or straight from a
 * remote's number keys; the fourth digit submits by itself, so there is no OK key to reach for.
 * [onSubmit] returns null to accept the PIN or the message to show for rejecting it; either way
 * the dots clear for the next attempt (the caller decides whether there is one).
 */
@Composable
fun PinPad(
    title: String,
    retryAt: Long,
    onSubmit: suspend (String) -> String?,
    modifier: Modifier = Modifier,
    /** From [TvDialog]: an OK press that began before the keypad existed must not type a digit. */
    armed: () -> Boolean = { true },
) {
    var digits by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }

    // The throttle countdown. Ticks once a second only while there is something to count down.
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(retryAt) {
        while (System.currentTimeMillis() < retryAt) {
            now = System.currentTimeMillis()
            delay(1000)
        }
        now = System.currentTimeMillis()
    }
    val waitSecs = ((retryAt - now + 999) / 1000).coerceAtLeast(0)
    val waiting = waitSecs > 0

    val centreKey = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { centreKey.requestFocus() } }

    fun press(d: Char) {
        if (busy || waiting || digits.length >= PIN_LENGTH) return
        error = null
        digits += d
    }
    fun erase() {
        if (!busy && digits.isNotEmpty()) digits = digits.dropLast(1)
    }

    LaunchedEffect(digits) {
        if (digits.length < PIN_LENGTH) return@LaunchedEffect
        busy = true
        val verdict = onSubmit(digits)
        digits = ""
        error = verdict
        busy = false
    }

    Column(
        modifier.onPreviewKeyEvent { ev ->
            if (ev.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
            val digit = digitOf(ev.key)
            when {
                digit != null -> { press(digit); true }
                ev.key == Key.Backspace || ev.key == Key.Delete -> { erase(); true }
                else -> false
            }
        },
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(title, style = MaterialTheme.typography.titleLarge)
        VSpace(14.dp)
        Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            repeat(PIN_LENGTH) { i ->
                Box(
                    Modifier.size(18.dp).clip(CircleShape)
                        .background(if (i < digits.length) CinematicaColors.Accent else CinematicaColors.SurfaceHigh),
                )
            }
        }
        VSpace(10.dp)
        val message = error
        Text(
            when {
                waiting -> "Too many wrong PINs — try again in ${waitSecs}s"
                message != null -> message
                else -> " "
            },
            style = MaterialTheme.typography.bodySmall,
            color = CinematicaColors.Danger,
        )
        VSpace(14.dp)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("123", "456", "789", " 0⌫").forEach { keys ->
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    keys.forEach { ch ->
                        when (ch) {
                            ' ' -> Spacer(Modifier.size(KEY_SIZE))
                            '⌫' -> PinKey("⌫") { if (armed()) erase() }
                            else -> PinKey(
                                ch.toString(),
                                modifier = if (ch == '5') Modifier.focusRequester(centreKey) else Modifier,
                            ) { if (armed()) press(ch) }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun PinKey(label: String, modifier: Modifier = Modifier, onClick: () -> Unit) {
    FocusSurface(onClick = onClick, modifier = modifier.size(KEY_SIZE), shape = RoundedCornerShape(10.dp), scale = 1.06f) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text(label, style = MaterialTheme.typography.headlineSmall)
        }
    }
}

private val KEY_SIZE = 64.dp

/** The remote's own number row, if it has one, and a keyboard's number pad. */
private fun digitOf(key: Key): Char? = when (key) {
    Key.Zero, Key.NumPad0 -> '0'
    Key.One, Key.NumPad1 -> '1'
    Key.Two, Key.NumPad2 -> '2'
    Key.Three, Key.NumPad3 -> '3'
    Key.Four, Key.NumPad4 -> '4'
    Key.Five, Key.NumPad5 -> '5'
    Key.Six, Key.NumPad6 -> '6'
    Key.Seven, Key.NumPad7 -> '7'
    Key.Eight, Key.NumPad8 -> '8'
    Key.Nine, Key.NumPad9 -> '9'
    else -> null
}
