"""Tests for the Linux virtual mouse. /dev/uinput and fcntl are replaced by fakes, so these run on any OS."""

import ctypes
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import linux_mouse as lm  # noqa: E402


def _IO(kind, nr):
    return (ord(kind) << 8) | nr


def _IOW(kind, nr, size):
    return (1 << 30) | (size << 16) | (ord(kind) << 8) | nr


class ConstantTests(unittest.TestCase):
    def test_ioctl_numbers_match_linux_uinput_h(self):
        self.assertEqual(lm.UI_DEV_CREATE, _IO("U", 1))
        self.assertEqual(lm.UI_DEV_DESTROY, _IO("U", 2))
        self.assertEqual(lm.UI_DEV_SETUP, _IOW("U", 3, lm.SETUP.size))
        self.assertEqual(lm.UI_SET_EVBIT, _IOW("U", 100, 4))
        self.assertEqual(lm.UI_SET_KEYBIT, _IOW("U", 101, 4))
        self.assertEqual(lm.UI_SET_RELBIT, _IOW("U", 102, 4))

    def test_structs_have_the_kernel_sizes(self):
        self.assertEqual(lm.SETUP.size, 92)  # struct uinput_setup
        self.assertEqual(lm.EVENT.size, 2 * ctypes.sizeof(ctypes.c_long) + 8)  # struct input_event


class MouseTests(unittest.TestCase):
    def mouse(self):
        mouse = lm.UinputMouse()
        mouse.fd = 9
        return mouse

    def sent(self, action):
        writes = []
        with mock.patch.object(lm.os, "write", side_effect=lambda fd, data: writes.append(data) or len(data)):
            action()
        return [(etype, code, value) for (_s, _us, etype, code, value) in (lm.EVENT.unpack(w) for w in writes)]

    def test_move_sends_x_then_y_then_a_sync(self):
        mouse = self.mouse()
        self.assertEqual(self.sent(lambda: mouse.move(3, -2)),
                         [(lm.EV_REL, lm.REL_X, 3), (lm.EV_REL, lm.REL_Y, -2), (lm.EV_SYN, lm.SYN_REPORT, 0)])

    def test_move_by_nothing_sends_nothing(self):
        self.assertEqual(self.sent(lambda: self.mouse().move(0, 0)), [])

    def test_a_button_sends_its_state_then_a_sync(self):
        mouse = self.mouse()
        self.assertEqual(self.sent(lambda: mouse.button("left", True)),
                         [(lm.EV_KEY, lm.BTN_LEFT, 1), (lm.EV_SYN, lm.SYN_REPORT, 0)])
        self.assertEqual(self.sent(lambda: mouse.button("right", False)),
                         [(lm.EV_KEY, lm.BTN_RIGHT, 0), (lm.EV_SYN, lm.SYN_REPORT, 0)])

    def test_create_sets_up_the_device_and_keeps_the_handle(self):
        fake = mock.Mock()
        with mock.patch.object(lm, "fcntl", fake), mock.patch.object(lm.os, "open", return_value=5) as opened, \
                mock.patch.object(lm.os, "close") as closed:
            mouse = lm.UinputMouse()
            mouse.create()
        self.assertEqual(opened.call_args.args[0], "/dev/uinput")
        requests = [call.args[1] for call in fake.ioctl.call_args_list]
        self.assertEqual(requests[0], lm.UI_SET_EVBIT)
        self.assertEqual(requests[-1], lm.UI_DEV_CREATE)
        self.assertIn(lm.UI_DEV_SETUP, requests)
        self.assertEqual(mouse.fd, 5)
        closed.assert_not_called()

    def test_a_failed_create_closes_the_handle_and_raises(self):
        fake = mock.Mock()
        fake.ioctl.side_effect = OSError(13, "Permission denied")
        with mock.patch.object(lm, "fcntl", fake), mock.patch.object(lm.os, "open", return_value=5), \
                mock.patch.object(lm.os, "close") as closed:
            mouse = lm.UinputMouse()
            with self.assertRaises(OSError):
                mouse.create()
        closed.assert_called_once_with(5)
        self.assertIsNone(mouse.fd)

    def test_create_needs_linux(self):
        with mock.patch.object(lm, "fcntl", None):
            with self.assertRaises(OSError):
                lm.UinputMouse().create()


if __name__ == "__main__":
    unittest.main()
