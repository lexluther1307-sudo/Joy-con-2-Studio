package com.joycon2studio

import java.util.UUID

/** One Joy-Con 2: the left or the right one. */
enum class Side(val label: String) {
    LEFT("Left"),
    RIGHT("Right"),
}

/** The Bluetooth LE ids of a Joy-Con 2, from Generated.kt (which comes from the desktop app). */
object JoyConUuids {
    val SERVICE: UUID = UUID.fromString(Generated.SERVICE_UUID)
    val INPUT_REPORT: UUID = UUID.fromString(Generated.INPUT_REPORT_UUID)
    val COMMAND_WRITE: UUID = UUID.fromString(Generated.COMMAND_WRITE_UUID)
    val COMMAND_NOTIFY: UUID = UUID.fromString(Generated.COMMAND_NOTIFY_UUID)
    val CLIENT_CONFIG: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    fun rumble(side: Side): UUID =
        UUID.fromString(if (side == Side.LEFT) Generated.RUMBLE_CHAR_UUID_L else Generated.RUMBLE_CHAR_UUID_R)

    /** The second notify channel whose byte 1 holds the battery. */
    fun battery(side: Side): UUID =
        UUID.fromString(if (side == Side.LEFT) Generated.BATTERY_CHANNEL_UUID_L else Generated.BATTERY_CHANNEL_UUID_R)
}

/** Sides that get the setup sequence (the right one only, as on the PC). */
val SETUP_SIDES: Set<Side> = setOf(Side.RIGHT)

/** Hex text to bytes: "0a91" -> [0x0a, 0x91]. */
fun String.hexBytes(): ByteArray = ByteArray(length / 2) { substring(it * 2, it * 2 + 2).toInt(16).toByte() }

/** The built-in vibration command: sample 0 is silence and stops playback. */
fun vibrationCmd(sample: Int): ByteArray =
    byteArrayOf(0x0A, 0x91.toByte(), 0x01, 0x02, 0x00, 0x04, 0x00, 0x00, sample.toByte(), 0x00, 0x00, 0x00)

/** The 17-zero-byte prefix that the rumble characteristic expects before a setup command. */
fun setupPacket(command: ByteArray): ByteArray = ByteArray(17) + command

/** The setup sequence sent to the right Joy-Con, in the order the PC app sends it. */
val SETUP_PACKETS: List<ByteArray> = Generated.SETUP_COMMANDS.map { setupPacket(it.hexBytes()) }
