package io.github.superthom196.cinematica

import android.os.Bundle
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import io.github.superthom196.cinematica.ui.AppRoot
import io.github.superthom196.cinematica.ui.CinematicaTheme
import io.github.superthom196.cinematica.ui.DpadTracker

class MainActivity : ComponentActivity() {

    private val vm: AppViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            CinematicaTheme { AppRoot(vm) }
        }
    }

    override fun onStart() {
        super.onStart()
        vm.setForeground(true)
    }

    override fun onStop() {
        vm.setForeground(false)
        super.onStop()
    }

    /**
     * Media keys drive playback from every screen, before Compose sees the event. D-pad and Back
     * are left alone so on-screen focus and navigation keep working untouched.
     */
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.action == KeyEvent.ACTION_DOWN && event.keyCode in dpadKeys) DpadTracker.stamp()
        if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0 && event.keyCode in mediaKeys) {
            vm.onMediaKey(event.keyCode)
            return true
        }
        if (event.action == KeyEvent.ACTION_UP && event.keyCode in mediaKeys) return true
        return super.dispatchKeyEvent(event)
    }

    private val dpadKeys = setOf(
        KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN,
        KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT,
    )

    private val mediaKeys = setOf(
        KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, KeyEvent.KEYCODE_MEDIA_PLAY, KeyEvent.KEYCODE_MEDIA_PAUSE,
        KeyEvent.KEYCODE_MEDIA_STOP, KeyEvent.KEYCODE_MEDIA_FAST_FORWARD, KeyEvent.KEYCODE_MEDIA_REWIND,
        KeyEvent.KEYCODE_MEDIA_NEXT, KeyEvent.KEYCODE_MEDIA_PREVIOUS,
    )
}
