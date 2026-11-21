package com.joycon2studio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The battery readings, as joycon2_gui.py's _battery_sampler gives them (bits 2-5 level, bit 1 charging). */
class BatteryTest {
    private fun report(power: Int) = byteArrayOf(0x00, power.toByte())

    @Test
    fun levelAndChargingComeFromTheRightBits() {
        val level = BatteryReading.parse(report((4 shl 2) or 0x02))!!
        assertEquals(4, level.bars)
        assertEquals(50, level.percent)
        assertTrue(level.charging)
    }

    @Test
    fun otherBitsDoNotChangeTheLevel() {
        val level = BatteryReading.parse(report((5 shl 2) or 0x01 or 0x80))!!
        assertEquals(5, level.bars)
        assertEquals(62, level.percent)
        assertFalse(level.charging)
    }

    @Test
    fun percentRoundsHalfToEvenLikePython() {
        // 3 bars is 37.5 per cent: Python rounds it to 38, and so must the app
        assertEquals(38, BatteryReading.parse(report(3 shl 2))!!.percent)
        // 1 bar is 12.5 per cent: Python rounds it to 12
        assertEquals(12, BatteryReading.parse(report(1 shl 2))!!.percent)
    }

    @Test
    fun fullIsOneHundredPerCent() {
        val full = BatteryReading.parse(report(8 shl 2))!!
        assertEquals(8, full.bars)
        assertEquals(100, full.percent)
        assertFalse(full.charging)
    }

    @Test
    fun aShortReportGivesNoReading() {
        assertNull(BatteryReading.parse(byteArrayOf(0x00)))
        assertNull(BatteryReading.parse(ByteArray(0)))
    }
}
