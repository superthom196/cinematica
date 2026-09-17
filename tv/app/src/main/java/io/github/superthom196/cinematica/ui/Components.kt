@file:OptIn(ExperimentalTvMaterial3Api::class)

package io.github.superthom196.cinematica.ui

import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.PlatformTextStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.LineHeightStyle
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import androidx.tv.material3.Border
import androidx.tv.material3.ClickableSurfaceDefaults
import androidx.tv.material3.ExperimentalTvMaterial3Api
import androidx.tv.material3.Icon
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Surface
import androidx.tv.material3.Text
import coil3.compose.AsyncImage
import coil3.compose.LocalPlatformContext
import coil3.request.ImageRequest
import coil3.request.crossfade
import io.github.superthom196.cinematica.R

/** A focusable, clickable panel with the app's focus treatment: white ring, slight grow. */
@Composable
fun FocusSurface(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    shape: RoundedCornerShape = RoundedCornerShape(14.dp),
    container: Color = CinematicaColors.Surface,
    focusedContainer: Color = CinematicaColors.SurfaceHigh,
    scale: Float = 1.04f,
    onLongClick: (() -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    Surface(
        onClick = onClick,
        onLongClick = onLongClick,
        modifier = modifier,
        shape = ClickableSurfaceDefaults.shape(shape = shape),
        colors = ClickableSurfaceDefaults.colors(
            containerColor = container,
            focusedContainerColor = focusedContainer,
            pressedContainerColor = focusedContainer,
            contentColor = CinematicaColors.Text,
            focusedContentColor = CinematicaColors.Text,
        ),
        border = ClickableSurfaceDefaults.border(
            focusedBorder = Border(BorderStroke(2.dp, CinematicaColors.Focus), shape = shape),
        ),
        scale = ClickableSurfaceDefaults.scale(focusedScale = scale),
    ) { content() }
}

/** Primary / secondary pill buttons. */
@Composable
fun PillButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    icon: ImageVector? = null,
    primary: Boolean = false,
    enabled: Boolean = true,
    /** Header chips and dialog footers use the tighter form; a screen's main action does not. */
    dense: Boolean = false,
) {
    FocusSurface(
        onClick = { if (enabled) onClick() },
        modifier = modifier,
        shape = RoundedCornerShape(50),
        container = if (primary && enabled) CinematicaColors.Accent else CinematicaColors.Surface,
        focusedContainer = if (primary && enabled) CinematicaColors.AccentBright else CinematicaColors.SurfaceHigh,
        scale = 1.06f,
    ) {
        Row(
            Modifier.padding(
                horizontal = if (dense) 12.dp else 20.dp,
                vertical = if (dense) 7.dp else 10.dp,
            ),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(8.dp),
        ) {
            val tint = if (primary && enabled) CinematicaColors.OnAccent else CinematicaColors.Text
            if (icon != null) Icon(icon, contentDescription = null, tint = tint, modifier = Modifier.size(if (dense) 18.dp else 26.dp))
            Text(
                text,
                style = if (dense) MaterialTheme.typography.labelMedium else MaterialTheme.typography.labelLarge,
                color = if (enabled) tint else CinematicaColors.Muted,
                maxLines = 1,
            )
        }
    }
}

/**
 * One pill, two (or more) segments, exactly one of them on: the header's Films | Series switch.
 * The chosen segment is filled in the accent; the rest sit greyed inside the same pill, so the
 * control reads as a single switch rather than a row of buttons.
 */
@Composable
fun SegmentedPill(
    options: List<Pair<String, String>>,
    selected: String,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val pill = RoundedCornerShape(50)
    Row(
        modifier.background(CinematicaColors.Surface, pill).padding(3.dp),
        horizontalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(2.dp),
    ) {
        options.forEach { (key, label) ->
            val on = key == selected
            FocusSurface(
                onClick = { if (!on) onSelect(key) },
                shape = pill,
                container = if (on) CinematicaColors.Accent else Color.Transparent,
                focusedContainer = if (on) CinematicaColors.AccentBright else CinematicaColors.SurfaceHigh,
                scale = 1f,
            ) {
                Text(
                    label,
                    style = MaterialTheme.typography.labelMedium,
                    color = if (on) CinematicaColors.OnAccent else CinematicaColors.Muted,
                    maxLines = 1,
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 5.dp),
                )
            }
        }
    }
}

/** Round icon-only transport button. */
@Composable
fun RoundIconButton(
    icon: ImageVector,
    contentDescription: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    size: Dp = 72.dp,
    primary: Boolean = false,
) {
    FocusSurface(
        onClick = onClick,
        modifier = modifier.size(size),
        shape = RoundedCornerShape(50),
        container = if (primary) CinematicaColors.Accent else CinematicaColors.SurfaceHigh,
        focusedContainer = if (primary) CinematicaColors.AccentBright else Color(0xFF3A3A3A),
        scale = 1.1f,
    ) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Icon(icon, contentDescription, tint = if (primary) CinematicaColors.OnAccent else CinematicaColors.Text, modifier = Modifier.size(size * 0.5f))
        }
    }
}

/**
 * A lighter-weight stand-in for [FocusSurface], used for grid tiles.
 *
 * tv-material's `Surface` sets up an interaction source, indication, glow, border, shape and
 * scale-animation machinery for every instance. Multiplied across a wide poster grid on a slow
 * TV CPU, that per-tile composition cost — not image decoding — is what shows up as scroll jank.
 * This reproduces the same ring-and-grow focus treatment with a plain clickable `Box`.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun GridTile(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    onLongClick: (() -> Unit)? = null,
    content: @Composable (focused: Boolean) -> Unit,
) {
    var focused by remember { mutableStateOf(false) }
    val scale by animateFloatAsState(if (focused) 1.05f else 1f, label = "tileScale")
    val shape = RoundedCornerShape(10.dp)
    Box(
        // Order matters: the caller's `modifier` (grid focusRequester/onFocusChanged) must stay
        // outermost so it still sees focus changes first; combinedClickable is the node that
        // actually becomes focusable and receives D-pad center/Enter (and long-press), so our own
        // focus tracking goes just before it; the background/border decoration reacts to that
        // state and is innermost since it only paints, it doesn't need to be focusable itself.
        modifier
            .graphicsLayer { scaleX = scale; scaleY = scale }
            .onFocusChanged { focused = it.isFocused }
            .combinedClickable(onClick = onClick, onLongClick = onLongClick, interactionSource = null, indication = null)
            .then(if (focused) Modifier.background(CinematicaColors.SurfaceHigh, shape) else Modifier)
            .then(if (focused) Modifier.border(2.dp, CinematicaColors.Focus, shape) else Modifier),
        propagateMinConstraints = false,
    ) {
        content(focused)
    }
}

/**
 * The app's wordmark: the name set in Alfa Slab One, in the ticket's cream.
 *
 * Type rather than `R.drawable.wordmark` — the PNG was drawn for a big box and turned to mush at
 * header size, where this stays crisp at any distance. (The drawable stays in the tree: the
 * launcher banner is still artwork.)
 *
 * [logoSize] is the height of the capitals, not the font size, so a caller sizing the header row
 * gets the letters it asked for. Alfa Slab One's capitals are 0.778 em, and the line box is
 * trimmed to the text itself, so the composable occupies the row and nothing more.
 */
@Composable
fun Wordmark(logoSize: Dp = 30.dp, modifier: Modifier = Modifier) {
    Text(
        text = "CINEMATICA",
        style = wordmarkStyle(logoSize),
        color = CinematicaColors.Cream,
        maxLines = 1,
        modifier = modifier,
    )
}

/** The wordmark with its tagline under it, for the screens that are nothing but the name. */
@Composable
fun WordmarkLockup(
    logoSize: Dp = 44.dp,
    tagline: String = "Top-rated 4K movies",
    modifier: Modifier = Modifier,
) {
    Column(modifier) {
        Wordmark(logoSize)
        Spacer(Modifier.height(logoSize * 0.28f))
        Text(tagline, style = MaterialTheme.typography.bodyLarge, color = CinematicaColors.Muted)
    }
}

@Composable
private fun wordmarkStyle(capHeight: Dp): TextStyle = remember(capHeight) {
    // dp in, sp out: this is display lettering sized to a layout, and a system font scale that
    // stretched it would only break the row it was measured into.
    val size = (capHeight.value / CAP_HEIGHT_EM).sp
    TextStyle(
        fontFamily = AlfaSlabOne,
        fontSize = size,
        // Slab capitals set tight look crowded; a hair of tracking opens them up without the name
        // reading as spaced-out.
        letterSpacing = 0.03.em,
        lineHeight = size,
        platformStyle = PlatformTextStyle(includeFontPadding = false),
        lineHeightStyle = LineHeightStyle(
            alignment = LineHeightStyle.Alignment.Center,
            trim = LineHeightStyle.Trim.Both,
        ),
    )
}

/** The bundled face, loaded once. Its licence is credited on the settings screen. */
private val AlfaSlabOne = FontFamily(Font(R.font.alfa_slab_one))

/** Alfa Slab One's capitals, as a fraction of the em (from the font's own OS/2 table). */
private const val CAP_HEIGHT_EM = 0.778f

/** A provider-supplied image, or a flat placeholder when the url is null. */
@Composable
fun PosterImage(url: String?, modifier: Modifier = Modifier, corner: Dp = 8.dp, crossfade: Boolean = false) {
    Box(modifier.clip(RoundedCornerShape(corner)).background(Color(0xFF23232B))) {
        if (url != null) {
            val context = LocalPlatformContext.current
            val request = remember(url, crossfade) { ImageRequest.Builder(context).data(url).crossfade(crossfade).build() }
            AsyncImage(model = request, contentDescription = null, modifier = Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
        }
    }
}

/** The one progress bar shape the app uses: tile, bottom strip and buffering overlay all share it. */
@Composable
fun ProgressBar(pct: Double, modifier: Modifier = Modifier, height: Dp = 4.dp) {
    val fraction = (pct / 100.0).coerceIn(0.0, 1.0).toFloat()
    Box(modifier.height(height).clip(RoundedCornerShape(50)).background(CinematicaColors.SurfaceHigh)) {
        Box(Modifier.fillMaxWidth(fraction).fillMaxHeight().background(CinematicaColors.Accent))
    }
}

/** An indeterminate bar for the parts of a job the server cannot count (the speed probe). */
@Composable
fun IndeterminateBar(modifier: Modifier = Modifier, height: Dp = 4.dp) {
    val transition = androidx.compose.animation.core.rememberInfiniteTransition(label = "indet")
    val x by transition.animateFloat(
        initialValue = -0.38f,
        targetValue = 1f,
        animationSpec = androidx.compose.animation.core.infiniteRepeatable(
            androidx.compose.animation.core.tween(1300, easing = androidx.compose.animation.core.LinearEasing),
        ),
        label = "indetX",
    )
    androidx.compose.foundation.layout.BoxWithConstraints(
        modifier.height(height).clip(RoundedCornerShape(50)).background(CinematicaColors.SurfaceHigh),
    ) {
        val w = maxWidth
        Box(
            Modifier
                .padding(start = (w * x).coerceAtLeast(0.dp))
                .width(w * 0.38f)
                .fillMaxHeight()
                .background(CinematicaColors.Accent),
        )
    }
}

/** A settings-style row: label on the left, current value in the accent on the right. */
@Composable
fun SettingRow(
    label: String,
    value: String,
    modifier: Modifier = Modifier,
    /** When set, D-pad left/right on the row call this with −1/+1 instead of moving focus. */
    onAdjust: ((Int) -> Unit)? = null,
    onClick: () -> Unit,
) {
    val adjustable = if (onAdjust == null) modifier else modifier.onPreviewKeyEvent { ev ->
        if (ev.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
        when (ev.key) {
            Key.DirectionLeft -> { onAdjust(-1); true }
            Key.DirectionRight -> { onAdjust(1); true }
            else -> false
        }
    }
    FocusSurface(onClick = onClick, modifier = adjustable.fillMaxWidth()) {
        Row(Modifier.padding(horizontal = 16.dp, vertical = 9.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(label, style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            Text(value, style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Accent)
        }
    }
}

/** A non-focusable outlined chip; the detail screen's facts row is made of these. */
@Composable
fun InfoChip(text: String, modifier: Modifier = Modifier) {
    Box(
        modifier
            .border(1.dp, CinematicaColors.SurfaceHigh, RoundedCornerShape(50))
            .padding(horizontal = 9.dp, vertical = 3.dp),
    ) {
        Text(text, style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted, maxLines = 1)
    }
}

/**
 * Text entry that works with a D-pad: focus ring, OK opens the on-screen keyboard.
 * (tv-material has no TextField; this is a styled BasicTextField.)
 */
@Composable
fun TvTextField(
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String,
    modifier: Modifier = Modifier,
    password: Boolean = false,
    imeAction: ImeAction = ImeAction.Next,
    onDone: () -> Unit = {},
) {
    var focused by remember { mutableStateOf(false) }
    val keyboard = LocalSoftwareKeyboardController.current
    val shape = RoundedCornerShape(10.dp)
    Box(
        modifier
            .background(if (focused) CinematicaColors.SurfaceHigh else CinematicaColors.Surface, shape)
            .then(if (focused) Modifier.border(2.dp, CinematicaColors.Focus, shape) else Modifier)
            .padding(horizontal = 14.dp, vertical = 11.dp),
    ) {
        if (value.isEmpty()) Text(placeholder, style = MaterialTheme.typography.bodyLarge, color = CinematicaColors.Muted)
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            singleLine = true,
            textStyle = TextStyle(color = CinematicaColors.Text, fontSize = MaterialTheme.typography.bodyLarge.fontSize),
            cursorBrush = SolidColor(CinematicaColors.Accent),
            visualTransformation = if (password) PasswordVisualTransformation() else VisualTransformation.None,
            keyboardOptions = KeyboardOptions(
                keyboardType = if (password) KeyboardType.Password else KeyboardType.Text,
                imeAction = imeAction,
                autoCorrectEnabled = false,
            ),
            keyboardActions = KeyboardActions(onDone = { keyboard?.hide(); onDone() }, onSearch = { keyboard?.hide(); onDone() }, onNext = { }),
            modifier = Modifier
                .fillMaxWidth()
                .onFocusChanged { focused = it.isFocused }
                .onPreviewKeyEvent { ev ->
                    if (ev.type == KeyEventType.KeyUp && (ev.key == Key.DirectionCenter || ev.key == Key.Enter || ev.key == Key.NumPadEnter)) {
                        keyboard?.show(); true
                    } else false
                },
        )
    }
}

@Composable
fun SectionLabel(text: String, modifier: Modifier = Modifier) {
    Text(text.uppercase(), style = MaterialTheme.typography.labelMedium, color = CinematicaColors.Muted, modifier = modifier)
}

@Composable
fun StatusDot(color: Color, modifier: Modifier = Modifier) {
    Box(modifier.size(12.dp).clip(CircleShape).background(color))
}

fun formatTime(seconds: Double?): String {
    if (seconds == null || seconds < 0) return "–:––"
    val s = seconds.toInt()
    val h = s / 3600; val m = (s % 3600) / 60; val sec = s % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, sec) else "%d:%02d".format(m, sec)
}

/**
 * The phone page prints JSON numbers as JavaScript does — `8`, not `8.0` — so a budget that is a
 * round number reads as one. Kotlin's Double always carries the `.0`, hence this.
 */
fun num(value: Double?): String {
    if (value == null) return "?"
    if (value == value.toLong().toDouble()) return value.toLong().toString()
    return value.toString()
}

@Composable
fun HSpace(w: Dp) = Spacer(Modifier.width(w))
@Composable
fun VSpace(h: Dp) = Spacer(Modifier.height(h))
