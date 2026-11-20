"""A virtual mouse for Linux, made through /dev/uinput.

The Joy-Cons move the pointer and click through this device. Creating it needs read and write access to
/dev/uinput: the Linux packages install a udev rule that gives the logged-in user that access.
"""

import os
import struct

try:
    import fcntl
except ImportError:  # not Linux: the mouse cannot be made, and the app says so
    fcntl = None

O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # Linux only; the module can still be imported elsewhere
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
SYN_REPORT = 0
REL_X = 0x00
REL_Y = 0x01
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BUS_USB = 0x03
VENDOR = 0x1209  # pid.codes, the open test vendor ID
PRODUCT = 0x0001
NAME = b"Joy-Con 2 Studio mouse"

# linux/uinput.h: _IO('U', n) for the plain requests, _IOW('U', n, size) for the ones that take a value
UI_DEV_CREATE = 0x5501  # _IO('U', 1)
UI_DEV_DESTROY = 0x5502  # _IO('U', 2)
UI_DEV_SETUP = 0x405C5503  # _IOW('U', 3, struct uinput_setup), 92 bytes
UI_SET_EVBIT = 0x40045564  # _IOW('U', 100, int)
UI_SET_KEYBIT = 0x40045565  # _IOW('U', 101, int)
UI_SET_RELBIT = 0x40045566  # _IOW('U', 102, int)

# struct input_event: a timeval (two C longs on 64-bit and 32-bit Linux), then type, code and value
EVENT = struct.Struct("@llHHi")
SETUP = struct.Struct("<HHHH80sI")  # struct uinput_setup: struct input_id, name[80], ff_effects_max


class UinputMouse:
    def __init__(self):
        self.fd = None

    def create(self):
        if fcntl is None:
            raise OSError("a virtual mouse needs Linux")
        fd = os.open("/dev/uinput", os.O_WRONLY | O_NONBLOCK)
        try:
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
            for code in (BTN_LEFT, BTN_RIGHT):
                fcntl.ioctl(fd, UI_SET_KEYBIT, code)
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
            for code in (REL_X, REL_Y):
                fcntl.ioctl(fd, UI_SET_RELBIT, code)
            fcntl.ioctl(fd, UI_DEV_SETUP, SETUP.pack(BUS_USB, VENDOR, PRODUCT, 1, NAME.ljust(80, b"\0"), 0))
            fcntl.ioctl(fd, UI_DEV_CREATE)
        except Exception:
            os.close(fd)
            raise
        self.fd = fd

    def move(self, dx: int, dy: int):
        if dx:
            self._event(EV_REL, REL_X, int(dx))
        if dy:
            self._event(EV_REL, REL_Y, int(dy))
        if dx or dy:
            self._event(EV_SYN, SYN_REPORT, 0)

    def button(self, button: str, down: bool):
        code = {"left": BTN_LEFT, "right": BTN_RIGHT}[button]
        self._event(EV_KEY, code, 1 if down else 0)
        self._event(EV_SYN, SYN_REPORT, 0)

    def close(self):
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None

    def _event(self, etype: int, code: int, value: int):
        if self.fd is not None:
            os.write(self.fd, EVENT.pack(0, 0, etype, code, value))
