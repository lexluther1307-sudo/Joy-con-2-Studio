package com.joycon2studio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Checks the zeroing of the sticks and gyro, and the Deck address check. The generated vectors do not cover them. */
class CalibrationAndAddressTest {
    @Test
    fun deckAddressMustBeAPlainIpv4() {
        assertTrue(isPlainIpv4("192.168.1.20"))
        assertTrue(isPlainIpv4("0.0.0.0"))
        assertTrue(isPlainIpv4("255.255.255.255"))
        assertFalse(isPlainIpv4("192.168.1.300"))
        assertFalse(isPlainIpv4("999.1.1.1"))
        assertFalse(isPlainIpv4("192.168.001.020"))
        assertFalse(isPlainIpv4("192.168.1"))
        assertFalse(isPlainIpv4("192.168.1.2.3"))
        assertFalse(isPlainIpv4("192.168.1.x"))
        assertFalse(isPlainIpv4(""))
    }

    @Test
    fun calibrateZeroesTheSticksAndTheGyro() {
        val state = JoyConState(Side.LEFT)
        state.connected = true
        state.stickX = 1500
        state.stickY = 2600
        state.gyroX = 1.5
        state.gyroY = -0.5
        state.gyroZ = 0.25
        state.calibrate()
        assertEquals(1500, state.centerX)
        assertEquals(2600, state.centerY)
        assertEquals(0, BridgeReport.normAxis(state.stickX, state.centerX))
        assertEquals(1.5, state.gyroBiasX, 1e-12)
        assertEquals(-0.5, state.gyroBiasY, 1e-12)
        assertEquals(0.25, state.gyroBiasZ, 1e-12)

        // The next report has a raw gyro X of 1000. The reading is the raw value times the scale, less the zero.
        val data = ByteArray(64)
        data[54] = (1000 and 0xFF).toByte()
        data[55] = ((1000 shr 8) and 0xFF).toByte()
        JoyConInput.apply(state, data)
        assertEquals(1000 * (2000.0 / 32768.0) - 1.5, state.gyroX, 1e-9)
    }

    @Test
    fun calibrateDoesNothingWhileTheJoyConIsNotConnected() {
        val state = JoyConState(Side.RIGHT)
        state.stickX = 1500
        state.gyroX = 1.5
        state.calibrate()
        assertEquals(2048, state.centerX)
        assertEquals(0.0, state.gyroBiasX, 0.0)
    }
}
