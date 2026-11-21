package com.joycon2studio

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.Build
import android.os.SystemClock
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import java.util.UUID

/**
 * Finds the Joy-Con 2s over Bluetooth LE and keeps them connected, as the PC app does. It reads the controller
 * values (buttons, sticks, motion) and sends the same setup and vibration commands. Everything runs on the main
 * thread: the Bluetooth callbacks hand their results over to it.
 */
class JoyConBle(context: Context, private val listener: Listener) {
    interface Listener {
        /** A Joy-Con connected or disconnected. */
        fun onChanged()

        /** A message for the user. */
        fun onStatus(text: String)
    }

    val left = JoyConState(Side.LEFT)
    val right = JoyConState(Side.RIGHT)

    private val appContext = context.applicationContext
    private val adapter: BluetoothAdapter? =
        (appContext.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val connections = HashMap<Side, Connection>()
    private var scanning = false
    private var scanJob: Job? = null
    @Volatile private var rumbleSmall = 0
    @Volatile private var rumbleLarge = 0

    init {
        scope.launch { rumbleLoop() }
    }

    fun stateFor(side: Side): JoyConState = if (side == Side.LEFT) left else right

    /** The Deck's rumble, from the link thread. */
    fun setRumble(small: Int, large: Int) {
        rumbleSmall = small
        rumbleLarge = large
    }

    @SuppressLint("MissingPermission")
    fun startScan() {
        val bt = adapter ?: run {
            listener.onStatus("This phone has no Bluetooth.")
            return
        }
        if (!bt.isEnabled) {
            listener.onStatus("Turn Bluetooth on, then scan again.")
            return
        }
        if (scanning) return
        if (left.connected && right.connected) {
            listener.onStatus("Both Joy-Cons are connected.")
            return
        }
        val scanner = bt.bluetoothLeScanner ?: run {
            listener.onStatus("Bluetooth scanning is not available on this phone.")
            return
        }
        scanning = true
        listener.onStatus("Scanning... Hold SYNC on one or two Joy-Con 2 controllers.")
        scanner.startScan(
            null,
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(),
            scanCallback,
        )
        scanJob = scope.launch {
            delay(SCAN_SECONDS * 1000L)
            stopScan()
            listener.onStatus(
                if (left.connected || right.connected) "Scan finished."
                else "Scan finished: no Joy-Con 2 found. Hold SYNC and try again.",
            )
        }
    }

    @SuppressLint("MissingPermission")
    fun stopScan() {
        if (!scanning) return
        scanning = false
        scanJob?.cancel()
        adapter?.bluetoothLeScanner?.stopScan(scanCallback)
    }

    /** Disconnects both Joy-Cons and stops scanning. */
    @SuppressLint("MissingPermission")
    fun disconnectAll() {
        stopScan()
        for (conn in connections.values.toList()) {
            conn.job?.cancel()
            conn.gatt?.disconnect()
        }
        connections.clear()
        left.reset()
        right.reset()
        listener.onChanged()
        listener.onStatus("Disconnected the Joy-Cons.")
    }

    /** Releases everything when the screen closes. */
    fun close() {
        stopScan()
        for (conn in connections.values.toList()) {
            conn.job?.cancel()
            conn.gatt?.close()
        }
        connections.clear()
        scope.cancel()
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            scope.launch { found(result) }
        }

        override fun onScanFailed(errorCode: Int) {
            scope.launch {
                scanning = false
                scanJob?.cancel() // so this scan's timer cannot stop a later scan early
                scanJob = null
                listener.onStatus("Scan failed (error $errorCode).")
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun found(result: ScanResult) {
        if (!scanning) return // a result that arrived after the scan was stopped
        val device = result.device ?: return
        val record = result.scanRecord
        val payloads = ArrayList<ByteArray>()
        val companyIds = HashSet<Int>()
        val manufacturer = record?.manufacturerSpecificData
        if (manufacturer != null) {
            for (i in 0 until manufacturer.size()) {
                companyIds.add(manufacturer.keyAt(i))
                payloads.add(manufacturer.valueAt(i))
            }
        }
        val hasService = record?.serviceUuids?.any { it.uuid == JoyConUuids.SERVICE } == true
        val name = device.name ?: ""
        val isJoyCon = companyIds.any { it in Generated.NINTENDO_COMPANY_IDS } ||
            hasService || name.lowercase().contains("joy-con")
        if (!isJoyCon) return
        val side = detectSide(payloads, name)
            ?: if (!left.connected && !connections.containsKey(Side.LEFT)) Side.LEFT else Side.RIGHT
        if (connections.containsKey(side) || stateFor(side).connected) return
        if (connections.values.any { it.device.address == device.address }) return
        connect(device, side)
    }

    @SuppressLint("MissingPermission")
    private fun connect(device: BluetoothDevice, side: Side) {
        val conn = Connection(side, device)
        connections[side] = conn
        listener.onStatus("Found the ${side.label} Joy-Con 2. Connecting...")
        val gatt: BluetoothGatt? = device.connectGatt(appContext, false, GattCallbacks(conn), BluetoothDevice.TRANSPORT_LE)
        if (gatt == null) {
            // no callback follows a null, so the entry would block this side until Disconnect
            connections.remove(side)
            listener.onStatus("The ${side.label} Joy-Con 2 could not be connected. Try again.")
            return
        }
        conn.gatt = gatt
    }

    /** One Joy-Con's Bluetooth connection, with the lock that keeps its requests in order. */
    private inner class Connection(val side: Side, val device: BluetoothDevice) {
        val lock = Mutex()
        var gatt: BluetoothGatt? = null
        var pending: CompletableDeferred<Int>? = null
        var job: Job? = null
        @Volatile var ready = false
    }

    private inner class GattCallbacks(private val conn: Connection) : BluetoothGattCallback() {
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            scope.launch {
                if (status == BluetoothGatt.GATT_SUCCESS && newState == BluetoothProfile.STATE_CONNECTED) {
                    // a connection that disconnectAll() already removed is not set up; its disconnect still closes it
                    if (connections[conn.side] === conn) conn.job = scope.launch { setUp(conn) }
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    dropped(conn)
                }
            }
        }

        override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
            conn.pending?.complete(status)
        }

        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            conn.pending?.complete(status)
        }

        override fun onDescriptorWrite(gatt: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
            conn.pending?.complete(status)
        }

        override fun onCharacteristicWrite(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
            conn.pending?.complete(status)
        }

        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            input(conn, characteristic.uuid, value.copyOf())
        }

        // Android 12 and older call this one instead of the one above
        @Suppress("OVERRIDE_DEPRECATION", "DEPRECATION")
        override fun onCharacteristicChanged(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
            val value = characteristic.value ?: return
            input(conn, characteristic.uuid, value.copyOf())
        }
    }

    private fun input(conn: Connection, uuid: UUID, data: ByteArray) {
        if (uuid != JoyConUuids.INPUT_REPORT) return
        scope.launch { JoyConInput.apply(stateFor(conn.side), data) }
    }

    /** Sends the same setup as the PC app, then marks the Joy-Con as ready. */
    private suspend fun setUp(conn: Connection) {
        val side = conn.side
        // The 63-byte input reports need a bigger packet size than the default
        if (conn.request { it.requestMtu(247) } != BluetoothGatt.GATT_SUCCESS) {
            return failed(conn, "its packet size could not be raised")
        }
        if (conn.request { it.discoverServices() } != BluetoothGatt.GATT_SUCCESS) {
            return failed(conn, "its services could not be read")
        }
        if (conn.gatt?.getService(JoyConUuids.SERVICE) == null) return failed(conn, "it is not a Joy-Con 2")
        conn.enableNotify(JoyConUuids.COMMAND_NOTIFY) // the replies to commands; the controller does not need them
        if (!conn.enableNotify(JoyConUuids.INPUT_REPORT)) return failed(conn, "its input could not be subscribed")
        // As the PC app: a refused command ends the connection; the setup packets stop at the first refusal
        if (!conn.write(JoyConUuids.COMMAND_WRITE, Generated.CMD_SET_LED_P1.hexBytes())) {
            return failed(conn, "the LED command was refused")
        }
        delay(100)
        if (!conn.write(JoyConUuids.COMMAND_WRITE, Generated.CMD_SET_FEATURES_IMU.hexBytes())) {
            return failed(conn, "the feature command was refused")
        }
        delay(450)
        if (!conn.write(JoyConUuids.COMMAND_WRITE, Generated.CMD_ENABLE_FEATURES_IMU.hexBytes())) {
            return failed(conn, "the IMU command was refused")
        }
        delay(100)
        if (side in SETUP_SIDES) {
            for (packet in SETUP_PACKETS) {
                if (!conn.write(JoyConUuids.rumble(side), packet)) {
                    listener.onStatus("The ${side.label} Joy-Con 2 setup stopped early.")
                    break
                }
                delay(80)
            }
        }
        if (!conn.write(JoyConUuids.COMMAND_WRITE, Generated.CMD_READ_COLORS.hexBytes())) {
            return failed(conn, "the colour read was refused")
        }
        stateFor(side).connected = true
        conn.ready = true
        listener.onStatus("Connected the ${side.label} Joy-Con 2.")
        if (left.connected && right.connected) stopScan() // both found: the scanner can stop
        listener.onChanged()
    }

    private fun failed(conn: Connection, reason: String) {
        listener.onStatus("The ${conn.side.label} Joy-Con 2 could not be used: $reason.")
        conn.gatt?.disconnect()
    }

    private fun dropped(conn: Connection) {
        // Cancel first: completing the pending request would otherwise let the setup run on before it is cancelled
        conn.job?.cancel()
        conn.pending?.complete(-1)
        conn.gatt?.close()
        conn.gatt = null
        conn.ready = false
        // A connection that disconnectAll() already removed must not reset its side, which may hold a newer Joy-Con
        if (connections[conn.side] !== conn) return
        connections.remove(conn.side)
        stateFor(conn.side).reset()
        listener.onStatus("The ${conn.side.label} Joy-Con 2 disconnected.")
        listener.onChanged()
    }

    /** Turns the Deck's rumble into the Joy-Cons' built-in vibration samples, as the PC app does. */
    private suspend fun rumbleLoop() {
        var wasActive = false
        var lastPlay = 0L
        while (true) {
            val large = rumbleLarge
            val small = rumbleSmall
            val active = maxOf(large, small) > 16
            val now = SystemClock.elapsedRealtime()
            val joycons = connections.values.filter { it.ready }
            if (active && (!wasActive || now - lastPlay > 900)) {
                val sample = if (large >= small) 1 else 2
                for (conn in joycons) conn.write(JoyConUuids.COMMAND_WRITE, vibrationCmd(sample))
                lastPlay = now
            } else if (wasActive && !active) {
                for (conn in joycons) conn.write(JoyConUuids.COMMAND_WRITE, vibrationCmd(0))
            }
            wasActive = active
            delay(50)
        }
    }

    /** Runs one Bluetooth request and waits for its answer. Returns the GATT status, or -1 if it did not finish. */
    private suspend fun Connection.request(action: (BluetoothGatt) -> Boolean): Int = lock.withLock {
        val g = gatt ?: return@withLock -1
        val done = CompletableDeferred<Int>()
        pending = done
        val started = action(g)
        val status = if (!started) -1 else (withTimeoutOrNull(REQUEST_MILLIS) { done.await() } ?: -1)
        pending = null
        status
    }

    private suspend fun Connection.write(uuid: UUID, bytes: ByteArray): Boolean {
        val g = gatt ?: return false
        val ch = findCharacteristic(g, uuid) ?: return false
        return request { it.writeCharacteristicCompat(ch, bytes) } == BluetoothGatt.GATT_SUCCESS
    }

    @SuppressLint("MissingPermission")
    private suspend fun Connection.enableNotify(uuid: UUID): Boolean {
        val g = gatt ?: return false
        val ch = findCharacteristic(g, uuid) ?: return false
        if (!g.setCharacteristicNotification(ch, true)) return false
        val config = ch.getDescriptor(JoyConUuids.CLIENT_CONFIG) ?: return false
        return request { it.writeDescriptorCompat(config) } == BluetoothGatt.GATT_SUCCESS
    }

    private fun findCharacteristic(g: BluetoothGatt, uuid: UUID): BluetoothGattCharacteristic? {
        val services: List<BluetoothGattService> = g.services ?: return null
        for (service in services) {
            val ch = service.getCharacteristic(uuid)
            if (ch != null) return ch
        }
        return null
    }

    private companion object {
        const val SCAN_SECONDS = 12
        const val REQUEST_MILLIS = 5_000L
    }
}

@SuppressLint("MissingPermission")
private fun BluetoothGatt.writeCharacteristicCompat(ch: BluetoothGattCharacteristic, value: ByteArray): Boolean {
    if (Build.VERSION.SDK_INT >= 33) {
        return writeCharacteristic(ch, value, BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE) ==
            BluetoothStatusCodes.SUCCESS
    }
    @Suppress("DEPRECATION")
    ch.value = value
    @Suppress("DEPRECATION")
    ch.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
    @Suppress("DEPRECATION")
    return writeCharacteristic(ch)
}

@SuppressLint("MissingPermission")
private fun BluetoothGatt.writeDescriptorCompat(descriptor: BluetoothGattDescriptor): Boolean {
    if (Build.VERSION.SDK_INT >= 33) {
        return writeDescriptor(descriptor, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE) ==
            BluetoothStatusCodes.SUCCESS
    }
    @Suppress("DEPRECATION")
    descriptor.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
    @Suppress("DEPRECATION")
    return writeDescriptor(descriptor)
}

/** The Joy-Con a scan result is, from its manufacturer data, as joycon2_gui.py's _detect_side. Null when unknown. */
internal fun detectSide(manufacturer: List<ByteArray>, name: String): Side? {
    for (payload in manufacturer) {
        if (containsPair(payload, 0x67, 0x20) || containsPair(payload, 0x20, 0x67)) return Side.LEFT
        if (containsPair(payload, 0x66, 0x20) || containsPair(payload, 0x20, 0x66)) return Side.RIGHT
    }
    val lower = name.lowercase()
    if ("(l)" in lower || "left" in lower) return Side.LEFT
    if ("(r)" in lower || "right" in lower) return Side.RIGHT
    return null
}

internal fun containsPair(data: ByteArray, first: Int, second: Int): Boolean {
    for (i in 0 until data.size - 1) {
        if ((data[i].toInt() and 0xFF) == first && (data[i + 1].toInt() and 0xFF) == second) return true
    }
    return false
}
