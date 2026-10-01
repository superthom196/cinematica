package io.github.superthom196.cinematica.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import androidx.tv.material3.LocalContentColor
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Typography
import androidx.tv.material3.darkColorScheme

/**
 * The accent colours the viewer can pick in Settings. Each is a fill dark enough for [onFill]
 * text, and a bright tint that reads on [CinematicaColors.Background]. None is green, red or
 * amber: those are kept for status (connected, excluded, warnings).
 */
enum class AccentColour(val key: String, val label: String, val fill: Color, val bright: Color, val onFill: Color) {
    /** The 4K purple: the original accent. */
    Purple("purple", "Purple", Color(0xFF7C3AED), Color(0xFFA78BFA), Color(0xFFF5F3FF)),
    Blue("blue", "Blue", Color(0xFF2563EB), Color(0xFF60A5FA), Color(0xFFEFF6FF)),
    Teal("teal", "Teal", Color(0xFF0F766E), Color(0xFF2DD4BF), Color(0xFFF0FDFA)),
    Pink("pink", "Pink", Color(0xFFDB2777), Color(0xFFF472B6), Color(0xFFFDF2F8)),
    Slate("slate", "Slate", Color(0xFF475569), Color(0xFFCBD5E1), Color(0xFFF8FAFC));

    companion object {
        fun fromKey(key: String?): AccentColour = entries.firstOrNull { it.key == key } ?: Purple
    }
}

object CinematicaColors {
    /** Which [AccentColour] the getters below answer with; set by [CinematicaTheme]. */
    internal var accent by mutableStateOf(AccentColour.Purple)

    val Background = Color(0xFF101014)
    val Surface = Color(0xFF1B1B22)
    val SurfaceHigh = Color(0xFF26262F)
    /** The app's one accent, whichever the viewer chose. State-backed, so a change recolours everything. */
    val Accent: Color get() = accent.fill
    val AccentBright: Color get() = accent.bright
    val OnAccent: Color get() = accent.onFill
    val Text = Color(0xFFF5F5F5)
    /** The wordmark's cream: the colour of the ticket the logo was drawn from. */
    val Cream = Color(0xFFF3E6C8)
    val Muted = Color(0xFF9E9E9E)
    val Focus = Color(0xFFFFFFFF)
    val Danger = Color(0xFFE5533D)
    /** The recency bonus on a rating badge: the phone page's `.rate .bo` light blue. */
    val Boost = Color(0xFF7DD3FC)
    val Warn = Color(0xFFE8B33C)
    val Good = Color(0xFF5BC18A)
}

/** Ten-foot typography: big headings, compact body text (a TV grid gets cluttered fast). */
private val tvTypography = Typography(
    displayLarge = TextStyle(fontSize = 64.sp, lineHeight = 72.sp, fontWeight = FontWeight.SemiBold),
    displayMedium = TextStyle(fontSize = 48.sp, lineHeight = 56.sp, fontWeight = FontWeight.SemiBold),
    displaySmall = TextStyle(fontSize = 40.sp, lineHeight = 48.sp, fontWeight = FontWeight.SemiBold),
    headlineLarge = TextStyle(fontSize = 34.sp, lineHeight = 42.sp, fontWeight = FontWeight.SemiBold),
    headlineMedium = TextStyle(fontSize = 28.sp, lineHeight = 36.sp, fontWeight = FontWeight.Medium),
    headlineSmall = TextStyle(fontSize = 24.sp, lineHeight = 32.sp, fontWeight = FontWeight.Medium),
    titleLarge = TextStyle(fontSize = 18.sp, lineHeight = 23.sp, fontWeight = FontWeight.Medium),
    titleMedium = TextStyle(fontSize = 15.sp, lineHeight = 19.sp, fontWeight = FontWeight.Medium),
    titleSmall = TextStyle(fontSize = 13.sp, lineHeight = 17.sp, fontWeight = FontWeight.Medium),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 21.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 18.sp),
    bodySmall = TextStyle(fontSize = 12.sp, lineHeight = 16.sp),
    labelLarge = TextStyle(fontSize = 15.sp, lineHeight = 19.sp, fontWeight = FontWeight.Medium),
    labelMedium = TextStyle(fontSize = 13.sp, lineHeight = 17.sp, fontWeight = FontWeight.Medium),
    labelSmall = TextStyle(fontSize = 12.sp, lineHeight = 15.sp, fontWeight = FontWeight.Medium),
)

@Composable
fun CinematicaTheme(accent: AccentColour = AccentColour.Purple, content: @Composable () -> Unit) {
    SideEffect { CinematicaColors.accent = accent }
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = CinematicaColors.Accent,
            onPrimary = CinematicaColors.OnAccent,
            secondary = CinematicaColors.Accent,
            background = CinematicaColors.Background,
            onBackground = CinematicaColors.Text,
            surface = CinematicaColors.Surface,
            onSurface = CinematicaColors.Text,
            surfaceVariant = CinematicaColors.SurfaceHigh,
            onSurfaceVariant = CinematicaColors.Muted,
            border = CinematicaColors.Focus,
            error = CinematicaColors.Danger,
        ),
        typography = tvTypography,
    ) {
        // tv-material's default content colour outside a Surface is dark; our screens sit on a dark background.
        CompositionLocalProvider(LocalContentColor provides CinematicaColors.Text, content = content)
    }
}
