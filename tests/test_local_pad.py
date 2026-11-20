"""Tests for this PC's own DualShock 4 report, which the Linux app sends through /dev/uhid.

The report is built from the same Joy-Con state the Windows app maps to ViGEm, so these checks need no device.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import joycon2_gui as g  # noqa: E402


def new_manager():
    return g.JoyCon2Manager(lambda text: None)


class LocalPadReportTests(unittest.TestCase):
    def test_nothing_connected_is_a_centred_pad(self):
        rep = new_manager()._local_ds4_report()
        self.assertEqual(len(rep), 64)
        self.assertEqual(rep[0], 0x01)
        self.assertEqual(list(rep[1:5]), [128, 128, 128, 128])
        self.assertEqual(rep[5], 8)  # d-pad centred, no face buttons
        self.assertEqual(rep[6:10], bytes(4))

    def test_a_face_button_and_the_trigger_are_mapped(self):
        m = new_manager()
        m.right.connected = True
        m.right.buttons = g.BTN_A | g.BTN_ZR
        rep = m._local_ds4_report()
        self.assertTrue(rep[5] & 0x40)  # A is Circle
        self.assertTrue(rep[6] & 0x08)  # ZR is R2 as a button
        self.assertEqual(rep[9], 255)  # and R2 is full on the analogue byte

    def test_home_and_capture_are_the_ps_and_touchpad_bits(self):
        m = new_manager()
        m.left.connected = True
        m.left.buttons = g.BTN_HOME | g.BTN_CAPTURE
        self.assertEqual(m._local_ds4_report()[7], 0x03)

    def test_a_stick_inside_the_dead_zone_stays_centred(self):
        m = new_manager()
        m.left.connected = True
        m.left.center_x = 2000
        m.left.stick_x = 2010
        self.assertEqual(m._local_ds4_report()[1], 128)

    def test_a_stick_pushed_right_moves_the_byte_up(self):
        m = new_manager()
        m.left.connected = True
        m.left.center_x = 2000
        m.left.stick_x = 3500
        self.assertGreater(m._local_ds4_report()[1], 200)

    def test_steam_rumble_sets_both_motors(self):
        m = new_manager()
        m._on_local_rumble(bytes([0x05, 0x07, 0x00, 0x00, 0x30, 0x90]))
        self.assertEqual((m.rumble_small, m.rumble_large), (0x30, 0x90))

    def test_other_output_reports_are_ignored(self):
        m = new_manager()
        m._on_local_rumble(bytes([0x02, 0x00, 0x00, 0x00, 0x30, 0x90]))
        self.assertEqual((m.rumble_small, m.rumble_large), (0, 0))


if __name__ == "__main__":
    unittest.main()
