package com.joycon2studio

import android.Manifest
import android.content.Context
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.WindowManager
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.lifecycleScope
import com.joycon2studio.databinding.ActivityMainBinding
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.io.IOException

class MainActivity : AppCompatActivity(), LinkModel.Screen {
    private lateinit var binding: ActivityMainBinding
    private lateinit var model: LinkModel
    private lateinit var prefs: SharedPreferences
    private var liveJob: Job? = null

    private val joyCons: JoyConBle get() = model.joyCons
    private val link: LinkTransport get() = model.link

    private val permissionRequest =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
            if (granted.values.all { it }) {
                startScanIfReady()
            } else {
                model.joyConText = "Bluetooth permission is needed to find Joy-Cons."
                render()
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.versionText.text = "Version ${BuildConfig.VERSION_NAME} for Android"

        prefs = getSharedPreferences("deck", Context.MODE_PRIVATE)
        binding.deckIpInput.setText(prefs.getString("ip", ""))
        binding.deckCodeInput.setText(prefs.getString("code", ""))

        // The links belong to the ViewModel, so a recreated screen keeps the session
        model = ViewModelProvider(this)[LinkModel::class.java]
        model.screen = this
        model.checkForUpdate()

        binding.scanButton.setOnClickListener { scanWithPermission() }
        binding.stopJoyConsButton.setOnClickListener { joyCons.disconnectAll() }
        binding.zeroButton.setOnClickListener { zeroJoyCons() }
        binding.deckConnectButton.setOnClickListener { connectDeck() }
        binding.deckDisconnectButton.setOnClickListener { link.disconnect() }
        binding.updateButton.setOnClickListener { model.installUpdate() }
        render()
    }

    override fun onResume() {
        super.onResume()
        liveJob = lifecycleScope.launch {
            while (true) {
                refreshLive()
                delay(100)
            }
        }
    }

    override fun onPause() {
        liveJob?.cancel()
        super.onPause()
    }

    override fun onDestroy() {
        // The links stay open: the ViewModel closes them when the screen finishes, not when it is recreated
        if (::model.isInitialized && model.screen === this) model.screen = null
        super.onDestroy()
    }

    /** Redraws the status lines, the buttons and the keep-awake flag from the model. Runs on the UI thread. */
    override fun render() {
        if (model.joyConText.isNotEmpty()) binding.joyConStatus.text = model.joyConText
        if (model.linkText.isNotEmpty()) binding.linkStatus.text = model.linkText
        val state = link.state
        // The screen stays on while the Deck is using the Joy-Cons, so the link is not put to sleep
        if (state == LinkClient.State.CONFIRMING || state == LinkClient.State.CONNECTED) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
        binding.deckConnectButton.isEnabled = state == LinkClient.State.OFF
        binding.deckDisconnectButton.isEnabled = state != LinkClient.State.OFF
        binding.updateText.text = model.updateText
        binding.updateButton.isEnabled = !model.updateBusy
        binding.updateButton.visibility = if (model.updateFound != null) View.VISIBLE else View.GONE
        refreshLive()
    }

    private fun scanWithPermission() {
        val needed = if (Build.VERSION.SDK_INT >= 31) {
            arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        } else {
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }
        val missing = needed.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) {
            startScanIfReady()
        } else {
            permissionRequest.launch(missing.toTypedArray())
        }
    }

    /** Starts the Joy-Con scan, unless Android before 12 needs the Location switch on for Bluetooth scans. */
    private fun startScanIfReady() {
        if (Build.VERSION.SDK_INT < 31 && !locationIsOn()) {
            model.joyConText = "Turn on Location to scan on this Android version, then scan again."
            render()
            return
        }
        joyCons.startScan()
    }

    /** Whether the phone's Location switch is on. Only asked once the location permission is granted. */
    private fun locationIsOn(): Boolean {
        val manager = getSystemService(Context.LOCATION_SERVICE) as? LocationManager ?: return false
        return if (Build.VERSION.SDK_INT >= 28) {
            manager.isLocationEnabled
        } else {
            manager.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
                manager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
        }
    }

    /** Sets each connected Joy-Con's stick centre and gyro zero to where it is now, as the PC app's button does. */
    private fun zeroJoyCons() {
        joyCons.left.calibrate()
        joyCons.right.calibrate()
        model.joyConText = if (joyCons.left.connected || joyCons.right.connected) {
            "Sticks and gyro zeroed."
        } else {
            "Connect a Joy-Con before zeroing."
        }
        render()
    }

    private fun connectDeck() {
        val ip = binding.deckIpInput.text?.toString()?.trim().orEmpty()
        val code = binding.deckCodeInput.text?.toString().orEmpty()
        if (!isPlainIpv4(ip)) {
            model.linkText = "Enter the Deck's IP address as four numbers from 0 to 255, for example 192.168.1.20."
            render()
            return
        }
        prefs.edit().putString("ip", ip).putString("code", code).apply()
        try {
            link.connect(ip, code)
        } catch (e: IllegalArgumentException) {
            model.linkText = e.message ?: "Check the pairing code."
        } catch (e: IOException) {
            model.linkText = "Could not use the Deck's address. Check it and try again."
        } catch (e: Exception) {
            model.linkText = e.message ?: "Could not start the link."
        }
        render()
    }

    private fun refreshLive() {
        binding.leftStatus.text = statusLine(joyCons.left)
        binding.rightStatus.text = statusLine(joyCons.right)
        val lines = listOf(joyCons.left, joyCons.right).filter { it.connected }.map { liveLine(it) }
        binding.liveText.text = lines.joinToString("\n")
    }

    private fun statusLine(state: JoyConState): String {
        val base = "${state.side.label} Joy-Con: " + if (state.connected) "connected" else "not connected"
        if (!state.connected) return base
        if (state.batteryBars == 0) return "$base, battery reading..."
        return "$base, battery ${state.batteryPct}%" + if (state.charging) " (charging)" else ""
    }

    private fun liveLine(state: JoyConState): String {
        val pressed = pressedNames(state.buttons).ifEmpty { listOf("none") }.joinToString(" ")
        return "${state.side.label}: stick ${state.stickX}, ${state.stickY}  pressed: $pressed"
    }

    private companion object {
        val BUTTON_NAMES: List<Pair<Int, String>> = listOf(
            Generated.BTN_A to "A",
            Generated.BTN_B to "B",
            Generated.BTN_X to "X",
            Generated.BTN_Y to "Y",
            Generated.BTN_UP to "Up",
            Generated.BTN_DOWN to "Down",
            Generated.BTN_LEFT to "Left",
            Generated.BTN_RIGHT to "Right",
            Generated.BTN_PLUS to "+",
            Generated.BTN_MINUS to "-",
            Generated.BTN_HOME to "Home",
            Generated.BTN_CAPTURE to "Capture",
            Generated.BTN_L to "L",
            Generated.BTN_R to "R",
            Generated.BTN_ZL to "ZL",
            Generated.BTN_ZR to "ZR",
            Generated.BTN_L3 to "L3",
            Generated.BTN_R3 to "R3",
            Generated.BTN_SL_L to "SL",
            Generated.BTN_SR_L to "SR",
            Generated.BTN_SL_R to "SL",
            Generated.BTN_SR_R to "SR",
        )

        /** The names of the buttons that are pressed in a button field. */
        fun pressedNames(buttons: Int): List<String> =
            BUTTON_NAMES.filter { (buttons and it.first) != 0 }.map { it.second }
    }
}
