"""Tests for the /dev/uhid gamepad. The device file is replaced by a fake, so these run anywhere."""

import os
import struct
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(os.path.dirname(HERE), "decky", "joycon2studio")
sys.path.insert(0, PLUGIN_DIR)

import uhid_pad  # noqa: E402


class UhidTests(unittest.TestCase):
    def test_the_descriptor_describes_a_gamepad(self):
        self.assertEqual(uhid_pad.DS4_REPORT_DESCRIPTOR[:4], bytes([0x05, 0x01, 0x09, 0x05]))
        self.assertEqual(uhid_pad.DS4_REPORT_DESCRIPTOR[-1], 0xC0)

    def test_feature_reports_have_their_sizes(self):
        self.assertEqual(len(uhid_pad.feature_report(0x81)), 7)
        self.assertEqual(uhid_pad.feature_report(0x81)[0], 0x81)
        self.assertEqual(len(uhid_pad.feature_report(0x02)), 37)
        self.assertEqual(len(uhid_pad.feature_report(0xA3)), 49)

    def test_create_send_and_destroy_write_the_right_events(self):
        written = []
        loop = mock.Mock()
        with mock.patch.object(uhid_pad.os, "open", return_value=7), \
                mock.patch.object(uhid_pad.os, "write", side_effect=lambda fd, data: written.append(data) or len(data)), \
                mock.patch.object(uhid_pad.os, "close"):
            pad = uhid_pad.UhidGamepad(loop, on_output=lambda data: None)
            pad.create()
            self.assertEqual(len(written[0]), uhid_pad.UHID_EVENT_SIZE)
            self.assertEqual(struct.unpack_from("<I", written[0], 0)[0], uhid_pad.UHID_CREATE2)
            loop.add_reader.assert_called_once()

            report = bytes([0x01]) + bytes(63)
            pad.send_input(report)
            event = written[-1]
            self.assertEqual(struct.unpack_from("<I", event, 0)[0], uhid_pad.UHID_INPUT2)
            self.assertEqual(struct.unpack_from("<H", event, 4)[0], 64)
            self.assertEqual(event[6:70], report)

            pad.destroy()
            self.assertEqual(struct.unpack_from("<I", written[-1], 0)[0], uhid_pad.UHID_DESTROY)
            self.assertIsNone(pad.fd)

    def test_rumble_in_an_output_event_reaches_the_callback(self):
        got = []
        pad = uhid_pad.UhidGamepad(mock.Mock(), on_output=got.append)
        data = bytes([0x05, 0x07, 0x00, 0x00, 0x30, 0x90])
        event = (struct.pack("<I", uhid_pad.UHID_OUTPUT) + data.ljust(4096, b"\0")
                 + struct.pack("<HB", len(data), 1))
        pad._handle(event)
        self.assertEqual(got, [data])

    def test_rumble_in_a_set_report_reaches_the_callback_and_is_answered(self):
        got = []
        pad = uhid_pad.UhidGamepad(mock.Mock(), on_output=got.append)
        pad.fd = 3
        data = bytes([0x05, 0x07, 0x00, 0x00, 0x30, 0x90])
        event = (struct.pack("<IIBBH", uhid_pad.UHID_SET_REPORT, 42, 0x05, uhid_pad.UHID_OUTPUT_REPORT, len(data))
                 + data.ljust(4096, b"\0"))
        answers = []
        with mock.patch.object(uhid_pad.os, "write", side_effect=lambda fd, d: answers.append(d) or len(d)):
            pad._handle(event)
        self.assertEqual(got, [data])
        self.assertEqual(struct.unpack_from("<IIH", answers[0], 0),
                         (uhid_pad.UHID_SET_REPORT_REPLY, 42, 0))


if __name__ == "__main__":
    unittest.main()
