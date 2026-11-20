"""Joy-Con 2 Studio backend for Decky Loader on the Steam Deck (runs as root).

The Windows app reads the Joy-Cons over Bluetooth and sends their state to this Deck over Wi-Fi.
The link is encrypted with the pairing code shown on this Deck (see joycon2_link.py). The state is shown
to Steam as a virtual DualShock 4 (a UHID HID device), and Steam's rumble is sent back to the PC.
"""

import asyncio
import os
import socket
import struct
import sys
import time
import traceback

try:
    import decky

    log = decky.logger
    SETTINGS_DIR = getattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", None)
except Exception:  # outside Decky, for local checks
    import logging

    log = logging.getLogger("joycon2studio")
    logging.basicConfig(level=logging.INFO)
    SETTINGS_DIR = None

PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR") or os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)
from joycon2_link import NEUTRAL_REPORT, DeckLink, new_pin, normalize_pin  # noqa: E402

VERSION = "0.2.8"  # kept in sync by tools/build.py

BRIDGE_PORT = 26761  # the Windows app connects to this UDP port
SCAN_SECONDS = 5.0  # how long "Scan network" listens

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


def describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def own_address() -> str:
    """This Deck's address on the local network. Connecting a UDP socket sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


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
    """A DualShock 4 style HID device created through /dev/uhid (needs root)."""

    def __init__(self, loop, on_output):
        self.loop = loop
        self.on_output = on_output  # called with the raw output report (report ID first)
        self.fd = None

    def create(self):
        self.fd = os.open("/dev/uhid", os.O_RDWR | os.O_NONBLOCK)
        name = b"Wireless Controller".ljust(128, b"\0")
        empty = bytes(64)
        rd = DS4_REPORT_DESCRIPTOR.ljust(4096, b"\0")
        payload = struct.pack(
            "<128s64s64sHHIIII4096s",
            name, empty, empty,
            len(DS4_REPORT_DESCRIPTOR), BUS_USB, DS4_VENDOR, DS4_PRODUCT, 0x0100, 0, rd,
        )
        self._write(struct.pack("<I", UHID_CREATE2) + payload)
        self.loop.add_reader(self.fd, self._on_readable)

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
                log.error(f"uhid read failed: {exc}")
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
            log.info(f"uhid event {etype}")


def pairing_file() -> str:
    base = SETTINGS_DIR or os.path.expanduser("~/homebrew/settings/joycon2studio")
    return os.path.join(base, "pairing.txt")


def read_code():
    """The six-digit pairing code shown on the Deck, or None if there is none yet."""
    try:
        with open(pairing_file(), encoding="utf-8") as handle:
            return normalize_pin(handle.read())
    except (OSError, ValueError):
        return None


def save_code(text: str):
    path = pairing_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # readable by root only
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")


def load_or_create_code() -> str:
    pin = read_code()
    if pin is None:
        pin = new_pin()
        save_code(pin)
    return pin


class DeckBridge:
    """Shows the PC's controller on this Deck as a virtual DualShock 4, over the encrypted link."""

    def __init__(self):
        self.loop = None
        self.pad = None
        self.pad_message = "not started"
        self.link = None
        self.background = []
        self.message = "Starting."
        self.scanning = False
        self.scan_result = []

    def say(self, text: str):
        self.message = text
        log.info(text)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.say(f"Joy-Con 2 Studio {VERSION} on Python {sys.version.split()[0]}.")
        try:
            self.pad = UhidGamepad(self.loop, self._on_pad_output)
            self.pad.create()
            self.pad_message = "ready"
        except Exception as exc:
            self.pad = None
            self.pad_message = f"could not create the virtual controller ({describe(exc)}). Needs /dev/uhid and root."
            self.say(self.pad_message)
        self.link = DeckLink(BRIDGE_PORT, load_or_create_code(), lambda state: None, self.say, on_new_pin=save_code)
        try:
            self.link.start()
            self.say(f"Waiting for the PC on UDP port {BRIDGE_PORT}. Enter the pairing code on the PC.")
        except OSError as exc:
            self.say(f"Network port {BRIDGE_PORT} did not open: {describe(exc)}")
        self.background.append(asyncio.create_task(self._pad_loop()))

    async def stop(self):
        for task in self.background:
            task.cancel()
        self.background = []
        if self.link is not None:
            self.link.stop()
        if self.pad is not None:
            self.pad.destroy()
            self.pad = None

    async def _pad_loop(self):
        while True:
            if self.pad is not None:
                report = self.link.current_report(time.monotonic()) if self.link else NEUTRAL_REPORT
                try:
                    self.pad.send_input(report)
                except OSError as exc:
                    self.pad_message = f"virtual controller write failed: {exc}"
            await asyncio.sleep(0.01)

    def _on_pad_output(self, data: bytes):
        # Steam's rumble: report 0x05, small motor in byte 4, large motor in byte 5
        if len(data) < 6 or data[0] != 0x05 or self.link is None:
            return
        self.link.send_rumble(data[4], data[5])

    async def scan_network(self):
        """Listens for SCAN_SECONDS and reports each PC that sent valid, encrypted packets in that time."""
        self.scanning = True
        before = dict(self.link.senders) if self.link else {}
        self.say(f"Scanning the network for {SCAN_SECONDS:.0f} seconds.")
        await asyncio.sleep(SCAN_SECONDS)
        found = []
        if self.link:
            for ip, total in self.link.senders.items():
                new = total - before.get(ip, 0)
                if new > 0:
                    found.append(f"PC found at {ip} ({new} packets)")
        self.scan_result = found or [
            "No PC found. On the PC, press Connect with this Deck's address and pairing code."
        ]
        self.scanning = False
        self.say(f"Scan done: {len(found)} PC(s) found.")

    def disconnect(self):
        if self.link is not None:
            self.link.disconnect()

    def connect(self):
        if self.link is not None:
            self.link.connect()

    def new_pairing_code(self):
        pin = new_pin()
        save_code(pin)
        if self.link is not None:
            self.link.set_pin(pin)
        self.say("New pairing code made. Enter it on the PC; the old code no longer works.")

    def status(self) -> dict:
        link = self.link
        return {
            "version": VERSION,
            "pad_ok": self.pad is not None,
            "pad_message": self.pad_message,
            "link_state": link.state if link else "off",
            "pc_address": link.pc_addr[0] if (link and link.pc_addr) else "",
            "pairing_code": read_code() or "",
            "deck_address": own_address(),
            "port": BRIDGE_PORT,
            "scanning": self.scanning,
            "scan_result": list(self.scan_result),
            "message": self.message,
        }


class Plugin:
    def __init__(self):
        self.bridge = DeckBridge()

    async def _main(self):
        try:
            await self.bridge.start()
            log.info("Joy-Con 2 Studio started")
        except Exception as exc:
            self.bridge.say(f"Plugin failed to start: {describe(exc)}")
            log.exception("start failed")

    async def _unload(self):
        await self.bridge.stop()

    async def _uninstall(self):
        pass

    # Called from the Decky frontend
    async def status(self):
        try:
            return self.bridge.status()
        except Exception as exc:
            return {
                "version": VERSION, "pad_ok": False, "pad_message": f"status error: {exc}",
                "link_state": "off", "pc_address": "", "pairing_code": "", "deck_address": "",
                "port": BRIDGE_PORT, "scanning": False, "scan_result": [],
                "message": traceback.format_exc()[-400:],
            }

    async def scan(self):
        if not self.bridge.scanning:
            asyncio.get_running_loop().create_task(self.bridge.scan_network())
        return True

    async def disconnect(self):
        self.bridge.disconnect()
        return True

    async def connect(self):
        self.bridge.connect()
        return True

    async def new_pairing_code(self):
        self.bridge.new_pairing_code()
        return True
