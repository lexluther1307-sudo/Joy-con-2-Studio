"""A DualShock 4 style gamepad made through /dev/uhid. The Steam Deck plugin and the Linux app both use it.

Steam or a game reads the input reports this gamepad sends, and the rumble they send back arrives as output
reports. Creating the device needs read and write access to /dev/uhid: the Deck plugin runs as root, and the
Linux app gets access from the udev rule in packaging/linux.
"""

import logging
import os
import struct

_default_log = logging.getLogger("joycon2studio")

# /dev/uhid (linux/uhid.h)
UHID_DESTROY = 1
UHID_START = 2
UHID_STOP = 3
UHID_OPEN = 4
UHID_CLOSE = 5
UHID_OUTPUT = 6
UHID_GET_REPORT = 9
UHID_GET_REPORT_REPLY = 10
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_SET_REPORT = 13
UHID_SET_REPORT_REPLY = 14
UHID_FEATURE_REPORT = 0
UHID_OUTPUT_REPORT = 1
UHID_EVENT_SIZE = 4376  # sizeof(struct uhid_event)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # Linux only; the module can still be imported elsewhere
BUS_USB = 0x03
DS4_VENDOR = 0x054C
DS4_PRODUCT = 0x05C4

# DualShock 4 style gamepad: input report 0x01 (64 bytes), rumble output 0x05,
# feature reports 0x02 (calibration), 0x81 (MAC) and 0xA3 (version)
DS4_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID 1: input report, 63 bytes after the ID
    0x06, 0x00, 0xFF,  #   Usage Page (Vendor)
    0x09, 0x01,        #   Usage 1
    0x15, 0x00,        #   Logical Minimum 0
    0x26, 0xFF, 0x00,  #   Logical Maximum 255
    0x75, 0x08,        #   Report Size 8
    0x95, 0x3F,        #   Report Count 63
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x85, 0x05,        #   Report ID 5: output report (rumble), 31 bytes after the ID
    0x09, 0x02,        #   Usage 2
    0x95, 0x1F,        #   Report Count 31
    0x91, 0x02,        #   Output (Data, Variable, Absolute)
    0x85, 0x02,        #   Report ID 2: feature report (calibration), 36 bytes
    0x09, 0x03,        #   Usage 3
    0x95, 0x24,        #   Report Count 36
    0xB1, 0x02,        #   Feature (Data, Variable, Absolute)
    0x85, 0x81,        #   Report ID 0x81: feature report (MAC), 6 bytes
    0x09, 0x04,        #   Usage 4
    0x95, 0x06,        #   Report Count 6
    0xB1, 0x02,        #   Feature (Data, Variable, Absolute)
    0x85, 0xA3,        #   Report ID 0xA3: feature report (version), 48 bytes
    0x09, 0x05,        #   Usage 5
    0x95, 0x30,        #   Report Count 48
    0xB1, 0x02,        #   Feature (Data, Variable, Absolute)
    0xC0,              # End Collection
])


def feature_report(rnum: int) -> bytes:
    """Answers for the feature reports Steam and the kernel ask for."""
    if rnum == 0x02:
        # Calibration: gyro bias 0, gyro +-8192 raw with speed +-512 dps (16 LSB per dps),
        # accelerometer +-8192 raw for +-1 g.
        cal = bytearray(37)
        cal[0] = 0x02
        struct.pack_into("<hhhhhh", cal, 7, 8192, -8192, 8192, -8192, 8192, -8192)
        struct.pack_into("<hh", cal, 19, 512, -512)
        struct.pack_into("<hhhhhh", cal, 23, 8192, -8192, 8192, -8192, 8192, -8192)
        return bytes(cal)
    if rnum == 0x81:
        return bytes([0x81, 0x02, 0x00, 0x00, 0x00, 0x00, 0x01])
    if rnum == 0xA3:
        return bytes([0xA3]) + bytes(48)
    return bytes([rnum]) + bytes(63)


class UhidGamepad:
    """A DualShock 4 style HID device created through /dev/uhid (needs root, or access granted to the user)."""

    def __init__(self, loop, on_output, log=None):
        self.loop = loop
        self.on_output = on_output  # called with the raw output report (report ID first)
        self.log = log or _default_log
        self.fd = None

    def create(self):
        self.fd = os.open("/dev/uhid", os.O_RDWR | O_NONBLOCK)
        name = b"Wireless Controller".ljust(128, b"\0")
        empty = bytes(64)
        rd = DS4_REPORT_DESCRIPTOR.ljust(4096, b"\0")
        payload = struct.pack(
            "<128s64s64sHHIIII4096s",
            name, empty, empty,
            len(DS4_REPORT_DESCRIPTOR), BUS_USB, DS4_VENDOR, DS4_PRODUCT, 0x0100, 0, rd,
        )
        try:
            self._write(struct.pack("<I", UHID_CREATE2) + payload)
            self.loop.add_reader(self.fd, self._on_readable)
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise

    def send_input(self, report: bytes):
        if self.fd is None:
            return
        self._write(struct.pack("<IH", UHID_INPUT2, len(report)) + report.ljust(4096, b"\0"))

    def destroy(self):
        if self.fd is None:
            return
        try:
            self.loop.remove_reader(self.fd)
        except Exception:
            pass
        try:
            self._write(struct.pack("<I", UHID_DESTROY))
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None

    def _write(self, event: bytes):
        os.write(self.fd, event.ljust(UHID_EVENT_SIZE, b"\0"))

    def _on_readable(self):
        while self.fd is not None:
            try:
                ev = os.read(self.fd, 8192)
            except BlockingIOError:
                return
            except OSError as exc:
                self.log.error(f"uhid read failed: {exc}")
                return
            self._handle(ev)

    def _handle(self, ev: bytes):
        if len(ev) < 4:
            return
        etype = struct.unpack_from("<I", ev, 0)[0]
        if etype == UHID_OUTPUT:
            size = struct.unpack_from("<H", ev, 4 + 4096)[0]
            self.on_output(bytes(ev[4:4 + min(size, 4096)]))
        elif etype == UHID_GET_REPORT:
            req_id, rnum, rtype = struct.unpack_from("<IBB", ev, 4)
            if rtype == UHID_FEATURE_REPORT:
                data = feature_report(rnum)
            else:
                data = bytes([rnum]) + bytes(63)
            self._write(struct.pack("<IIHH", UHID_GET_REPORT_REPLY, req_id, 0, len(data))
                        + data.ljust(4096, b"\0"))
        elif etype == UHID_SET_REPORT:
            req_id, rnum, rtype = struct.unpack_from("<IBB", ev, 4)
            size = struct.unpack_from("<H", ev, 10)[0]
            data = bytes(ev[12:12 + min(size, 4096)])
            if rtype == UHID_OUTPUT_REPORT and data[:1] == b"\x05":
                self.on_output(data)
            self._write(struct.pack("<IIH", UHID_SET_REPORT_REPLY, req_id, 0))
        elif etype in (UHID_START, UHID_STOP, UHID_OPEN, UHID_CLOSE):
            self.log.info(f"uhid event {etype}")
