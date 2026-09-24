package io.github.superthom196.cinematica.player

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import io.github.superthom196.cinematica.api.ChannelPlay

/**
 * Hands a channel video's link to another app on the box to play it. Cinematica never plays a
 * channel video itself — it only starts the intent; Back in that app returns here, the same as
 * leaving for any other app.
 */
object ExternalLaunch {

    /**
     * Prefers [ChannelPlay.pkg] when the server named one, so the link opens directly in the app
     * that owns it rather than a disambiguation sheet; falls back to an unqualified ACTION_VIEW
     * when that app is not installed (or none was named). Returns false when nothing could handle
     * the link at all.
     */
    fun open(context: Context, play: ChannelPlay): Boolean {
        val uri = runCatching { Uri.parse(play.url) }.getOrNull() ?: return false

        fun launch(withPackage: Boolean): Boolean = try {
            val intent = Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            if (withPackage) intent.setPackage(play.pkg)
            context.startActivity(intent)
            true
        } catch (_: ActivityNotFoundException) {
            false
        }

        if (play.pkg.isBlank()) return launch(withPackage = false)
        return launch(withPackage = true) || launch(withPackage = false)
    }
}
