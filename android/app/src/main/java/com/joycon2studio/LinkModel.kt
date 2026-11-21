package com.joycon2studio

import android.app.Application
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel

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

    private fun redrawLater() {
        mainHandler.post { screen?.render() }
    }

    /** The Joy-Con 2 ticks in the BLE report, for the Deck's report clock (about 187.5 kHz, 16 bits). */
    private fun ticks(): Int {
        val ticks = (SystemClock.elapsedRealtimeNanos() / 1_000_000_000.0) * 187_500
        return ticks.toLong().toInt() and 0xFFFF
    }

    override fun onCleared() {
        link.disconnect()
        joyCons.close()
    }
}
