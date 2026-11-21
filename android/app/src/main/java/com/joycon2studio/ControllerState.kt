package com.joycon2studio

import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/** The latest values of one Joy-Con 2. Read from the link thread, so the fields are volatile. */
class JoyConState(val side: Side) {
    @Volatile var connected = false
    @Volatile var buttons = 0
    @Volatile var stickX = 2048
    @Volatile var stickY = 2048
    @Volatile var centerX = 2048
    @Volatile var centerY = 2048
    // The gyro reading when the Joy-Con was zeroed. It is taken off every reading, as the PC app does.
    @Volatile var gyroBiasX = 0.0
    @Volatile var gyroBiasY = 0.0
    @Volatile var gyroBiasZ = 0.0
    @Volatile var gyroX = 0.0
    @Volatile var gyroY = 0.0
    @Volatile var gyroZ = 0.0
    @Volatile var accelX = 0.0
    @Volatile var accelY = 0.0
    @Volatile var accelZ = 0.0

    /** Sets the stick centre and the gyro zero to the current readings, as joycon2_gui.py's calibrate does. */
    fun calibrate() {
        if (!connected) return
        centerX = stickX
        centerY = stickY
        gyroBiasX = gyroX + gyroBiasX
        gyroBiasY = gyroY + gyroBiasY
        gyroBiasZ = gyroZ + gyroBiasZ
    }

    fun reset() {
        connected = false
        buttons = 0
        stickX = centerX
        stickY = centerY
        gyroX = 0.0
        gyroY = 0.0
        gyroZ = 0.0
        accelX = 0.0
        accelY = 0.0
        accelZ = 0.0
    }
}

/** Decodes Joy-Con 2 input reports the way joycon2_gui.py's _parse_input_report does (the controller values). */
object JoyConInput {
    private const val DPS_SCALE = 2000.0 / 32768.0
    private const val ACCEL_SCALE = 8.0 / 32767.0

    fun apply(state: JoyConState, data: ByteArray) {
        if (data.size < 16) return
        val buf = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN)

        // Bytes 4..8: the 32-bit button field
        state.buttons = buf.getInt(4)

        // Bytes 10..15: two 12-bit sticks. Each Joy-Con only moves its own stick.
        val (lx, ly) = decodeStick(u(data, 10), u(data, 11), u(data, 12))
        val (rx, ry) = decodeStick(u(data, 13), u(data, 14), u(data, 15))
        if (state.side == Side.LEFT && (lx != 0 || ly != 0)) {
            state.stickX = lx
            state.stickY = ly
        } else if (state.side == Side.RIGHT && (rx != 0 || ry != 0)) {
            state.stickX = rx
            state.stickY = ry
        }

        // Bytes 48..59: accelerometer then gyroscope, little-endian int16 each
        if (data.size >= 60) {
            state.accelX = buf.getShort(48).toInt() * ACCEL_SCALE
            state.accelY = buf.getShort(50).toInt() * ACCEL_SCALE
            state.accelZ = buf.getShort(52).toInt() * ACCEL_SCALE
            state.gyroX = buf.getShort(54).toInt() * DPS_SCALE - state.gyroBiasX
            state.gyroY = buf.getShort(56).toInt() * DPS_SCALE - state.gyroBiasY
            state.gyroZ = buf.getShort(58).toInt() * DPS_SCALE - state.gyroBiasZ
        }
    }

    private fun u(data: ByteArray, index: Int): Int = data[index].toInt() and 0xFF
}

/** A 12-bit stick from three bytes, as joycon2_gui.py's decode_stick. */
fun decodeStick(b0: Int, b1: Int, b2: Int): Pair<Int, Int> {
    val x = b0 or ((b1 and 0x0F) shl 8)
    val y = (b1 shr 4) or (b2 shl 4)
    return Pair(x, y)
}

/** Builds the 64-byte DualShock 4 report the Deck receives, as joycon2_gui.py's _bridge_report does. */
object BridgeReport {
    private const val DPAD_NONE = 8

    fun build(left: JoyConState, right: JoyConState, ticks: Int): ByteArray {
        val b = (if (left.connected) left.buttons else 0) or (if (right.connected) right.buttons else 0)
        val lx = if (left.connected) normAxis(left.stickX, left.centerX) else 0
        val ly = if (left.connected) normAxis(left.stickY, left.centerY) else 0
        val rx = if (right.connected) normAxis(right.stickX, right.centerX) else 0
        val ry = if (right.connected) normAxis(right.stickY, right.centerY) else 0
        val rep = ByteArray(64)
        rep[0] = 0x01
        rep[1] = stickByte(lx).toByte()
        rep[2] = stickByte(-ly).toByte()
        rep[3] = stickByte(rx).toByte()
        rep[4] = stickByte(-ry).toByte()

        var face = dpadNibble(
            (b and Generated.BTN_UP) != 0,
            (b and Generated.BTN_DOWN) != 0,
            (b and Generated.BTN_LEFT) != 0,
            (b and Generated.BTN_RIGHT) != 0,
        )
        for ((mask, bit) in listOf(
            Generated.BTN_Y to 0x10, Generated.BTN_B to 0x20, Generated.BTN_A to 0x40, Generated.BTN_X to 0x80,
        )) {
            if ((b and mask) != 0) face = face or bit
        }
        rep[5] = face.toByte()

        var shoulders = 0
        for ((mask, bit) in listOf(
            Generated.BTN_L to 0x01, Generated.BTN_R to 0x02, Generated.BTN_ZL to 0x04, Generated.BTN_ZR to 0x08,
            Generated.BTN_MINUS to 0x10, Generated.BTN_PLUS to 0x20, Generated.BTN_L3 to 0x40, Generated.BTN_R3 to 0x80,
        )) {
            if ((b and mask) != 0) shoulders = shoulders or bit
        }
        rep[6] = shoulders.toByte()
        val system = (if ((b and Generated.BTN_HOME) != 0) 0x01 else 0) or
            (if ((b and Generated.BTN_CAPTURE) != 0) 0x02 else 0)
        rep[7] = system.toByte()
        rep[8] = if ((b and Generated.BTN_ZL) != 0) 0xFF.toByte() else 0.toByte()
        rep[9] = if ((b and Generated.BTN_ZR) != 0) 0xFF.toByte() else 0.toByte()
        writeShortLE(rep, 10, ticks and 0xFFFF)

        val driver: JoyConState? = if (right.connected) right else if (left.connected) left else null
        if (driver != null) {
            writeShortLE(rep, 13, clamp16((driver.gyroX * 16).toInt()))
            writeShortLE(rep, 15, clamp16((driver.gyroY * 16).toInt()))
            writeShortLE(rep, 17, clamp16((driver.gyroZ * 16).toInt()))
            writeShortLE(rep, 19, clamp16((driver.accelX * 8192).toInt()))
            writeShortLE(rep, 21, clamp16((driver.accelY * 8192).toInt()))
            writeShortLE(rep, 23, clamp16((driver.accelZ * 8192).toInt()))
        }
        rep[30] = 0x1B
        return rep
    }

    private fun dpadNibble(up: Boolean, down: Boolean, left: Boolean, right: Boolean): Int {
        val mask = (if (up) 1 else 0) or (if (down) 2 else 0) or (if (left) 4 else 0) or (if (right) 8 else 0)
        return Generated.DPAD_NIBBLE[mask] ?: DPAD_NONE
    }

    /** A DualShock 4 stick byte: 128 is the centre, as joycon2_gui.py's stick_byte (Python rounds half to even). */
    fun stickByte(v: Int): Int {
        val scaled = Math.rint((v / 32767.0) * 127.0).toInt()
        return max(0, min(255, 128 + scaled))
    }

    /** The Joy-Con stick value (about -32767..32767) from its raw reading, with a dead zone. */
    fun normAxis(raw: Int, center: Int, deadzone: Int = 140, span: Double = 1750.0): Int {
        val delta = raw - center
        if (abs(delta) < deadzone) return 0
        val scaled = max(-1.0, min(1.0, delta / span))
        return (scaled * 32767).toInt()
    }

    private fun clamp16(v: Int): Int = max(-32768, min(32767, v))

    private fun writeShortLE(rep: ByteArray, offset: Int, value: Int) {
        rep[offset] = (value and 0xFF).toByte()
        rep[offset + 1] = ((value shr 8) and 0xFF).toByte()
    }
}
