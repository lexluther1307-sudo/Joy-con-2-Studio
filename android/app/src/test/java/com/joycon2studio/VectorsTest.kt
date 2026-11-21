package com.joycon2studio

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.math.BigInteger

/**
 * Checks the Android code against src/test/resources/vectors.json. tools/android_vectors.py writes that file from
 * the desktop code (joycon2_gui.py and the Deck link), so these tests fail if the two ends stop agreeing.
 */
class VectorsTest {
    private val vectors: JSONObject by lazy {
        val text = javaClass.classLoader!!.getResourceAsStream("vectors.json")!!.bufferedReader().readText()
        JSONObject(text)
    }

    private fun hex(key: String): ByteArray = vectors.getString(key).hexBytes()

    @Test
    fun basePointsAndPinMatchTheDesktop() {
        assertArrayEquals(hex("B"), Ed25519.encodePoint(Ed25519.B))
        assertArrayEquals(hex("M"), Ed25519.encodePoint(Ed25519.M))
        assertArrayEquals(hex("N"), Ed25519.encodePoint(Ed25519.N))
        assertArrayEquals(hex("w"), Ed25519.toFixedBE(Ed25519.pinScalar(vectors.getString("pin")), 32))
        assertTrue(Ed25519.equal(Ed25519.decodePoint(hex("B"))!!, Ed25519.B))
    }

    @Test
    fun sticksDecodeLikeTheDesktop() {
        val cases = vectors.getJSONArray("sticks")
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val b = c.getJSONArray("bytes")
            val (x, y) = decodeStick(b.getInt(0), b.getInt(1), b.getInt(2))
            assertEquals(c.getInt("x"), x)
            assertEquals(c.getInt("y"), y)
        }
    }

    @Test
    fun stickBytesAndAxesMatchTheDesktop() {
        val bytes = vectors.getJSONArray("stick_byte")
        for (i in 0 until bytes.length()) {
            val c = bytes.getJSONObject(i)
            assertEquals(c.getInt("byte"), BridgeReport.stickByte(c.getInt("value")))
        }
        val axes = vectors.getJSONArray("norm_axis")
        for (i in 0 until axes.length()) {
            val c = axes.getJSONObject(i)
            assertEquals(c.getInt("value"), BridgeReport.normAxis(c.getInt("raw"), c.getInt("center")))
        }
    }

    @Test
    fun vibrationCommandsMatchTheDesktop() {
        val commands = vectors.getJSONObject("vibration")
        for (key in commands.keys().asSequence()) {
            assertArrayEquals(commands.getString(key).hexBytes(), vibrationCmd(key.toInt()))
        }
    }

    @Test
    fun inputReportsDecodeLikeTheDesktop() {
        val inputs = vectors.getJSONArray("inputs")
        for (i in 0 until inputs.length()) {
            val c = inputs.getJSONObject(i)
            val side = if (c.getString("side") == "L") Side.LEFT else Side.RIGHT
            val state = JoyConState(side)
            JoyConInput.apply(state, c.getString("data").hexBytes())
            assertEquals("input $i buttons", c.getLong("buttons").toInt(), state.buttons)
            assertEquals("input $i stick x", c.getInt("stick_x"), state.stickX)
            assertEquals("input $i stick y", c.getInt("stick_y"), state.stickY)
            val gyro = c.getJSONArray("gyro")
            assertEquals(gyro.getDouble(0), state.gyroX, 1e-9)
            assertEquals(gyro.getDouble(1), state.gyroY, 1e-9)
            assertEquals(gyro.getDouble(2), state.gyroZ, 1e-9)
            val accel = c.getJSONArray("accel")
            assertEquals(accel.getDouble(0), state.accelX, 1e-9)
            assertEquals(accel.getDouble(1), state.accelY, 1e-9)
            assertEquals(accel.getDouble(2), state.accelZ, 1e-9)
        }
    }

    @Test
    fun controllerReportMatchesTheDesktop() {
        val reports = vectors.getJSONArray("reports")
        for (i in 0 until reports.length()) {
            val c = reports.getJSONObject(i)
            val left = stateFrom(Side.LEFT, c.getJSONObject("left"))
            val right = stateFrom(Side.RIGHT, c.getJSONObject("right"))
            val report = BridgeReport.build(left, right, c.getInt("ticks"))
            assertArrayEquals(c.getString("label"), c.getString("report").hexBytes(), report)
        }
    }

    @Test
    fun pairingAndConnectionMatchTheDeck() {
        val states = mutableListOf<LinkClient.State>()
        val messages = mutableListOf<String>()
        val rumble = mutableListOf<Pair<Int, Int>>()
        val x = BigInteger(1, hex("x"))
        val report = vectors.getJSONArray("reports").getJSONObject(0).getString("report").hexBytes()
        val client = LinkClient(
            randomScalar = { x },
            reportProvider = { report },
            listener = object : LinkClient.Listener {
                override fun onState(state: LinkClient.State) {
                    states.add(state)
                }

                override fun onMessage(text: String) {
                    messages.add(text)
                }

                override fun onRumble(small: Int, large: Int) {
                    rumble.add(small to large)
                }
            },
        )

        client.configure(DECK, vectors.getString("pin"))
        client.start(10.0)
        assertEquals(LinkClient.State.SEARCHING, client.state)

        val hsX = client.tick(11.0)
        assertEquals(1, hsX.size)
        assertArrayEquals(hex("hs_x_msg"), hsX[0])

        // A packet from another address is ignored
        assertTrue(client.onDatagram(hex("hs_y_msg"), "10.0.0.9", 11.1).isEmpty())
        val confirm = client.onDatagram(hex("hs_y_msg"), DECK, 11.1)
        assertEquals(1, confirm.size)
        assertArrayEquals(hex("hs_confirm_msg"), confirm[0])
        assertEquals(LinkClient.State.CONFIRMING, client.state)

        client.onDatagram(hex("deck_heartbeat"), DECK, 11.2)
        assertEquals(LinkClient.State.CONNECTED, client.state)

        val sent = client.tick(11.3)
        assertEquals(1, sent.size)
        assertArrayEquals(hex("pc_state"), sent[0])

        // A packet whose tag does not match is ignored; the next good one is used
        client.onDatagram(hex("deck_tampered"), DECK, 11.4)
        assertTrue(rumble.isEmpty())
        client.onDatagram(hex("deck_rumble"), DECK, 11.5)
        assertEquals(listOf(0x40 to 0x80), rumble)

        val bye = client.disconnect()
        assertEquals(1, bye.size)
        assertArrayEquals(hex("pc_bye"), bye[0])
        assertEquals(LinkClient.State.OFF, client.state)
    }

    @Test
    fun joyConSidesAreRecognised() {
        assertEquals(Side.LEFT, detectSide(listOf(byteArrayOf(0x67.toByte(), 0x20)), ""))
        assertEquals(Side.RIGHT, detectSide(listOf(byteArrayOf(0x00, 0x20.toByte(), 0x66.toByte())), ""))
        assertEquals(Side.LEFT, detectSide(emptyList(), "Joy-Con (L)"))
        assertEquals(Side.RIGHT, detectSide(emptyList(), "Joy-Con (R)"))
        assertNull(detectSide(emptyList(), "Headphones"))
    }

    @Test
    fun pairingCodesAreSixDigits() {
        assertEquals("042917", Ed25519.normalizePin(" 042-917 "))
        var rejected = false
        try {
            Ed25519.normalizePin("12345")
        } catch (e: IllegalArgumentException) {
            rejected = true
        }
        assertTrue(rejected)
    }

    private fun stateFrom(side: Side, c: JSONObject): JoyConState {
        val state = JoyConState(side)
        state.connected = c.getBoolean("connected")
        state.buttons = c.getLong("buttons").toInt()
        state.stickX = c.getInt("stick_x")
        state.stickY = c.getInt("stick_y")
        state.centerX = c.getInt("center_x")
        state.centerY = c.getInt("center_y")
        val gyro = c.getJSONArray("gyro")
        state.gyroX = gyro.getDouble(0)
        state.gyroY = gyro.getDouble(1)
        state.gyroZ = gyro.getDouble(2)
        val accel = c.getJSONArray("accel")
        state.accelX = accel.getDouble(0)
        state.accelY = accel.getDouble(1)
        state.accelZ = accel.getDouble(2)
        return state
    }

    private companion object {
        const val DECK = "192.168.1.20"
    }
}
