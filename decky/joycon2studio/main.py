"""Joy-Con 2 Studio backend for Decky Loader on the Steam Deck (runs as root).

Joy-Con 2 controllers connect over Bluetooth LE (BlueZ, through bleak). Their state is
published as a virtual DualShock 4: a UHID HID device, so Steam's PlayStation support sees
buttons, sticks, gyro, accelerometer and rumble. Steam's rumble arrives as HID output
reports and is played on the Joy-Cons with their built-in vibration samples.
"""

import asyncio
import os
import re
import struct
import sys
import time
import traceback

try:
    import decky

    log = decky.logger
except Exception:  # outside Decky, for local checks
    import logging

    log = logging.getLogger("joycon2studio")
    logging.basicConfig(level=logging.INFO)

# The Deck's Python has an xml package without etree. Add the etree vendored in py_modules to
# the xml package's search path; the stdlib copy is still searched first.
PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR") or os.path.dirname(os.path.abspath(__file__))
try:
    import xml

    _vendored_xml = os.path.join(PLUGIN_DIR, "py_modules", "xml")
    if os.path.isdir(_vendored_xml) and _vendored_xml not in xml.__path__:
        xml.__path__.append(_vendored_xml)
except ImportError:
    pass  # no xml package at all: py_modules is on sys.path, so its xml is used

# The plugin still loads when bleak does not; the error is shown in the plugin's status
BLEAK_ERROR = ""
try:
    from bleak import BleakClient, BleakScanner
except Exception:
    BleakClient = None
    BleakScanner = None
    BLEAK_ERROR = traceback.format_exc()[-1500:]

# Recent calls BlueZ made to our pairing agent (shown in Diagnostics)
AGENT_CALLS = []


def _agent_call(name: str, *args):
    AGENT_CALLS.append(f"{name} {' '.join(str(a) for a in args)}"[:160])
    del AGENT_CALLS[:-8]


# BlueZ pairing helper (D-Bus). If it does not load, the plugin still runs without it.
DBUS_ERROR = ""
try:
    from dbus_fast import BusType, Message, MessageType, Variant
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method

    class PairingAgent(ServiceInterface):
        """Answers BlueZ pairing requests with 'just works', since the plugin has no screen."""

        def __init__(self):
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self) -> None:
            _agent_call("Release")

        @method()
        def RequestPinCode(self, device: "o") -> "s":
            _agent_call("RequestPinCode", device)
            return "0000"

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s") -> None:
            _agent_call("DisplayPinCode", device, pincode)

        @method()
        def RequestPasskey(self, device: "o") -> "u":
            _agent_call("RequestPasskey", device)
            return 0

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:
            _agent_call("DisplayPasskey", device, passkey)

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:
            _agent_call("RequestConfirmation", device, passkey)

        @method()
        def RequestAuthorization(self, device: "o") -> None:
            _agent_call("RequestAuthorization", device)

        @method()
        def AuthorizeService(self, device: "o", uuid: "s") -> None:
            _agent_call("AuthorizeService", device, uuid)

        @method()
        def Cancel(self) -> None:
            _agent_call("Cancel")

except Exception:
    PairingAgent = None
    DBUS_ERROR = traceback.format_exc()[-800:]

AGENT_PATH = "/com/joycon2studio/agent"
CONNECT_TIMEOUT = 30.0   # seconds for one connection attempt (the Windows app needed 30 s too)
STEP_TIMEOUT = 10.0      # seconds for one setup step after connecting
PAIR_TIMEOUT = 40.0      # seconds allowed for pairing with a Joy-Con
CONNECT_TRIES = 3

# Nintendo Switch 2 BLE UUIDs (same as the Windows app)
SERVICE_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd0"
INPUT_REPORT_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
COMMAND_WRITE_UUID = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"
COMMAND_NOTIFY_UUID = "c765a961-d9d8-4d36-a20a-5315b111836a"
NINTENDO_COMPANY_IDS = {0x0553, 0x057E}
SCAN_SECONDS = 60.0

# Network bridge: a PC that keeps the Joy-Cons sends its controller state here over UDP.
BRIDGE_PORT = 26761
BRIDGE_KEY = b"JC2BRDG1"                 # must match the Windows app
BRIDGE_MAGIC = b"JCB1"
BRIDGE_HDR = struct.Struct("<4s8sBI")   # magic, key, kind (1 = state from the PC, 2 = rumble to the PC), sequence
BRIDGE_STALE = 0.5                      # seconds without a state packet before the bridge counts as idle

# joycon2cpp's setup sequence, sent to a Joy-Con's rumble characteristic on connect.
# Each packet is 17 zero bytes followed by the command.
RUMBLE_CHAR_UUID = {
    "L": "ce49a830-dced-48ae-931e-c8cf88aadbea",
    "R": "65a724b3-f1e7-4a61-8078-a342376b27ff",
}
SETUP_SIDES = ("R",)  # right only until tested on both
SETUP_COMMANDS = [bytes.fromhex(h) for h in (
    "0791010100000000",
    "0291010400080000407e000000300100",
    "1091010100000000",
    "1691010100000000",
    "0a9101020004000003000000",
    "09910107000800000100000000000000",
    "0c9101020004000037000000",
    "0291010400080000407e000080300100",
    "0291010400080000407e000040c01f00",
    "0291010400080000107e000040300100",
    "0291010400080000187e000000310100",
    "1191010300000000",
    "0291010400080000207e000060300100",
    "0a910108001400000159090000ffffffff3500460000000000000000",
    "1191010100000000",
    "0c9101040004000037000000",
)]

CMD_SET_LED_P1 = bytes.fromhex("09910107000800000100000000000000")
CMD_SET_FEATURES_IMU = bytes.fromhex("0c9101020004000037000000")
CMD_ENABLE_FEATURES_IMU = bytes.fromhex("0c9101040004000037000000")

# Battery: a second notify channel per Joy-Con; byte 1 holds bit 0 external power,
# bit 1 charging and bits 2-5 the level (0-8, where 8 is full).
BATTERY_CHANNEL = {
    "R": "d5a9e01e-2ffc-4cca-b20c-8b67142bf442",
    "L": "cc1bbbb5-7354-4d32-a716-a81cb241a32a",
}
BATTERY_SAMPLE_SECONDS = 3.0
BATTERY_EVERY_SECONDS = 300.0

# Sample songs: (vibration sample id, wait in ms before the next note). Same as the Windows app.
SONGS = [
    ("Heartbeat", [(1, 200), (1, 180), (1, 900), (1, 200), (1, 180), (1, 1200)]),
    ("Morse SOS", [(6, 160), (6, 160), (6, 400), (1, 500), (1, 500), (1, 400), (6, 160), (6, 160), (6, 600)]),
    ("Click march", [(3, 300), (3, 300), (5, 300), (3, 300), (3, 300), (5, 700)]),
    ("Beep ping-pong", [(7, 250), (6, 250)] * 4 + [(4, 800)]),
    ("Victory", [(6, 150), (6, 150), (6, 150), (2, 700), (4, 600)]),
    ("Rising buzz", [(1, 300), (2, 300), (1, 300), (2, 700)]),
    ("Secret found", [(6, 120), (6, 120), (7, 120), (7, 120), (4, 500), (2, 600)]),
    ("Pickup chime", [(7, 70), (6, 70), (7, 70), (6, 70), (7, 260)]),
]

# Joy-Con button bits (same as the Windows app)
BTN_Y = 0x00000001
BTN_X = 0x00000002
BTN_B = 0x00000004
BTN_A = 0x00000008
BTN_R = 0x00000040
BTN_ZR = 0x00000080
BTN_MINUS = 0x00000100
BTN_PLUS = 0x00000200
BTN_R3 = 0x00000400
BTN_L3 = 0x00000800
BTN_HOME = 0x00001000
BTN_CAPTURE = 0x00002000
BTN_DOWN = 0x00010000
BTN_UP = 0x00020000
BTN_RIGHT = 0x00040000
BTN_LEFT = 0x00080000
BTN_L = 0x00400000
BTN_ZL = 0x00800000

# uhid (linux/uhid.h)
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

# A gamepad HID descriptor with the DualShock 4 report IDs Steam uses:
# 0x01 input (64 bytes), 0x05 output (rumble, 32 bytes), 0x02/0x81/0xA3 feature reports.
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

DPAD_VALUES = {
    (True, False, False, False): 0,  # north
    (True, False, False, True): 1,   # north-east
    (False, False, False, True): 2,  # east
    (False, True, False, True): 3,   # south-east
    (False, True, False, False): 4,  # south
    (False, True, True, False): 5,   # south-west
    (False, False, True, False): 6,  # west
    (True, False, True, False): 7,   # north-west
}
DPAD_NONE = 8


def describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def vibration_cmd(sample_id: int) -> bytes:
    return bytes([0x0A, 0x91, 0x01, 0x02, 0x00, 0x04, 0x00, 0x00, sample_id, 0x00, 0x00, 0x00])


def decode_stick(b0: int, b1: int, b2: int) -> tuple:
    x = b0 | ((b1 & 0x0F) << 8)
    y = (b1 >> 4) | (b2 << 4)
    return x, y


def clamp16(v: int) -> int:
    return max(-32768, min(32767, v))


def stick_byte(v: int) -> int:
    # v is -32767..32767 with 0 centered; DualShock 4 sticks are 0..255 with 128 centered
    return max(0, min(255, 128 + round(v / 32767 * 127)))


def norm_axis(raw: int, center: int, deadzone: int = 140, span: float = 1750.0) -> int:
    delta = raw - center
    if abs(delta) < deadzone:
        return 0
    scaled = max(-1.0, min(1.0, delta / span))
    return int(scaled * 32767)


def dpad_value(up, down, left, right) -> int:
    return DPAD_VALUES.get((bool(up), bool(down), bool(left), bool(right)), DPAD_NONE)


def detect_side(device, adv) -> str:
    for payload in adv.manufacturer_data.values():
        if b"\x67\x20" in payload or b"\x20\x67" in payload:
            return "L"
        if b"\x66\x20" in payload or b"\x20\x66" in payload:
            return "R"
    name = (device.name or "").lower()
    if "(l)" in name or "left" in name:
        return "L"
    if "(r)" in name or "right" in name:
        return "R"
    return "UNKNOWN"


def feature_report(rnum: int) -> bytes:
    """Answers for the feature reports Steam and the kernel ask for."""
    if rnum == 0x02:
        # Calibration: gyro bias 0, gyro +-8192 raw with speed +-512 dps (16 LSB per dps),
        # accelerometer +-8192 raw for +-1 g. Values to tune if the gyro reads too fast or slow.
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


class JoyConState:
    def __init__(self, side: str):
        self.side = side  # "L" or "R"
        self.name = f"Joy-Con 2 ({side})"
        self.connected = False
        self.client = None
        self.address = ""
        self.step = ""   # the connection step in progress, shown when a try fails
        self.hint = ""   # BlueZ's view of the device, taken part-way through a try
        self.buttons = 0
        self.stick_x = self.stick_y = 2048
        self.center_x = self.center_y = 2048
        self.gyro_x = self.gyro_y = self.gyro_z = 0.0
        self.gyro_bias = [0.0, 0.0, 0.0]
        self.accel_x = self.accel_y = self.accel_z = 0.0
        self.battery_pct = 0
        self.charging = False

    def reset(self):
        self.connected = False
        self.client = None
        self.address = ""
        self.buttons = 0
        self.stick_x, self.stick_y = self.center_x, self.center_y
        self.gyro_x = self.gyro_y = self.gyro_z = 0.0
        self.accel_x = self.accel_y = self.accel_z = 0.0
        self.battery_pct = 0
        self.charging = False


class BridgeProtocol(asyncio.DatagramProtocol):
    """Receives controller state from the PC (the Windows app's network bridge)."""

    def __init__(self, studio):
        self.studio = studio

    def datagram_received(self, data, addr):
        if len(data) != BRIDGE_HDR.size + 64:
            return
        magic, key, kind, seq = BRIDGE_HDR.unpack_from(data)
        if magic != BRIDGE_MAGIC or key != BRIDGE_KEY or kind != 1:
            return
        self.studio.on_bridge_state(data[BRIDGE_HDR.size:], addr, seq)


class JoyConStudio:
    def __init__(self):
        self.loop = None
        self.left = JoyConState("L")
        self.right = JoyConState("R")
        self.pad = None
        self.pad_message = "not started"
        self.agent_bus = None
        self.agent_state = "not started"
        self.rumble_large = 0
        self.rumble_small = 0
        self.rumble_sample = 1
        self.tasks = {}          # Bluetooth address -> device worker task
        self.busy = {"L": False, "R": False}
        self.scanning = False
        self.scan_task = None
        self.scan_stats = {"devices": 0, "nintendo": 0}  # counted by the last scan
        self.background = []
        self.message = "Hold SYNC on one or two Joy-Cons, then press Scan."
        self.seen = {}     # Nintendo-looking devices found by the last scan
        self.recent = []   # last few messages, shown in the plugin
        self.bluez_log = []  # Bluetooth and kernel log lines from the last failed try
        self.bridge_transport = None
        self.bridge_addr = None
        self.bridge_report = None
        self.bridge_time = 0.0
        self.bridge_seq = -1
        self.bridge_count = 0

    def say(self, text: str):
        self.message = text
        self.recent = (self.recent + [text])[-8:]
        log.info(text)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.say(f"Started on Python {sys.version.split()[0]}.")
        try:
            self.pad = UhidGamepad(self.loop, self._on_pad_output)
            self.pad.create()
            self.pad_message = "ready"
        except Exception as exc:
            self.pad = None
            self.pad_message = f"could not create the virtual controller ({describe(exc)}). Needs /dev/uhid."
            self.say(self.pad_message)
        try:
            self.bridge_transport, _ = await self.loop.create_datagram_endpoint(
                lambda: BridgeProtocol(self), local_addr=("0.0.0.0", BRIDGE_PORT))
        except Exception as exc:
            self.say(f"Network bridge port {BRIDGE_PORT} did not open: {describe(exc)}")
        self.background.append(asyncio.create_task(self._register_agent()))
        self.background.append(asyncio.create_task(self._pad_loop()))
        self.background.append(asyncio.create_task(self._rumble_loop()))

    async def _register_agent(self):
        """Registers a 'just works' pairing agent with BlueZ, in case the Joy-Cons ask for pairing."""
        if PairingAgent is None:
            self.say("Pairing helper did not load: " + DBUS_ERROR.strip().splitlines()[-1])
            return
        try:
            self.agent_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self.agent_bus.export(AGENT_PATH, PairingAgent())
            for member, signature, body in (
                ("RegisterAgent", "os", [AGENT_PATH, "NoInputNoOutput"]),
                ("RequestDefaultAgent", "o", [AGENT_PATH]),
            ):
                reply = await self.agent_bus.call(Message(
                    destination="org.bluez", path="/org/bluez",
                    interface="org.bluez.AgentManager1", member=member,
                    signature=signature, body=body,
                ))
                if reply.message_type == MessageType.ERROR:
                    if member == "RequestDefaultAgent":
                        self.agent_state = f"registered, NOT the default agent ({reply.error_name})"
                        continue
                    raise RuntimeError(f"{member} failed: {reply.error_name}")
            if self.agent_state.startswith("registered, NOT"):
                pass
            else:
                self.agent_state = "registered and the default agent"
            log.info("BlueZ pairing agent registered")
        except Exception as exc:
            self.agent_state = f"not registered: {describe(exc)}"
            self.say(f"Pairing helper not registered: {describe(exc)}")

    async def stop(self):
        if self.bridge_transport is not None:
            self.bridge_transport.close()
            self.bridge_transport = None
        tasks = list(self.background) + list(self.tasks.values())
        if self.scan_task is not None:
            tasks.append(self.scan_task)
        for task in tasks:
            task.cancel()
        if self.pad is not None:
            self.pad.destroy()
            self.pad = None
        if self.agent_bus is not None:
            try:
                self.agent_bus.disconnect()
            except Exception:
                pass
            self.agent_bus = None

    # ---- virtual DualShock 4 -------------------------------------------------

    def _build_pad_report(self) -> bytes:
        left, right = self.left, self.right
        b = (left.buttons if left.connected else 0) | (right.buttons if right.connected else 0)
        lx = norm_axis(left.stick_x, left.center_x) if left.connected else 0
        ly = norm_axis(left.stick_y, left.center_y) if left.connected else 0
        rx = norm_axis(right.stick_x, right.center_x) if right.connected else 0
        ry = norm_axis(right.stick_y, right.center_y) if right.connected else 0

        rep = bytearray(64)
        rep[0] = 0x01
        rep[1] = stick_byte(lx)
        rep[2] = stick_byte(-ly)  # DualShock 4 Y is 0 at the top
        rep[3] = stick_byte(rx)
        rep[4] = stick_byte(-ry)

        # Byte 5: D-pad in the low nibble, then the face buttons. Nintendo positions:
        # A is on the right (Circle), B at the bottom (Cross), X on top (Triangle), Y on the left (Square)
        face = dpad_value(b & BTN_UP, b & BTN_DOWN, b & BTN_LEFT, b & BTN_RIGHT)
        if b & BTN_Y:
            face |= 0x10
        if b & BTN_B:
            face |= 0x20
        if b & BTN_A:
            face |= 0x40
        if b & BTN_X:
            face |= 0x80
        rep[5] = face

        # Byte 6: shoulders, digital triggers, Share, Options, sticks pressed
        shoulders = 0
        for mask, bit in ((BTN_L, 0x01), (BTN_R, 0x02), (BTN_ZL, 0x04), (BTN_ZR, 0x08),
                          (BTN_MINUS, 0x10), (BTN_PLUS, 0x20), (BTN_L3, 0x40), (BTN_R3, 0x80)):
            if b & mask:
                shoulders |= bit
        rep[6] = shoulders

        # Byte 7: PS (Home) and Touchpad (Capture)
        rep[7] = (0x01 if b & BTN_HOME else 0) | (0x02 if b & BTN_CAPTURE else 0)
        rep[8] = 255 if b & BTN_ZL else 0  # analog L2
        rep[9] = 255 if b & BTN_ZR else 0  # analog R2
        struct.pack_into("<H", rep, 10, int(time.monotonic() * 187500) & 0xFFFF)

        driver = right if right.connected else (left if left.connected else None)
        if driver is not None:
            struct.pack_into("<hhh", rep, 13, *(clamp16(int(v * 16)) for v in
                                                (driver.gyro_x, driver.gyro_y, driver.gyro_z)))
            struct.pack_into("<hhh", rep, 19, *(clamp16(int(v * 8192)) for v in
                                                (driver.accel_x, driver.accel_y, driver.accel_z)))
        rep[30] = 0x1B  # USB cable connected, battery full
        return bytes(rep)

    def on_bridge_state(self, report: bytes, addr, seq: int):
        if addr != self.bridge_addr:
            self.bridge_addr = addr
            self.say(f"Receiving the controller from the PC at {addr[0]}.")
        self.bridge_report = report
        self.bridge_time = time.monotonic()
        self.bridge_seq = seq
        self.bridge_count += 1

    def _bridge_active(self) -> bool:
        return self.bridge_report is not None and time.monotonic() - self.bridge_time < BRIDGE_STALE

    def _current_report(self) -> bytes:
        # The PC's state while it is sending; otherwise this Deck's own Joy-Cons
        if self._bridge_active():
            return self.bridge_report
        return self._build_pad_report()

    def _send_bridge_rumble(self, small: int, large: int):
        if self.bridge_transport is None or self.bridge_addr is None:
            return
        packet = BRIDGE_HDR.pack(BRIDGE_MAGIC, BRIDGE_KEY, 2, 0) + bytes([small, large])
        try:
            self.bridge_transport.sendto(packet, self.bridge_addr)
        except OSError:
            pass

    async def _pad_loop(self):
        while True:
            if self.pad is not None:
                try:
                    self.pad.send_input(self._current_report())
                except OSError as exc:
                    self.pad_message = f"virtual controller write failed: {exc}"
            await asyncio.sleep(0.01)

    def _on_pad_output(self, data: bytes):
        # Steam's rumble: report 0x05, small motor in byte 4, large motor in byte 5
        if len(data) < 6 or data[0] != 0x05:
            return
        small, large = data[4], data[5]
        if (large, small) != (self.rumble_large, self.rumble_small):
            self.say(f"Steam rumble: large {large}, small {small}")
        self.rumble_large, self.rumble_small = large, small
        self._send_bridge_rumble(small, large)

    # ---- rumble and sounds ---------------------------------------------------

    async def _sample_both(self, sample: int):
        for s in (self.left, self.right):
            if s.connected and s.client is not None:
                try:
                    await s.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(sample), response=False)
                except Exception as exc:
                    self.say(f"{s.side} vibration write failed: {describe(exc)}")

    async def _rumble_loop(self):
        was_active = False
        last_play = 0.0
        while True:
            large, small = self.rumble_large, self.rumble_small
            active = max(large, small) > 16
            now = time.monotonic()
            if active and (not was_active or now - last_play > 0.9):
                await self._sample_both(1 if large >= small else 2)
                last_play = now
            elif was_active and not active:
                await self._sample_both(0)
            was_active = active
            await asyncio.sleep(0.05)

    async def test_rumble(self, side: str):
        state = self.left if side == "L" else self.right
        if not state.connected or state.client is None:
            self.say(f"{side} Joy-Con is not connected.")
            return
        try:
            await state.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(self.rumble_sample), response=False)
            self.say(f"Played vibration sample {self.rumble_sample} on {side} Joy-Con.")
            await asyncio.sleep(1.5)
        except Exception as exc:
            self.say(f"{side} rumble failed: {describe(exc)}")
        finally:
            if state.client is not None:
                try:
                    await state.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(0), response=False)
                except Exception:
                    pass

    async def _play_song(self, steps):
        try:
            for sample, wait_ms in steps:
                await self._sample_both(sample)
                await asyncio.sleep(wait_ms / 1000.0)
        finally:
            await self._sample_both(0)
        self.say("Song finished.")

    def play_song(self, index: int):
        name, steps = SONGS[index]
        self.say(f"Playing {name}.")
        asyncio.get_running_loop().create_task(self._play_song(steps))

    # ---- Joy-Con connections -------------------------------------------------

    def start_scan(self):
        if BleakScanner is None:
            self.say("Bluetooth library did not load: " + BLEAK_ERROR.strip().splitlines()[-1])
            return
        if not self.scanning:
            self.scan_task = asyncio.get_running_loop().create_task(self._scan_and_connect(SCAN_SECONDS))

    def _wanted_sides(self):
        return [s for s in ("L", "R") if not (self.left if s == "L" else self.right).connected]

    async def _scan_and_connect(self, duration: float):
        """Scans for real wall-clock time. Each Joy-Con is connected as soon as it is found,
        while it is still advertising."""
        self.scanning = True
        self.seen = {}
        self.scan_stats = {"devices": 0, "nintendo": 0}
        loop = asyncio.get_running_loop()
        counted = set()
        failed = False
        started = []   # sides with a connection in progress during this scan

        def on_advert(device, adv):
            try:
                if device.address not in counted:
                    counted.add(device.address)
                    self.scan_stats["devices"] += 1
                has_mfg = any(cid in adv.manufacturer_data for cid in NINTENDO_COMPANY_IDS)
                has_svc = SERVICE_UUID in [u.lower() for u in adv.service_uuids]
                has_name = bool(device.name) and "joy-con" in device.name.lower()
                if not (has_mfg or has_svc or has_name):
                    return
                if device.address not in self.seen:
                    self.scan_stats["nintendo"] += 1
                self.seen[device.address] = f"{device.name or 'no name'} [{device.address}] rssi {adv.rssi}"
                if device.address in self.tasks:
                    return
                side = detect_side(device, adv)
                if side == "UNKNOWN":
                    open_sides = [s for s in self._wanted_sides() if self.busy[s] is False]
                    if not open_sides:
                        return
                    side = open_sides[0]
                if side not in self._wanted_sides() or self.busy[side]:
                    return
                state = self.left if side == "L" else self.right
                self.busy[side] = True
                started.append(side)
                self.say(f"Found {side} Joy-Con 2 [{device.address}]")
                self.tasks[device.address] = loop.create_task(self._device_worker(device, state))
            except Exception as exc:
                log.error(f"scan callback failed: {exc}")

        try:
            self.say(f"Scanning for {int(duration)} s... hold SYNC on the Joy-Cons.")
            async with BleakScanner(detection_callback=on_advert):
                deadline = loop.time() + duration
                while loop.time() < deadline:
                    if not self._wanted_sides():
                        break  # both Joy-Cons are connected
                    if started:
                        self.message = f"Connecting... {int(deadline - loop.time())} s left in the scan."
                    else:
                        self.message = f"Scanning... {int(deadline - loop.time())} s left, hold SYNC on the Joy-Cons."
                    await asyncio.sleep(0.5)
        except Exception as exc:
            failed = True
            self.say(f"Scan failed: {describe(exc)}")
            log.exception("scan failed")
        finally:
            self.scanning = False
            if not failed and not started and not self.left.connected and not self.right.connected:
                self.say(f"Scan finished: no Joy-Con 2 found. Saw {self.scan_stats['devices']} devices, "
                         f"{self.scan_stats['nintendo']} of them Nintendo-looking. Hold SYNC and try again.")

    async def _bluez_call(self, path: str, interface: str, member: str, signature: str = "",
                          body=None, timeout: float = None):
        bus = self.agent_bus or await MessageBus(bus_type=BusType.SYSTEM).connect()
        message = Message(destination="org.bluez", path=path, interface=interface, member=member,
                          signature=signature, body=body or [])
        reply = await (asyncio.wait_for(bus.call(message), timeout) if timeout else bus.call(message))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member} failed: {reply.error_name}")
        return reply

    async def _bluez_props(self, path: str) -> dict:
        reply = await self._bluez_call(path, "org.freedesktop.DBus.Properties", "GetAll", "s",
                                       ["org.bluez.Device1"])
        return {key: value.value for key, value in reply.body[0].items()}

    async def _ensure_paired(self, device, state: JoyConState):
        """BlueZ does not resolve a Joy-Con's services until the two are paired, so pair first."""
        details = getattr(device, "details", None)
        path = details.get("path") if isinstance(details, dict) else None
        if not path:
            return
        if (await self._bluez_props(path)).get("Paired"):
            return
        state.step = "pairing with the Joy-Con (keep SYNC held)"
        self.say(f"Pairing with {state.side} Joy-Con... keep SYNC held.")
        await self._bluez_cancel_pairing(path)  # clear any pairing an earlier try left pending
        try:
            await self._bluez_call(path, "org.bluez.Device1", "Pair", timeout=PAIR_TIMEOUT)
        except asyncio.TimeoutError:
            await self._bluez_cancel_pairing(path)
            raise RuntimeError(f"pairing timed out after {PAIR_TIMEOUT:.0f} s")
        except RuntimeError as exc:
            if "AlreadyExists" not in str(exc) and "InProgress" not in str(exc):
                raise
            # BlueZ is still pairing this Joy-Con from an earlier try; wait for that to finish
            deadline = time.monotonic() + PAIR_TIMEOUT
            while not (await self._bluez_props(path)).get("Paired"):
                if time.monotonic() > deadline:
                    await self._bluez_cancel_pairing(path)
                    raise RuntimeError("the earlier pairing did not finish") from exc
                await asyncio.sleep(1.0)
        state.step = "trusting the Joy-Con"
        await self._bluez_call(path, "org.freedesktop.DBus.Properties", "Set", "ssv",
                               ["org.bluez.Device1", "Trusted", Variant("b", True)])
        self.say(f"Paired {state.side} Joy-Con.")

    async def _capture_bluez_log(self):
        """Keeps the Bluetooth and kernel log lines about pairing, so a failed try says why."""
        lines = []
        commands = (
            ["journalctl", "-u", "bluetooth", "-n", "200", "--no-pager", "-o", "cat"],
            ["journalctl", "-k", "-n", "300", "--no-pager", "-o", "cat"],
        )
        for command in commands:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(proc.communicate(), 10)
                for line in out.decode("utf-8", "replace").splitlines():
                    if re.search(r"smp|pair|auth|bond|disconn|hci0", line, re.IGNORECASE):
                        lines.append(f"{command[1]}: {line[-150:]}")
            except Exception as exc:
                lines.append(f"{command[1]} log unavailable: {describe(exc)}")
        self.bluez_log = lines[-8:]

    async def _bluez_cancel_pairing(self, path: str):
        """Stops a pairing BlueZ still has pending for this device. Errors are ignored: nothing may be pending."""
        try:
            await self._bluez_call(path, "org.bluez.Device1", "CancelPairing", timeout=10)
        except Exception:
            pass

    async def _bluez_state(self, device) -> str:
        """Reads the device's properties from BlueZ, to see how far a connection got."""
        try:
            details = getattr(device, "details", None)
            path = details.get("path") if isinstance(details, dict) else None
            if not path:
                return "bluez: no device path"
            bus = self.agent_bus or await MessageBus(bus_type=BusType.SYSTEM).connect()
            reply = await bus.call(Message(
                destination="org.bluez", path=path,
                interface="org.freedesktop.DBus.Properties", member="GetAll",
                signature="s", body=["org.bluez.Device1"],
            ))
            if reply.message_type == MessageType.ERROR:
                return f"bluez: {reply.error_name}"
            props = reply.body[0]
            parts = [f"{key}={props[key].value}" for key in
                     ("Connected", "ServicesResolved", "Paired", "Bonded", "Trusted", "RSSI", "AddressType", "Blocked")
                     if key in props]
            return "bluez: " + " ".join(parts)
        except Exception as exc:
            return f"bluez snapshot failed: {describe(exc)}"

    async def _snapshot_later(self, device, state: JoyConState, delay: float):
        await asyncio.sleep(delay)
        state.hint = await self._bluez_state(device)

    async def _device_worker(self, device, state: JoyConState):
        """Connects to one Joy-Con, with a few tries. The step that failed is shown."""
        try:
            for attempt in range(1, CONNECT_TRIES + 1):
                self.say(f"Connecting to {state.side} Joy-Con 2 [{device.address}] "
                         f"(try {attempt} of {CONNECT_TRIES})...")
                state.hint = ""
                hint_task = asyncio.create_task(self._snapshot_later(device, state, 6.0))
                try:
                    await self._run_connection(device, state)
                    self.say(f"{state.side} Joy-Con disconnected (it may have gone to sleep). Press Scan to reconnect.")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    state.reset()
                    extra = f" [{state.hint}]" if state.hint else ""
                    self.say(f"{state.side} try {attempt} failed while '{state.step}': {describe(exc)}{extra}")
                    await self._capture_bluez_log()
                    if attempt < CONNECT_TRIES:
                        await asyncio.sleep(1.0)
                finally:
                    hint_task.cancel()
            self.say(f"{state.side} Joy-Con did not connect after {CONNECT_TRIES} tries. Hold SYNC and press Scan.")
        except asyncio.CancelledError:
            pass
        finally:
            state.reset()
            self.tasks.pop(device.address, None)
            self.busy[state.side] = False

    async def _run_connection(self, device, state: JoyConState):
        """One connection: set up the Joy-Con, then stay connected until it drops."""
        battery_task = None
        await self._ensure_paired(device, state)
        state.step = "opening the Bluetooth connection"
        try:
            async with BleakClient(device, timeout=CONNECT_TIMEOUT) as client:
                state.client = client
                state.connected = True
                state.address = device.address
                state.step = "subscribing to command replies"
                try:
                    await asyncio.wait_for(client.start_notify(COMMAND_NOTIFY_UUID, lambda _s, _d: None),
                                           STEP_TIMEOUT)
                except Exception:
                    pass  # replies are not needed for the controls
                state.step = "subscribing to input reports"
                await asyncio.wait_for(
                    client.start_notify(INPUT_REPORT_UUID, lambda _s, d: self._parse_input(state, d)),
                    STEP_TIMEOUT)
                state.step = "setting the player LED"
                await asyncio.wait_for(client.write_gatt_char(COMMAND_WRITE_UUID, CMD_SET_LED_P1, response=False),
                                       STEP_TIMEOUT)
                await asyncio.sleep(0.1)
                state.step = "enabling motion features"
                await asyncio.wait_for(client.write_gatt_char(COMMAND_WRITE_UUID, CMD_SET_FEATURES_IMU,
                                                              response=False), STEP_TIMEOUT)
                await asyncio.sleep(0.45)
                await asyncio.wait_for(client.write_gatt_char(COMMAND_WRITE_UUID, CMD_ENABLE_FEATURES_IMU,
                                                              response=False), STEP_TIMEOUT)
                await asyncio.sleep(0.1)
                if state.side in SETUP_SIDES:
                    state.step = "sending the setup sequence"
                    await self._send_setup(state)
                battery_task = asyncio.create_task(self._battery_sampler(client, state))
                state.step = "connected"
                self.say(f"Connected {state.side} Joy-Con 2.")
                while client.is_connected:
                    await asyncio.sleep(0.5)
        finally:
            if battery_task is not None:
                battery_task.cancel()

    async def _send_setup(self, state: JoyConState):
        try:
            for cmd in SETUP_COMMANDS:
                await asyncio.wait_for(
                    state.client.write_gatt_char(RUMBLE_CHAR_UUID[state.side], bytes(17) + cmd, response=False),
                    STEP_TIMEOUT)
                await asyncio.sleep(0.08)
            self.say(f"Setup sent to {state.side} Joy-Con.")
        except Exception as exc:
            self.say(f"{state.side} setup failed: {describe(exc)}")

    async def _battery_sampler(self, client, state: JoyConState):
        channel = BATTERY_CHANNEL[state.side]
        while client.is_connected:
            samples = []
            try:
                await client.start_notify(channel, lambda _s, d: samples.append(bytes(d)))
                await asyncio.sleep(BATTERY_SAMPLE_SECONDS)
                await client.stop_notify(channel)
            except Exception:
                pass
            if samples and len(samples[-1]) > 1:
                level = (samples[-1][1] >> 2) & 0x0F
                state.battery_pct = min(100, round(level / 8 * 100))
                state.charging = bool(samples[-1][1] & 0x02)
            await asyncio.sleep(BATTERY_EVERY_SECONDS)

    def _parse_input(self, state: JoyConState, data):
        data = bytes(data)
        if len(data) < 16:
            return
        state.buttons = int.from_bytes(data[4:8], "little")
        lx, ly = decode_stick(data[10], data[11], data[12])
        rx, ry = decode_stick(data[13], data[14], data[15])
        if state.side == "L" and (lx or ly):
            state.stick_x, state.stick_y = lx, ly
        elif state.side == "R" and (rx or ry):
            state.stick_x, state.stick_y = rx, ry
        if len(data) >= 60:
            ax, ay, az = struct.unpack_from("<hhh", data, 48)
            gx, gy, gz = struct.unpack_from("<hhh", data, 54)
            state.accel_x = ax * (8.0 / 32767.0)
            state.accel_y = ay * (8.0 / 32767.0)
            state.accel_z = az * (8.0 / 32767.0)
            dps_scale = 2000.0 / 32768.0
            state.gyro_x = gx * dps_scale - state.gyro_bias[0]
            state.gyro_y = gy * dps_scale - state.gyro_bias[1]
            state.gyro_z = gz * dps_scale - state.gyro_bias[2]

    def disconnect_all(self):
        for task in list(self.tasks.values()):
            task.cancel()
        self.say("Disconnected all Joy-Cons.")

    def calibrate(self):
        for s in (self.left, self.right):
            if s.connected:
                s.center_x, s.center_y = s.stick_x, s.stick_y
                s.gyro_bias = [s.gyro_x + s.gyro_bias[0], s.gyro_y + s.gyro_bias[1], s.gyro_z + s.gyro_bias[2]]
        self.say("Zeroed sticks and gyro.")

    def status(self) -> dict:
        def side(s: JoyConState) -> dict:
            return {"connected": s.connected, "battery": s.battery_pct, "charging": s.charging, "name": s.name}

        diag = [
            f"Python {sys.version.split()[0]}",
            f"root: {'yes' if os.geteuid() == 0 else 'no'}",
            f"/dev/uhid: {'present' if os.path.exists('/dev/uhid') else 'missing'}",
            f"bleak: {'loaded' if BleakScanner is not None else 'failed'}",
            f"pairing helper: {'loaded' if PairingAgent is not None else 'failed'}",
            f"pairing agent: {self.agent_state}",
            "BlueZ calls to the agent: " + ("; ".join(AGENT_CALLS) if AGENT_CALLS else "none yet"),
        ]
        diag += [f"log {line}" for line in self.bluez_log]
        if self._bridge_active():
            diag.append(f"Deck bridge: receiving from the PC ({self.bridge_count} packets)")
        else:
            diag.append(f"Deck bridge: waiting for the PC on UDP {BRIDGE_PORT} ({self.bridge_count} packets so far)")
        try:
            adapters = sorted(os.listdir("/sys/class/bluetooth"))
            diag.append("Bluetooth adapters: " + (", ".join(adapters) or "none"))
        except Exception as exc:
            diag.append(f"Bluetooth adapters: unknown ({exc})")
        return {
            "pad_ok": self.pad is not None,
            "pad_message": self.pad_message,
            "left": side(self.left),
            "right": side(self.right),
            "scanning": self.scanning,
            "message": self.message,
            "songs": [name for name, _ in SONGS],
            "diag": diag,
            "bleak_error": BLEAK_ERROR,
            "seen": list(self.seen.values()),
            "scan_stats": dict(self.scan_stats),
            "recent": self.recent,
        }


class Plugin:
    def __init__(self):
        self.studio = JoyConStudio()

    async def _main(self):
        try:
            await self.studio.start()
            log.info("Joy-Con 2 Studio started")
        except Exception as exc:
            self.studio.say(f"Plugin failed to start: {describe(exc)}")
            log.exception("start failed")

    async def _unload(self):
        await self.studio.stop()

    async def _uninstall(self):
        pass

    # Called from the Decky frontend
    async def status(self):
        try:
            return self.studio.status()
        except Exception as exc:
            return {"pad_ok": False, "pad_message": f"status error: {exc}", "left": {"connected": False},
                    "right": {"connected": False}, "scanning": False, "message": f"status error: {exc}",
                    "songs": [name for name, _ in SONGS], "diag": [traceback.format_exc()[-600:]],
                    "bleak_error": BLEAK_ERROR, "seen": [], "scan_stats": {}, "recent": []}

    async def scan(self):
        self.studio.start_scan()
        return True

    async def disconnect_all(self):
        self.studio.disconnect_all()
        return True

    async def calibrate(self):
        self.studio.calibrate()
        return True

    async def test_rumble(self, side: str):
        asyncio.get_running_loop().create_task(self.studio.test_rumble(side))
        return True

    async def play_song(self, index: int):
        self.studio.play_song(int(index))
        return True

    async def stop_vibration(self):
        await self.studio._sample_both(0)
        self.studio.say("Stopped vibration.")
        return True
