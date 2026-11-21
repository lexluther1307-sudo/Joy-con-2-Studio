package com.joycon2studio

import android.app.Application
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * The Joy-Con and Deck links, held by a ViewModel: a recreated screen (for example after a theme change) keeps the
 * session. They are closed when the screen finishes. Callbacks can come from the link thread, so the screen is
 * redrawn on the UI thread.
 */
class LinkModel(application: Application) : AndroidViewModel(application) {
    /** Redraws the screen from this model. Runs on the UI thread. */
    interface Screen {
        fun render()
    }

    private val mainHandler = Handler(Looper.getMainLooper())

    /** The screen that is open now, or null while it is being recreated or closed. */
    @Volatile var screen: Screen? = null

    /** The last messages, so a recreated screen shows them again. */
    @Volatile var joyConText = ""
    @Volatile var linkText = ""

    // The app's own update. It is checked once per start, and a download survives a recreated screen.
    @Volatile var updateText = ""
    @Volatile var updateFound: AppUpdate.Found? = null
    @Volatile var updateBusy = false
    private var updateChecked = false
    private val updateScope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    val joyCons = JoyConBle(application, object : JoyConBle.Listener {
        override fun onChanged() = redrawLater()

        override fun onStatus(text: String) {
            joyConText = text
            redrawLater()
        }
    })

    val link = LinkTransport(
        LinkClient(
            randomScalar = { Ed25519.randomScalar() },
            reportProvider = { BridgeReport.build(joyCons.left, joyCons.right, ticks()) },
            listener = object : LinkClient.Listener {
                override fun onState(state: LinkClient.State) {
                    // As the PC app does: no rumble is left over once the Deck is not connected
                    if (state != LinkClient.State.CONNECTED) joyCons.setRumble(0, 0)
                    redrawLater()
                }

                override fun onMessage(text: String) {
                    linkText = text
                    redrawLater()
                }

                override fun onRumble(small: Int, large: Int) = joyCons.setRumble(small, large)
            },
        ),
        Generated.BRIDGE_PORT,
    ) { text ->
        linkText = text
        redrawLater()
    }

    /** Looks for a newer Android build, once per app start. */
    fun checkForUpdate() {
        if (updateChecked) return
        updateChecked = true
        updateScope.launch {
            updateText = "Checking for updates..."
            redrawLater()
            val installed = BuildConfig.VERSION_NAME
            updateText = when (val check = AppUpdate.check(installed)) {
                is AppUpdate.Check.Newer -> {
                    updateFound = check.found
                    "Version ${check.found.version} is available. You have $installed."
                }
                AppUpdate.Check.UpToDate -> "You have the newest Android build ($installed)."
                AppUpdate.Check.NoBuild -> "No Android build is published on GitHub yet."
                AppUpdate.Check.Failed -> {
                    updateChecked = false // try again the next time the app starts
                    "Could not check for updates. Check the connection."
                }
            }
            redrawLater()
        }
    }

    /** Downloads the offered build, checks it, and opens the system installer. */
    fun installUpdate() {
        val found = updateFound ?: return
        if (updateBusy) return
        val app: Application = getApplication()
        // The install permission is asked for first, so a refused install does not waste a download
        if (!AppUpdate.canInstall(app)) {
            updateText = if (AppUpdate.openUnknownSourcesSettings(app)) {
                "Allow installs from this app on the screen that opened, then press Install update again."
            } else {
                "Allow installs from this app in Settings, then press Install update again."
            }
            redrawLater()
            return
        }
        updateBusy = true
        updateScope.launch {
            updateText = "Downloading ${found.version}..."
            redrawLater()
            val result = AppUpdate.download(app, found) { percent ->
                updateText = "Downloading ${found.version}... $percent%"
                redrawLater()
            }
            updateText = when (result) {
                is AppUpdate.Download.Ready ->
                    if (AppUpdate.install(app, result.file)) {
                        "Android asks you to confirm version ${found.version}."
                    } else {
                        "Could not open the installer. Try again."
                    }
                AppUpdate.Download.Mismatch ->
                    "The download did not match its checksum, so nothing was installed. Try again."
                AppUpdate.Download.NoChecksum ->
                    "This release has no checksum for the Android build, so nothing was installed."
                AppUpdate.Download.Failed -> "The download failed. Check the connection and try again."
            }
            updateBusy = false
            redrawLater()
        }
    }

    private fun redrawLater() {
        mainHandler.post { screen?.render() }
    }

    /** The Joy-Con 2 ticks in the BLE report, for the Deck's report clock (about 187.5 kHz, 16 bits). */
    private fun ticks(): Int {
        val ticks = (SystemClock.elapsedRealtimeNanos() / 1_000_000_000.0) * 187_500
        return ticks.toLong().toInt() and 0xFFFF
    }

    override fun onCleared() {
        updateScope.cancel()
        link.disconnect()
        joyCons.close()
    }
}
