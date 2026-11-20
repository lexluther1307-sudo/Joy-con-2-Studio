import asyncio
import collections
import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import colorchooser
from tkinter import font as tkfont
from tkinter import messagebox
from bleak import BleakClient, BleakScanner

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# The encrypted Deck link and the uhid gamepad are shared with the Deck plugin (standard library only)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "decky", "joycon2studio"))
from joycon2_link import PcLink  # noqa: E402

vg = None
HAS_VGAMEPAD = False
if IS_WINDOWS:
    try:
        import vgamepad as vg
        HAS_VGAMEPAD = True
    except Exception:
        pass
if IS_LINUX:
    from linux_mouse import UinputMouse  # noqa: E402
    from uhid_pad import UhidGamepad  # noqa: E402

# Nintendo Switch 2 BLE UUIDs
SERVICE_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd0"
INPUT_REPORT_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
COMMAND_WRITE_UUID = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"
COMMAND_NOTIFY_UUID = "c765a961-d9d8-4d36-a20a-5315b111836a"
# joycon2cpp's setup sequence, sent to a Joy-Con's rumble characteristic on connect.
# Each packet is 17 zero bytes followed by the command.
RUMBLE_CHAR_UUID = {
    "L": "ce49a830-dced-48ae-931e-c8cf88aadbea",
    "R": "65a724b3-f1e7-4a61-8078-a342376b27ff",
}
# Sides that get the setup sequence (right only until it is tested on both)
SETUP_SIDES = ("R",)
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
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

NINTENDO_COMPANY_IDS = {0x0553, 0x057E}

# Switch 2 handshake, IMU enable (feature mask 0x37), and SPI flash color read (0x13019)
CMD_SET_LED_P1 = bytes.fromhex("09910107000800000100000000000000")
CMD_SET_FEATURES_IMU = bytes.fromhex("0c9101020004000037000000")
CMD_ENABLE_FEATURES_IMU = bytes.fromhex("0c9101040004000037000000")
CMD_READ_COLORS = bytes.fromhex("02910101000800000c00000019300100")
COLOR_ADDR = 0x13019  # SPI address of body, button and rail colors

# Battery: each Joy-Con has a second notify channel whose byte 1 holds the power info:
# bit 0 external power, bit 1 charging, bits 2-5 level (0-8, where 8 is full).
BATTERY_CHANNEL = {
    "R": "d5a9e01e-2ffc-4cca-b20c-8b67142bf442",
    "L": "cc1bbbb5-7354-4d32-a716-a81cb241a32a",
}
BATTERY_SAMPLE_SECONDS = 3.0   # the motion stream pauses while this channel is read
BATTERY_EVERY_SECONDS = 300.0  # read again every 5 minutes

# Network bridge: this PC keeps the Joy-Cons and sends the controller state to a Deck over UDP.
# The Deck presents it to Steam as a DualShock 4 and sends Steam's rumble back here.
BRIDGE_PORT = 26761
DPAD_NIBBLE = {
    (True, False, False, False): 0, (True, False, False, True): 1, (False, False, False, True): 2,
    (False, True, False, True): 3, (False, True, False, False): 4, (False, True, True, False): 5,
    (False, False, True, False): 6, (True, False, True, False): 7,
}


def stick_byte(v: int) -> int:
    # v is -32767..32767 with 0 centered; DualShock 4 sticks are 0..255 with 128 centered
    return max(0, min(255, 128 + round(v / 32767 * 127)))


def bridge_dpad(up, down, left, right) -> int:
    return DPAD_NIBBLE.get((bool(up), bool(down), bool(left), bool(right)), 8)


# Virtual controller shown to Windows and Steam
OUTPUT_LABELS = {"ds4": "DualShock 4 (Steam)", "x360": "Xbox 360 (games)"}

# Built-in vibration samples (command 0x0A, subcommand 0x02). Sample 0 is silence and stops playback.
VIBRATION_SAMPLES = [
    ("Stop (silence)", 0),
    ("Low-frequency buzz", 1),
    ("High-frequency buzz", 2),
    ("Soft click-click", 3),
    ("Beep-beep, high to higher", 4),
    ("Stronger click-click", 5),
    ("Short high beep", 6),
    ("Short higher beep", 7),
]

# Sample songs: (vibration sample id, wait in ms before the next note). Edit these to make your own.
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


def vibration_cmd(sample_id: int) -> bytes:
    return bytes([0x0A, 0x91, 0x01, 0x02, 0x00, 0x04, 0x00, 0x00, sample_id, 0x00, 0x00, 0x00])


# Button bitmasks in 32-bit field (bytes 4..8 of 63-byte BLE report)
BTN_Y = 0x00000001
BTN_X = 0x00000002
BTN_B = 0x00000004
BTN_A = 0x00000008
BTN_SR_R = 0x00000010
BTN_SL_R = 0x00000020
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
BTN_SR_L = 0x00100000
BTN_SL_L = 0x00200000
BTN_L = 0x00400000
BTN_ZL = 0x00800000

# The buttons that click for the mouse: (left click, right click) for each Joy-Con
CLICK_BUTTONS = {"R": (BTN_ZR, BTN_R), "L": (BTN_ZL, BTN_L)}
CLICK_MASK = {"R": BTN_ZR | BTN_R, "L": BTN_ZL | BTN_L}  # both click buttons of each Joy-Con
# Every button that is a controller press. The click buttons count only while the Joy-Con is a controller.
CONTROLLER_MASK = (BTN_A | BTN_B | BTN_X | BTN_Y | BTN_UP | BTN_DOWN | BTN_LEFT | BTN_RIGHT |
                   BTN_PLUS | BTN_MINUS | BTN_HOME | BTN_CAPTURE | BTN_L | BTN_R | BTN_ZL | BTN_ZR |
                   BTN_L3 | BTN_R3 | BTN_SL_L | BTN_SR_L | BTN_SL_R | BTN_SR_R)
STICK_PUSHED = 300  # raw distance of a stick from its centre that counts as using the stick
MOUSE_SLIDE = 3  # sensor counts in one report that count as a Joy-Con sliding across a surface

COLOR_THEMES = [
    ("Switch 2 Default", "#2A2D34", "#181A1F", "#00B8E6", "#FF4E42"),
    ("Neon Blue / Red", "#0AB9E6", "#1E2229", "#0891B2", "#FF3C28"),
    ("OLED White", "#E6E8EC", "#23262E", "#00B8E6", "#FF4E42"),
    ("Pastel Purple / Green", "#B594F6", "#252230", "#9370DB", "#7CE0A3"),
]


def decode_stick(b0: int, b1: int, b2: int) -> tuple[int, int]:
    x = b0 | ((b1 & 0x0F) << 8)
    y = (b1 >> 4) | (b2 << 4)
    return x, y


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def is_valid_rgb(r: int, g: int, b: int) -> bool:
    # Filter out unprogrammed flash (0xFF/0x00)
    if (r, g, b) in ((0xFF, 0xFF, 0xFF), (0x00, 0x00, 0x00)):
        return False
    return True


def apply_color_reply(state, data: bytes) -> bool:
    # SPI read reply: 8-byte header, 4-byte length, 4-byte address, then the data.
    if len(data) < 25 or data[0] != 0x02:
        return False
    if int.from_bytes(data[12:16], "little") != COLOR_ADDR:
        return False
    body = tuple(data[16:19])     # shell color
    buttons = tuple(data[19:22])  # button color
    rail = tuple(data[22:25])     # this Joy-Con's rail color
    if is_valid_rgb(*body):
        state.body_color = rgb_to_hex(*body)
    if is_valid_rgb(*buttons):
        state.btn_color = rgb_to_hex(*buttons)
    if is_valid_rgb(*rail):
        state.rail_color = rgb_to_hex(*rail)
    return True


def _settings_path() -> str:
    if IS_LINUX:
        # A packaged install lives in /usr, so the settings go in the user's config folder
        folder = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "joycon2studio")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "settings.json")
    # Next to the exe when frozen, otherwise next to this script
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "settings.json")


def load_settings() -> dict:
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: dict):
    try:
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def clamp16(v: int) -> int:
    return max(-32768, min(32767, v))


def dpad_direction(up, down, left, right):
    D = vg.DS4_DPAD_DIRECTIONS
    table = {
        (True, False, False, False): D.DS4_BUTTON_DPAD_NORTH,
        (True, False, False, True): D.DS4_BUTTON_DPAD_NORTHEAST,
        (False, False, False, True): D.DS4_BUTTON_DPAD_EAST,
        (False, True, False, True): D.DS4_BUTTON_DPAD_SOUTHEAST,
        (False, True, False, False): D.DS4_BUTTON_DPAD_SOUTH,
        (False, True, True, False): D.DS4_BUTTON_DPAD_SOUTHWEST,
        (False, False, True, False): D.DS4_BUTTON_DPAD_WEST,
        (True, False, True, False): D.DS4_BUTTON_DPAD_NORTHWEST,
    }
    return table.get((bool(up), bool(down), bool(left), bool(right)), D.DS4_BUTTON_DPAD_NONE)


def _wrap16(d: int) -> int:
    """A difference of two 16-bit counters, wrapped so a rollover is not read as a jump."""
    return ((d + 32768) % 65536) - 32768


class JoyConState:
    def __init__(self, side: str):
        self.side = side  # "L" or "R"
        self.connected = False
        self.address = ""
        self.name = f"Joy-Con 2 ({side})"
        self.buttons = 0
        self.stick_x = 2048
        self.stick_y = 2048
        self.center_x = 2048
        self.center_y = 2048

        # IMU (Gyro in deg/s, Accel in G)
        self.gyro_x = 0.0
        self.gyro_y = 0.0
        self.gyro_z = 0.0
        self.gyro_bias = [0.0, 0.0, 0.0]
        self.accel_x = 0.0
        self.accel_y = 0.0
        self.accel_z = 0.0

        self.client = None
        self.mouse_raw = (0, 0)
        self.mouse_prev = None
        self.mouse_acc = [0.0, 0.0]
        self.mouse_left = False
        self.mouse_right = False
        self.last_active = 0.0  # when this Joy-Con was last used, to decide which one is in use
        self.mode = "controller"  # "controller" or "mouse": what this Joy-Con is doing (automatic mode)
        self.controller_at = 0.0  # when its sticks or buttons were last used as a controller
        self.mouse_at = 0.0  # when it last slid across a surface
        self.was_pushed = False  # whether its stick was pushed away from the centre in the last report
        self.stick_prev = (2048, 2048)

        # Battery: level in bars (0-9, 9 = full), from byte 46
        self.battery_pct = 0
        self.battery_bars = 0
        self.battery_mv = 0
        self.charging = False

        # Colors (Authentic Switch 2 Charcoal + Light Blue / Light Red accents)
        self.theme_idx = 0
        self.body_color = "#2A2D34"
        self.btn_color = "#181A1F"
        self.rail_color = "#00B8E6" if side == "L" else "#FF4E42"

    def apply_theme(self, idx: int):
        self.theme_idx = idx % len(COLOR_THEMES)
        _, body, btn, rail_l, rail_r = COLOR_THEMES[self.theme_idx]
        self.body_color = body
        self.btn_color = btn
        self.rail_color = rail_l if self.side == "L" else rail_r

    def reset(self):
        self.connected = False
        self.address = ""
        self.buttons = 0
        self.stick_x = self.center_x
        self.stick_y = self.center_y
        self.gyro_x = self.gyro_y = self.gyro_z = 0.0
        self.accel_x = self.accel_y = self.accel_z = 0.0
        self.battery_pct = 0
        self.battery_bars = 0
        self.battery_mv = 0
        self.charging = False
        self.client = None
        self.mouse_prev = None
        self.mouse_raw = (0, 0)
        self.mouse_left = False
        self.mouse_right = False

    def calibrate(self):
        if self.connected:
            self.center_x = self.stick_x
            self.center_y = self.stick_y
            self.gyro_bias = [
                self.gyro_x + self.gyro_bias[0],
                self.gyro_y + self.gyro_bias[1],
                self.gyro_z + self.gyro_bias[2],
            ]


class JoyCon2Manager:
    def __init__(self, log_callback):
        self.log = log_callback
        self.left = JoyConState("L")
        self.right = JoyConState("R")
        self.sideways_single = False
        self.gyro_aim_enabled = False
        self.gyro_sensitivity = 14.0
        self.mouse_enabled = False
        self.mouse_speed = 0.3
        self.active_choice = "auto"  # "auto", "L" or "R": which Joy-Con the mouse and sideways mode use
        self.active_side = None  # the Joy-Con in use right now
        self.auto_mode = load_settings().get("auto_mode", True)  # switch between mouse and controller by itself
        self.rumble_sample = 1
        self.left.rumble_request = lambda: self.rumble_test(self.left)
        self.right.rumble_request = lambda: self.rumble_test(self.right)
        self.output = "ds4" if IS_LINUX else load_settings().get("output", "ds4")  # Linux makes only a DualShock 4
        self.ds4 = None
        self.uhid_pad = None  # Linux: this PC's DualShock 4, made through /dev/uhid by _local_pad_loop
        self.want_local_pad = False  # Linux: True while this PC, not the Deck, should show the controller
        self.local_pad_message = ""
        self.uinput_mouse = None  # Linux: this PC's mouse, made through /dev/uinput
        self._ds4_failed = False
        self.rumble_large = 0
        self.rumble_small = 0
        self.link = PcLink(BRIDGE_PORT, self._bridge_report, self._on_link_state, self._on_link_rumble, self.log)

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.scanning = False
        self._tasks: dict[str, asyncio.Task] = {}

        self.gamepad = None
        if HAS_VGAMEPAD and self.output == "x360":
            try:
                self.gamepad = vg.VX360Gamepad()
            except Exception as e:
                self.log(f"ViGEmBus driver warning: {e}")

        self.thread.start()
        asyncio.run_coroutine_threadsafe(self._rumble_loop(), self.loop)
        if IS_LINUX:
            self._open_uinput_mouse()
            asyncio.run_coroutine_threadsafe(self._local_pad_loop(), self.loop)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start_scan(self, duration: float = 12.0):
        if not self.scanning:
            asyncio.run_coroutine_threadsafe(self._scan_and_connect(duration), self.loop)

    def disconnect_all(self):
        asyncio.run_coroutine_threadsafe(self._disconnect_all_async(), self.loop)

    async def _disconnect_all_async(self):
        self.scanning = False
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self.left.reset()
        self.right.reset()
        self._reset_virtual_gamepad()
        self.log("Disconnected all Joy-Cons.")

    def _detect_side(self, device, adv) -> str:
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

    async def _scan_and_connect(self, duration: float):
        self.scanning = True
        self.log("Scanning... Hold SYNC on 1 or 2 Joy-Con 2 controllers.")
        elapsed = 0.0
        step = 2.0

        try:
            while self.scanning and elapsed < duration:
                if self.left.connected and self.right.connected:
                    break

                devices = await BleakScanner.discover(timeout=step, return_adv=True)
                for device, adv in devices.values():
                    if device.address in self._tasks:
                        continue

                    has_mfg = any(cid in adv.manufacturer_data for cid in NINTENDO_COMPANY_IDS)
                    has_svc = SERVICE_UUID in [u.lower() for u in adv.service_uuids]
                    has_name = device.name and "joy-con" in device.name.lower()

                    if has_mfg or has_svc or has_name:
                        side = self._detect_side(device, adv)
                        if side == "UNKNOWN":
                            side = "L" if not self.left.connected else "R"

                        if side == "L" and not self.left.connected:
                            self.log(f"Found Left Joy-Con 2 [{device.address}]")
                            self._tasks[device.address] = asyncio.create_task(
                                self._device_worker(device, self.left)
                            )
                        elif side == "R" and not self.right.connected:
                            self.log(f"Found Right Joy-Con 2 [{device.address}]")
                            self._tasks[device.address] = asyncio.create_task(
                                self._device_worker(device, self.right)
                            )

                elapsed += step
        finally:
            self.scanning = False
            count = int(self.left.connected) + int(self.right.connected)
            if count == 0:
                self.log("Scan finished: No Joy-Con 2 found. Hold SYNC and try again.")
            else:
                self.log(f"Ready: {count} Joy-Con(s) active.")

    async def _device_worker(self, device, state: JoyConState):
        battery_task = None
        try:
            async with BleakClient(device) as client:
                state.client = client
                state.connected = True
                state.address = device.address
                if device.name:
                    state.name = device.name

                def on_cmd_reply(_sender, data: bytearray):
                    apply_color_reply(state, bytes(data))

                def on_input(_sender, data: bytearray):
                    self._parse_input_report(state, data)

                # 1. Subscribe to command responses & input notifications first
                try:
                    await client.start_notify(COMMAND_NOTIFY_UUID, on_cmd_reply)
                except Exception:
                    pass

                await client.start_notify(INPUT_REPORT_UUID, on_input)

                # 2. Set Player 1 LED
                await client.write_gatt_char(COMMAND_WRITE_UUID, CMD_SET_LED_P1, response=False)
                await asyncio.sleep(0.1)

                # 3. Set and enable the feature mask (IMU, mouse, battery current)
                await client.write_gatt_char(
                    COMMAND_WRITE_UUID, CMD_SET_FEATURES_IMU, response=False
                )
                await asyncio.sleep(0.45)
                await client.write_gatt_char(
                    COMMAND_WRITE_UUID, CMD_ENABLE_FEATURES_IMU, response=False
                )
                await asyncio.sleep(0.1)

                # 3b. joycon2cpp setup sequence (only for the sides in SETUP_SIDES)
                if state.side in SETUP_SIDES:
                    await self._send_setup(state)

                # 4. Request the SPI flash RGB colors at 0x13019
                await client.write_gatt_char(COMMAND_WRITE_UUID, CMD_READ_COLORS, response=False)

                battery_task = asyncio.create_task(self._battery_sampler(client, state))
                self.log(f"Connected {state.side} Joy-Con 2!")
                while client.is_connected:
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log(f"{state.side} Joy-Con connection closed: {e}")
        finally:
            if battery_task is not None:
                battery_task.cancel()
            state.reset()
            self._tasks.pop(device.address, None)
            self._sync_virtual_gamepad()

    async def _send_setup(self, state: JoyConState):
        try:
            for cmd in SETUP_COMMANDS:
                await state.client.write_gatt_char(RUMBLE_CHAR_UUID[state.side], bytes(17) + cmd, response=False)
                await asyncio.sleep(0.08)
            self.log(f"Setup sent to {state.side} Joy-Con.")
        except Exception as e:
            self.log(f"{state.side} setup failed: {e}")

    def _parse_input_report(self, state: JoyConState, data: bytearray):
        if len(data) < 16:
            return
        now = time.monotonic()

        # Bytes 4..8: 32-bit button bitfield. Any change means this Joy-Con is being used.
        previous = state.buttons
        state.buttons = int.from_bytes(data[4:8], byteorder="little")
        moved = state.buttons != previous

        # Bytes 10..15: 12-bit analog sticks
        lx, ly = decode_stick(data[10], data[11], data[12])
        rx, ry = decode_stick(data[13], data[14], data[15])
        if state.side == "L" and (lx != 0 or ly != 0):
            stick = (lx, ly)
        elif state.side == "R" and (rx != 0 or ry != 0):
            stick = (rx, ry)
        else:
            stick = None
        if stick is not None:
            if abs(stick[0] - state.stick_prev[0]) > 120 or abs(stick[1] - state.stick_prev[1]) > 120:
                moved = True
            state.stick_prev = stick
            state.stick_x, state.stick_y = stick
        pushed = (abs(state.stick_x - state.center_x) > STICK_PUSHED
                  or abs(state.stick_y - state.center_y) > STICK_PUSHED)
        new_push = pushed and not state.was_pushed
        state.was_pushed = pushed

        # Mouse sensor: bytes 16..19, little-endian int16 X and Y (needs feature bit 0x10)
        sliding = False
        if len(data) >= 20:
            mx, my = struct.unpack_from("<hh", data, 16)
            step = abs(_wrap16(mx - state.mouse_raw[0])) + abs(_wrap16(my - state.mouse_raw[1]))
            if step >= 2:
                moved = True
            state.mouse_raw = (mx, my)
            sliding = step >= MOUSE_SLIDE

        # Accel XYZ at bytes 48..53 (+-8 g) and gyro XYZ at bytes 54..59 (+-2000 dps), little-endian int16
        if len(data) >= 60:
            ax, ay, az = struct.unpack_from("<hhh", data, 48)
            gx, gy, gz = struct.unpack_from("<hhh", data, 54)

            state.accel_x = ax * (8.0 / 32767.0)
            state.accel_y = ay * (8.0 / 32767.0)
            state.accel_z = az * (8.0 / 32767.0)

            # +-2000 deg/s full-scale range (2000 / 32768 = 0.061035 dps/LSB)
            dps_scale = 2000.0 / 32768.0
            state.gyro_x = (gx * dps_scale) - state.gyro_bias[0]
            state.gyro_y = (gy * dps_scale) - state.gyro_bias[1]
            state.gyro_z = (gz * dps_scale) - state.gyro_bias[2]
            turn = max(abs(state.gyro_x), abs(state.gyro_y), abs(state.gyro_z))
            if turn > 25.0:
                moved = True
            if turn > 60.0:
                sliding = False  # a Joy-Con turned in the hand is aiming, not sliding across a surface

        if moved:
            state.last_active = now
        presses = state.buttons & ~previous & CONTROLLER_MASK  # buttons pressed just now
        if state.mode == "mouse":
            presses &= ~CLICK_MASK[state.side]  # a click on a mouse is not a new controller press
        self._update_mode(state, now, pushed, sliding)
        self._update_active_side(now, control_side=state.side if (presses or new_push) else None)
        if len(data) >= 20:
            self._move_cursor(state)
            self._mouse_clicks(state)
        self._sync_virtual_gamepad()

    ACTIVE_HOLD = 0.6  # seconds: a Joy-Con used this recently keeps control, so two in use do not take turns
    MODE_HOLD = 0.8  # seconds without controller input before a Joy-Con that is sliding can become a mouse

    def _state_for(self, side):
        return self.left if side == "L" else self.right

    def _update_active_side(self, now: float, force: bool = False, control_side=None):
        """Works out which connected Joy-Con is in use. Auto follows whichever was used most recently, and a
        new press on a Joy-Con takes over at once."""
        connected = [s for s in (self.right, self.left) if s.connected]
        if not connected:
            want = None
        elif self.active_choice in ("L", "R") and self._state_for(self.active_choice).connected:
            want = self.active_choice
        elif len(connected) == 1:
            want = connected[0].side
        elif control_side is not None and self._state_for(control_side).connected:
            want = control_side
        else:
            current = self._state_for(self.active_side) if self.active_side else None
            if current is not None and current.connected and now - current.last_active < self.ACTIVE_HOLD:
                want = current.side
            else:
                want = max(connected, key=lambda s: s.last_active).side
        if force or want != self.active_side:
            self._set_active_side(want)

    def set_active_choice(self, choice: str):
        """From the window: "auto", "L" or "R"."""
        self.active_choice = choice
        self._update_active_side(time.monotonic(), force=True)

    def _set_active_side(self, side):
        old = self.active_side
        if old is not None and old != side:
            self._release_mouse_buttons(self._state_for(old))  # no click stays held on the Joy-Con that stopped
        self.active_side = side
        if side is not None:
            self._state_for(side).mouse_acc = [0.0, 0.0]
            use = self._describe_use()
            if use and old != side:
                self.log(f"{'Left' if side == 'L' else 'Right'} Joy-Con is in use for {use}.")
        self._sync_virtual_gamepad()

    def _describe_use(self) -> str:
        parts = []
        if self.mouse_enabled:
            parts.append("the mouse")
        if self.sideways_single:
            parts.append("a sideways controller")
        return " and ".join(parts)

    def set_auto_mode(self, value: bool):
        """Turns the automatic switch between mouse and controller on or off."""
        self.release_mouse()
        self.auto_mode = bool(value)
        for state in (self.left, self.right):
            state.mode = "controller"  # the automatic switch starts again from controller
        self._sync_virtual_gamepad()

    def _update_mode(self, state: JoyConState, now: float, pushed: bool, sliding: bool):
        """Automatic mode. A Joy-Con is a controller while its sticks or buttons are used, and a mouse while it
        slides across a surface with nothing pressed. While it is a mouse, its click buttons are not controller
        presses, so clicking does not switch it back."""
        pressed = state.buttons & CONTROLLER_MASK
        if state.mode == "mouse":
            pressed &= ~CLICK_MASK[state.side]
        if pushed or pressed:
            state.controller_at = now
        if sliding:
            state.mouse_at = now
        if not self.auto_mode:
            return
        if now - state.controller_at < self.MODE_HOLD:
            want = "controller"
        elif now - state.mouse_at < self.MODE_HOLD:
            want = "mouse"
        else:
            want = state.mode
        if want != state.mode:
            state.mode = want
            if want == "controller":
                self._release_mouse_buttons(state)  # no click stays held once the Joy-Con is a controller again
            self.log(f"{'Left' if state.side == 'L' else 'Right'} Joy-Con is now a {want}.")

    def _is_mouse(self, state: JoyConState) -> bool:
        """True when this Joy-Con is the mouse right now. Never while the Deck has the controller."""
        if not self.mouse_enabled or self.deck_connected():
            return False
        if self.auto_mode:
            return state.mode == "mouse"
        return state.side == self.active_side

    def _contributes(self, state: JoyConState) -> bool:
        """True when this Joy-Con sends its buttons and sticks to the controller, which is when it is not the mouse."""
        return not self._is_mouse(state)

    def release_mouse(self):
        """Lets go of any mouse button this app holds, for example when mouse mode is switched off."""
        for state in (self.left, self.right):
            self._release_mouse_buttons(state)

    def _release_mouse_buttons(self, state: JoyConState):
        if state.mouse_left:
            self._mouse_button("left", False)
            state.mouse_left = False
        if state.mouse_right:
            self._mouse_button("right", False)
            state.mouse_right = False

    def _move_cursor(self, state: JoyConState):
        mx, my = state.mouse_raw
        prev = state.mouse_prev
        state.mouse_prev = (mx, my)  # kept current for both Joy-Cons, so switching never makes the cursor jump
        if prev is None or state.side != self.active_side or not self._is_mouse(state):
            return
        dx = _wrap16(mx - prev[0])  # a counter rollover is not a jump
        dy = _wrap16(my - prev[1])
        state.mouse_acc[0] += dx * self.mouse_speed
        state.mouse_acc[1] += dy * self.mouse_speed
        ix, iy = int(state.mouse_acc[0]), int(state.mouse_acc[1])
        if ix or iy:
            state.mouse_acc[0] -= ix
            state.mouse_acc[1] -= iy
            self._mouse_move(ix, iy)

    def _mouse_clicks(self, state: JoyConState):
        if state.side != self.active_side or not self._is_mouse(state):
            return
        left_mask, right_mask = CLICK_BUTTONS[state.side]
        left = bool(state.buttons & left_mask)
        right = bool(state.buttons & right_mask)
        if left != state.mouse_left:
            self._mouse_button("left", left)
            state.mouse_left = left
        if right != state.mouse_right:
            self._mouse_button("right", right)
            state.mouse_right = right

    def rumble_test(self, state: JoyConState):
        if state.client is None or not state.connected:
            self.log(f"{state.side} Joy-Con is not connected.")
            return
        asyncio.run_coroutine_threadsafe(self._rumble_test_async(state), self.loop)

    async def _rumble_test_async(self, state: JoyConState):
        sample = self.rumble_sample
        try:
            await state.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(sample), response=False)
            self.log(f"Played vibration sample {sample} on {state.side} Joy-Con.")
            await asyncio.sleep(1.5)
        except Exception as e:
            self.log(f"{state.side} rumble failed: {e}")
        finally:
            try:
                await state.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(0), response=False)
            except Exception:
                pass

    def play_song(self, name: str):
        steps = dict(SONGS)[name]
        asyncio.run_coroutine_threadsafe(self._play_song_async(steps), self.loop)

    async def _play_song_async(self, steps):
        clients = [s for s in (self.left, self.right) if s.connected and s.client is not None]
        if not clients:
            self.log("No connected Joy-Con to play the song.")
            return
        try:
            for sample, wait_ms in steps:
                await asyncio.gather(
                    *(s.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(sample), response=False) for s in clients)
                )
                await asyncio.sleep(wait_ms / 1000.0)
        except Exception as e:
            self.log(f"Song stopped: {e}")
        finally:
            for s in clients:
                try:
                    await s.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(0), response=False)
                except Exception:
                    pass
        self.log("Song finished.")

    def stop_vibration(self):
        asyncio.run_coroutine_threadsafe(self._stop_vibration_async(), self.loop)

    async def _stop_vibration_async(self):
        for s in (self.left, self.right):
            if s.connected and s.client is not None:
                try:
                    await s.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(0), response=False)
                except Exception:
                    pass

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
                state.battery_bars = level
                state.battery_pct = min(100, round(level / 8 * 100))
                state.charging = bool(samples[-1][1] & 0x02)
            await asyncio.sleep(BATTERY_EVERY_SECONDS)

    def set_output(self, mode: str):
        if mode == self.output:
            return
        self.output = mode
        if mode == "x360":
            self.ds4 = None  # releasing the DualShock 4 object unplugs it
            if HAS_VGAMEPAD and self.gamepad is None and not self.deck_connected():
                try:
                    self.gamepad = vg.VX360Gamepad()
                except Exception as e:
                    self.log(f"ViGEmBus driver warning: {e}")
        else:
            if self.gamepad is not None:
                self.gamepad.reset()
                self.gamepad.update()
                self.gamepad = None  # releasing the Xbox 360 object unplugs it
            self._ds4_failed = False
        self._sync_virtual_gamepad()

    def _ensure_ds4(self):
        if self.ds4 is None and HAS_VGAMEPAD and not self._ds4_failed:
            try:
                self.ds4 = vg.VDS4Gamepad()
                self.ds4.register_notification(self._on_ds4_rumble)
            except Exception as e:
                self._ds4_failed = True
                self.log(f"DualShock 4 output unavailable: {e}")
        return self.ds4

    def _on_ds4_rumble(self, client, target, large_motor, small_motor, lightbar_color, user_data):
        # Called by the virtual pad when Steam sends rumble
        if (large_motor, small_motor) != (self.rumble_large, self.rumble_small):
            self.log(f"Steam rumble: large {large_motor}, small {small_motor}")
        self.rumble_large = large_motor
        self.rumble_small = small_motor

    async def _rumble_loop(self):
        # Turns Steam's rumble into the Joy-Cons' built-in vibration samples
        was_active = False
        last_play = 0.0
        while True:
            large, small = self.rumble_large, self.rumble_small
            active = max(large, small) > 16
            joycons = [s for s in (self.left, self.right) if s.connected and s.client is not None]
            now = time.monotonic()
            if active and (not was_active or now - last_play > 0.9):
                sample = 1 if large >= small else 2
                for s in joycons:
                    try:
                        await s.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(sample), response=False)
                    except Exception:
                        pass
                last_play = now
            elif was_active and not active:
                for s in joycons:
                    try:
                        await s.client.write_gatt_char(COMMAND_WRITE_UUID, vibration_cmd(0), response=False)
                    except Exception:
                        pass
            was_active = active
            await asyncio.sleep(0.05)

    # ---- network bridge to a Deck -------------------------------------------

    def start_bridge(self, ip: str, code: str) -> str:
        """Starts the link. Returns "" when it started, or the reason it did not."""
        if self.link.state != "off":
            self.link.disconnect()
        try:
            self.link.configure(ip, code)
            self.link.start()
        except ValueError as exc:
            self.log(str(exc))
            return str(exc)
        return ""

    def stop_bridge(self):
        self.link.disconnect()

    def deck_connected(self) -> bool:
        return self.link.state == "connected"

    def _on_link_state(self, state: str):
        # Runs on the link's thread. Connected: the Deck has the controller, so this PC lets go of it.
        if state == "connected":
            self._sync_virtual_gamepad()
        else:
            self.rumble_large = self.rumble_small = 0  # no rumble left over from the Deck
            self._restore_virtual_gamepad()  # this PC uses the Joy-Cons again

    def _on_link_rumble(self, small: int, large: int):
        self.rumble_small = small
        self.rumble_large = large

    def _bridge_report(self) -> bytes:
        """The 64-byte DualShock 4 input report for the current Joy-Con state (the Deck passes it to Steam)."""
        left, right = self.left, self.right
        b = (left.buttons if left.connected else 0) | (right.buttons if right.connected else 0)
        lx = self._norm_axis(left.stick_x, left.center_x) if left.connected else 0
        ly = self._norm_axis(left.stick_y, left.center_y) if left.connected else 0
        rx = self._norm_axis(right.stick_x, right.center_x) if right.connected else 0
        ry = self._norm_axis(right.stick_y, right.center_y) if right.connected else 0
        rep = bytearray(64)
        rep[0] = 0x01
        rep[1] = stick_byte(lx)
        rep[2] = stick_byte(-ly)
        rep[3] = stick_byte(rx)
        rep[4] = stick_byte(-ry)
        face = bridge_dpad(b & BTN_UP, b & BTN_DOWN, b & BTN_LEFT, b & BTN_RIGHT)
        for mask, bit in ((BTN_Y, 0x10), (BTN_B, 0x20), (BTN_A, 0x40), (BTN_X, 0x80)):
            if b & mask:
                face |= bit
        rep[5] = face
        shoulders = 0
        for mask, bit in ((BTN_L, 0x01), (BTN_R, 0x02), (BTN_ZL, 0x04), (BTN_ZR, 0x08),
                          (BTN_MINUS, 0x10), (BTN_PLUS, 0x20), (BTN_L3, 0x40), (BTN_R3, 0x80)):
            if b & mask:
                shoulders |= bit
        rep[6] = shoulders
        rep[7] = (0x01 if b & BTN_HOME else 0) | (0x02 if b & BTN_CAPTURE else 0)
        rep[8] = 255 if b & BTN_ZL else 0
        rep[9] = 255 if b & BTN_ZR else 0
        struct.pack_into("<H", rep, 10, int(time.monotonic() * 187500) & 0xFFFF)
        driver = right if right.connected else (left if left.connected else None)
        if driver is not None:
            struct.pack_into("<hhh", rep, 13, *(clamp16(int(v * 16)) for v in
                                                (driver.gyro_x, driver.gyro_y, driver.gyro_z)))
            struct.pack_into("<hhh", rep, 19, *(clamp16(int(v * 8192)) for v in
                                                (driver.accel_x, driver.accel_y, driver.accel_z)))
        rep[30] = 0x1B
        return bytes(rep)

    def _sideways_joycon(self):
        """The one Joy-Con that works alone, held sideways: the one in use, while it is a controller."""
        if not self.sideways_single or self.active_side is None:
            return None
        jc = self._state_for(self.active_side)
        return jc if (jc.connected and self._contributes(jc)) else None

    def _sideways_pad(self, jc: JoyConState):
        """One Joy-Con held sideways as an Xbox-style pad: (stick x, stick y, set of pressed button names)."""
        b = jc.buttons
        sx = self._norm_axis(jc.stick_x, jc.center_x)
        sy = self._norm_axis(jc.stick_y, jc.center_y)
        if jc.side == "L":
            stick = (-sy, sx)
            pressed = (
                ("A", b & BTN_DOWN), ("B", b & BTN_RIGHT), ("X", b & BTN_LEFT), ("Y", b & BTN_UP),
                ("LB", b & BTN_SL_L), ("RB", b & BTN_SR_L), ("START", b & BTN_MINUS), ("LS", b & BTN_L3),
            )
        else:
            stick = (sy, -sx)
            pressed = (
                ("A", b & BTN_X), ("B", b & BTN_A), ("X", b & BTN_Y), ("Y", b & BTN_B),
                ("LB", b & BTN_SL_R), ("RB", b & BTN_SR_R), ("START", b & BTN_PLUS), ("LS", b & BTN_R3),
            )
        return stick[0], stick[1], {name for name, on in pressed if on}

    def _sync_ds4(self):
        ds4 = self._ensure_ds4()
        if ds4 is None:
            return
        D = vg.DS4_BUTTONS
        ds4.reset()
        if self.sideways_single:
            jc = self._sideways_joycon()
            if jc is not None:
                # Sideways: the Joy-Con in use is the whole controller, laid out like a pad
                sx, sy, pressed = self._sideways_pad(jc)
                ds4.left_joystick_float(sx / 32767.0, -sy / 32767.0)
                buttons = {
                    "A": D.DS4_BUTTON_CROSS, "B": D.DS4_BUTTON_CIRCLE,
                    "X": D.DS4_BUTTON_SQUARE, "Y": D.DS4_BUTTON_TRIANGLE,
                    "LB": D.DS4_BUTTON_SHOULDER_LEFT, "RB": D.DS4_BUTTON_SHOULDER_RIGHT,
                    "START": D.DS4_BUTTON_OPTIONS, "LS": D.DS4_BUTTON_THUMB_LEFT,
                }
                for name in pressed:
                    ds4.press_button(buttons[name])
                ds4.gyroscope(*(clamp16(int(v * 16)) for v in (jc.gyro_x, jc.gyro_y, jc.gyro_z)))
                ds4.accelerometer(*(clamp16(int(v * 8192)) for v in (jc.accel_x, jc.accel_y, jc.accel_z)))
            ds4.update()  # neutral while the Joy-Con in use is the mouse
            return
        left_on = self.left.connected and self._contributes(self.left)
        right_on = self.right.connected and self._contributes(self.right)
        b = (self.left.buttons if left_on else 0) | (self.right.buttons if right_on else 0)
        lx = self._norm_axis(self.left.stick_x, self.left.center_x) if left_on else 0
        ly = self._norm_axis(self.left.stick_y, self.left.center_y) if left_on else 0
        rx = self._norm_axis(self.right.stick_x, self.right.center_x) if right_on else 0
        ry = self._norm_axis(self.right.stick_y, self.right.center_y) if right_on else 0
        # DualShock 4 sticks are 8-bit with 128 as center, and Y is inverted (0 = up)
        ds4.left_joystick_float(lx / 32767.0, -ly / 32767.0)
        ds4.right_joystick_float(rx / 32767.0, -ry / 32767.0)
        # Nintendo positions: A is on the right (Circle), B at the bottom (Cross),
        # X on top (Triangle), Y on the left (Square)
        for mask, button in (
            (BTN_A, D.DS4_BUTTON_CIRCLE),
            (BTN_B, D.DS4_BUTTON_CROSS),
            (BTN_X, D.DS4_BUTTON_TRIANGLE),
            (BTN_Y, D.DS4_BUTTON_SQUARE),
            (BTN_L, D.DS4_BUTTON_SHOULDER_LEFT),
            (BTN_R, D.DS4_BUTTON_SHOULDER_RIGHT),
            (BTN_ZL, D.DS4_BUTTON_TRIGGER_LEFT),
            (BTN_ZR, D.DS4_BUTTON_TRIGGER_RIGHT),
            (BTN_L3, D.DS4_BUTTON_THUMB_LEFT),
            (BTN_R3, D.DS4_BUTTON_THUMB_RIGHT),
            (BTN_PLUS, D.DS4_BUTTON_OPTIONS),
            (BTN_MINUS, D.DS4_BUTTON_SHARE),
        ):
            if b & mask:
                ds4.press_button(button)
        ds4.left_trigger(255 if (b & BTN_ZL) else 0)
        ds4.right_trigger(255 if (b & BTN_ZR) else 0)
        if b & BTN_HOME:
            ds4.press_special_button(vg.DS4_SPECIAL_BUTTONS.DS4_SPECIAL_BUTTON_PS)
        if b & BTN_CAPTURE:
            ds4.press_special_button(vg.DS4_SPECIAL_BUTTONS.DS4_SPECIAL_BUTTON_TOUCHPAD)
        ds4.directional_pad(dpad_direction(b & BTN_UP, b & BTN_DOWN, b & BTN_LEFT, b & BTN_RIGHT))
        driver = self.right if right_on else (self.left if left_on else None)
        if driver is not None:
            # DualShock 4 units: 16 LSB per deg/s and 8192 LSB per g
            ds4.gyroscope(*(clamp16(int(v * 16)) for v in (driver.gyro_x, driver.gyro_y, driver.gyro_z)))
            ds4.accelerometer(*(clamp16(int(v * 8192)) for v in (driver.accel_x, driver.accel_y, driver.accel_z)))
        ds4.update()

    def _norm_axis(self, raw: int, center: int, deadzone: int = 140, span: float = 1750.0) -> int:
        delta = raw - center
        if abs(delta) < deadzone:
            return 0
        scaled = max(-1.0, min(1.0, delta / span))
        return int(scaled * 32767)

    def _reset_virtual_gamepad(self):
        if self.gamepad:
            self.gamepad.reset()
            self.gamepad.update()
        if self.ds4 is not None:
            self.ds4.reset()
            self.ds4.update()

    def _release_virtual_gamepads(self):
        """Unplugs this PC's virtual controller, so Windows and its games do not see the Joy-Cons."""
        if self.gamepad is not None:
            self.gamepad.reset()
            self.gamepad.update()
            self.gamepad = None  # releasing the Xbox 360 object unplugs it
        if self.ds4 is not None:
            self.ds4.reset()
            self.ds4.update()
            self.ds4 = None  # releasing the DualShock 4 object unplugs it

    def _restore_virtual_gamepad(self):
        """Plugs this PC's virtual controller back in when the Deck bridge stops."""
        self._ds4_failed = False
        if self.output == "x360" and HAS_VGAMEPAD and self.gamepad is None:
            try:
                self.gamepad = vg.VX360Gamepad()
            except Exception as e:
                self.log(f"ViGEmBus driver warning: {e}")
        self._sync_virtual_gamepad()

    def _sync_virtual_gamepad(self):
        if IS_LINUX:
            # Linux: _local_pad_loop makes and removes this PC's DualShock 4, on the Bluetooth thread
            self.want_local_pad = not self.deck_connected()
            return
        if self.deck_connected():
            # The Deck has the controller while it is connected, so this PC's virtual controller stays unplugged
            self._release_virtual_gamepads()
            return
        if self.output == "ds4":
            self._sync_ds4()
            return
        if not self.gamepad:
            return

        self.gamepad.reset()
        if self.sideways_single:
            jc = self._sideways_joycon()
            if jc is not None:
                # Sideways: the Joy-Con in use is the whole controller, laid out like a pad
                xb = vg.XUSB_BUTTON
                names = {
                    "A": xb.XUSB_GAMEPAD_A, "B": xb.XUSB_GAMEPAD_B,
                    "X": xb.XUSB_GAMEPAD_X, "Y": xb.XUSB_GAMEPAD_Y,
                    "LB": xb.XUSB_GAMEPAD_LEFT_SHOULDER, "RB": xb.XUSB_GAMEPAD_RIGHT_SHOULDER,
                    "START": xb.XUSB_GAMEPAD_START, "LS": xb.XUSB_GAMEPAD_LEFT_THUMB,
                }
                sx, sy, pressed = self._sideways_pad(jc)
                self.gamepad.left_joystick(x_value=sx, y_value=sy)
                for name in pressed:
                    self.gamepad.press_button(names[name])
            self.gamepad.update()  # neutral while the Joy-Con in use is the mouse
            return

        # Standard Unified Mode (works with Left only, Right only, or Both merged)
        left_on = self.left.connected and self._contributes(self.left)
        right_on = self.right.connected and self._contributes(self.right)
        b = (self.left.buttons if left_on else 0) | (self.right.buttons if right_on else 0)

        btn_pairs = [
            (BTN_A, vg.XUSB_BUTTON.XUSB_GAMEPAD_A),
            (BTN_B, vg.XUSB_BUTTON.XUSB_GAMEPAD_B),
            (BTN_X, vg.XUSB_BUTTON.XUSB_GAMEPAD_X),
            (BTN_Y, vg.XUSB_BUTTON.XUSB_GAMEPAD_Y),
            (BTN_UP, vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP),
            (BTN_DOWN, vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN),
            (BTN_LEFT, vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT),
            (BTN_RIGHT, vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT),
            (BTN_L, vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER),
            (BTN_R, vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER),
            (BTN_L3, vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB),
            (BTN_R3, vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB),
            (BTN_PLUS, vg.XUSB_BUTTON.XUSB_GAMEPAD_START),
            (BTN_MINUS, vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK),
            (BTN_HOME, vg.XUSB_BUTTON.XUSB_GAMEPAD_GUIDE),
        ]
        for mask, xb in btn_pairs:
            if b & mask:
                self.gamepad.press_button(xb)

        self.gamepad.left_trigger(255 if (b & BTN_ZL) else 0)
        self.gamepad.right_trigger(255 if (b & BTN_ZR) else 0)

        lx = self._norm_axis(self.left.stick_x, self.left.center_x) if left_on else 0
        ly = self._norm_axis(self.left.stick_y, self.left.center_y) if left_on else 0
        rx = self._norm_axis(self.right.stick_x, self.right.center_x) if right_on else 0
        ry = self._norm_axis(self.right.stick_y, self.right.center_y) if right_on else 0

        if self.gyro_aim_enabled:
            active_imu = self.right if right_on else (self.left if left_on else None)
            if active_imu:
                gx_add = int(
                    max(-1.0, min(1.0, -active_imu.gyro_z * self.gyro_sensitivity / 360.0)) * 32767
                )
                gy_add = int(
                    max(-1.0, min(1.0, active_imu.gyro_x * self.gyro_sensitivity / 360.0)) * 32767
                )
                rx = max(-32767, min(32767, rx + gx_add))
                ry = max(-32767, min(32767, ry + gy_add))

        self.gamepad.left_joystick(x_value=lx, y_value=ly)
        self.gamepad.right_joystick(x_value=rx, y_value=ry)
        self.gamepad.update()

    # ---- Linux: this PC's own DualShock 4 (through /dev/uhid) and mouse (through /dev/uinput) ----

    def _open_uinput_mouse(self):
        try:
            mouse = UinputMouse()
            mouse.create()
            self.uinput_mouse = mouse
        except Exception as exc:
            self.uinput_mouse = None
            self.log(f"Mouse mode needs /dev/uinput ({exc}). See the About page.")

    def _mouse_move(self, dx: int, dy: int):
        if IS_WINDOWS:
            ctypes.windll.user32.mouse_event(0x0001, dx, dy, 0, 0)  # MOUSEEVENTF_MOVE
        elif self.uinput_mouse is not None:
            self.uinput_mouse.move(dx, dy)

    def _mouse_button(self, button: str, down: bool):
        if IS_WINDOWS:
            flags = {("left", True): 0x0002, ("left", False): 0x0004,
                     ("right", True): 0x0008, ("right", False): 0x0010}
            ctypes.windll.user32.mouse_event(flags[(button, down)], 0, 0, 0, 0)
        elif self.uinput_mouse is not None:
            self.uinput_mouse.button(button, down)

    def _on_local_rumble(self, data: bytes):
        # Steam's rumble to this PC's DualShock 4: report 0x05, small motor in byte 4, large motor in byte 5
        if len(data) >= 6 and data[0] == 0x05:
            self.rumble_small = data[4]
            self.rumble_large = data[5]

    def _local_ds4_report(self) -> bytes:
        """This PC's controller as a 64-byte DualShock 4 input report, mapped the same way as _sync_ds4."""
        rep = bytearray(64)
        rep[0] = 0x01
        rep[1] = rep[2] = rep[3] = rep[4] = 128  # sticks centred
        face, shoulders, system, left_trigger, right_trigger = 8, 0, 0, 0, 0  # d-pad 8 = centred
        driver = None
        if self.sideways_single:
            jc = self._sideways_joycon()
            if jc is not None:
                # Sideways: the Joy-Con in use is the whole controller, laid out like a pad
                sx, sy, pressed = self._sideways_pad(jc)
                rep[1], rep[2] = stick_byte(sx), stick_byte(-sy)
                face_bits = {"A": 0x20, "B": 0x40, "X": 0x10, "Y": 0x80}  # Cross, Circle, Square, Triangle
                shoulder_bits = {"LB": 0x01, "RB": 0x02, "START": 0x20, "LS": 0x40}
                for name in pressed:
                    face |= face_bits.get(name, 0)
                    shoulders |= shoulder_bits.get(name, 0)
                driver = jc
        else:
            left_on = self.left.connected and self._contributes(self.left)
            right_on = self.right.connected and self._contributes(self.right)
            b = (self.left.buttons if left_on else 0) | (self.right.buttons if right_on else 0)
            lx = self._norm_axis(self.left.stick_x, self.left.center_x) if left_on else 0
            ly = self._norm_axis(self.left.stick_y, self.left.center_y) if left_on else 0
            rx = self._norm_axis(self.right.stick_x, self.right.center_x) if right_on else 0
            ry = self._norm_axis(self.right.stick_y, self.right.center_y) if right_on else 0
            rep[1], rep[2] = stick_byte(lx), stick_byte(-ly)  # DualShock 4 Y is inverted: 0 is up
            rep[3], rep[4] = stick_byte(rx), stick_byte(-ry)
            face = bridge_dpad(b & BTN_UP, b & BTN_DOWN, b & BTN_LEFT, b & BTN_RIGHT)
            for mask, bit in ((BTN_Y, 0x10), (BTN_B, 0x20), (BTN_A, 0x40), (BTN_X, 0x80)):
                if b & mask:
                    face |= bit
            for mask, bit in ((BTN_L, 0x01), (BTN_R, 0x02), (BTN_MINUS, 0x10), (BTN_PLUS, 0x20),
                              (BTN_L3, 0x40), (BTN_R3, 0x80)):
                if b & mask:
                    shoulders |= bit
            if b & BTN_ZL:
                shoulders |= 0x04
                left_trigger = 255
            if b & BTN_ZR:
                shoulders |= 0x08
                right_trigger = 255
            if b & BTN_HOME:
                system |= 0x01
            if b & BTN_CAPTURE:
                system |= 0x02
            driver = self.right if right_on else (self.left if left_on else None)
        rep[5] = face
        rep[6] = shoulders
        rep[7] = system
        rep[8] = left_trigger
        rep[9] = right_trigger
        struct.pack_into("<H", rep, 10, int(time.monotonic() * 187500) & 0xFFFF)
        if driver is not None:
            # DualShock 4 units: 16 LSB per deg/s and 8192 LSB per g
            struct.pack_into("<hhh", rep, 13, *(clamp16(int(v * 16)) for v in
                                                (driver.gyro_x, driver.gyro_y, driver.gyro_z)))
            struct.pack_into("<hhh", rep, 19, *(clamp16(int(v * 8192)) for v in
                                                (driver.accel_x, driver.accel_y, driver.accel_z)))
        rep[30] = 0x1B
        return bytes(rep)

    async def _local_pad_loop(self):
        """Linux: keeps this PC's DualShock 4 in place while the Joy-Cons drive it, about 100 times a second."""
        pad = None
        retry_at = 0.0
        while True:
            now = time.monotonic()
            want = self.want_local_pad and self.output == "ds4"
            if want and pad is None and now >= retry_at:
                try:
                    pad = UhidGamepad(self.loop, self._on_local_rumble)
                    pad.create()
                    self.uhid_pad = pad
                    self.local_pad_message = ""
                except Exception as exc:
                    pad = None
                    self.uhid_pad = None
                    message = (f"needs /dev/uhid ({exc.strerror or exc})" if isinstance(exc, OSError)
                               else f"could not start ({exc})")
                    if message != self.local_pad_message:
                        self.local_pad_message = message
                        self.log(f"Virtual controller {message}. See the About page.")
                    retry_at = now + 5.0
            elif not want and pad is not None:
                pad.destroy()  # the Deck has the controller now, or the output was changed
                pad = None
                self.uhid_pad = None
                self.rumble_large = self.rumble_small = 0
            if pad is not None:
                try:
                    pad.send_input(self._local_ds4_report())
                except OSError as exc:
                    self.local_pad_message = f"write failed ({exc.strerror or exc})"
                    self.log(f"Virtual controller {self.local_pad_message}.")
            await asyncio.sleep(0.01)


# ---- Look and feel ----------------------------------------------------------
# --- Material 3 colour roles (dark scheme). The names follow the M3 roles, so the palette can be swapped. ---
BG = "#141218"               # background
SURFACE = "#211F26"          # surface container: cards
SURFACE_2 = "#2B2930"        # surface container high: panels inside cards
SURFACE_3 = "#36343B"        # surface container highest: fields and bars
BORDER = "#49454F"           # outline variant
OUTLINE = "#938F99"          # outline
TEXT = "#E6E0E9"             # on surface
MUTED = "#CAC4D0"            # on surface variant
PRIMARY = "#D0BCFF"
ON_PRIMARY = "#381E72"
PRIMARY_CONTAINER = "#4F378B"
ON_PRIMARY_CONTAINER = "#EADDFF"
SECONDARY_CONTAINER = "#4A4458"
ON_SECONDARY_CONTAINER = "#E8DEF8"
ERROR = "#F2B8B5"
ERROR_CONTAINER = "#8C1D18"
ON_ERROR_CONTAINER = "#F9DEDC"
GOOD = "#A8DAB5"
GOOD_CONTAINER = "#1F4D2E"
ON_GOOD_CONTAINER = "#C4F0D0"
WARN = "#F5C98C"
WARN_CONTAINER = "#4F3A14"
ON_WARN_CONTAINER = "#FFDDB0"
INVERSE_SURFACE = "#E6E0E9"  # the snackbar
INVERSE_TEXT = "#322F35"
BAD = ERROR                  # low battery
ACCENT = PRIMARY             # bars
ACTIVE_L = "#38BDF8"         # buttons pressed on the Joy-Con preview
ACTIVE_R = "#F87171"
FONT = "Segoe UI" if IS_WINDOWS else "DejaVu Sans"
MONO = "Consolas" if IS_WINDOWS else "DejaVu Sans Mono"

# The glass look (Windows 11): the window and the cards become see-through, so Windows' acrylic backdrop shows
# through them. It is a setting, and it takes effect when the app starts.
GLASS_KEY = "#010203"  # the colour Tk makes see-through; no part of the app uses it
GLASS_AVAILABLE = IS_WINDOWS and sys.getwindowsversion().build >= 22621  # Windows 11 22H2 and later
GLASS = GLASS_AVAILABLE and bool(load_settings().get("glass", False))
if GLASS:
    BG = GLASS_KEY
    SURFACE = GLASS_KEY
    SURFACE_2 = GLASS_KEY


def _mix(a: str, b: str, t: float) -> str:
    """Blends colour a towards colour b by t, from 0 to 1."""
    ra, ga, ba = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    rb, gb, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return "#%02X%02X%02X" % (
        round(ra + (rb - ra) * t),
        round(ga + (gb - ga) * t),
        round(ba + (bb - ba) * t),
    )


def rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


def make_chip(parent, text, bg, fg):
    """A small tonal label: the Material 3 chip used for status."""
    return tk.Label(parent, text=text, font=(FONT, 9, "bold"), fg=fg, bg=bg, padx=12, pady=5)


class Card(tk.Canvas):
    """A rounded surface. Put the content in .body; the card grows to fit it."""

    def __init__(self, parent, bg=BG, fill=SURFACE, pad=18, radius=22):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, height=1)
        self.fill = fill
        self.pad = pad
        self.radius = radius
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(pad, pad, anchor="nw", window=self.body)
        self.bind("<Configure>", self._fit)
        self.body.bind("<Configure>", self._fit)

    def _fit(self, _event=None):
        w = self.winfo_width()
        if w < 2 * self.pad + 4:
            return
        h = self.body.winfo_reqheight() + 2 * self.pad
        self.itemconfigure(self._win, width=w - 2 * self.pad)
        if int(self.cget("height")) != h:
            self.configure(height=h)
        self.delete("bg")
        if GLASS:
            rounded_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=self.fill, outline="#8D8799", width=1.5, tags="bg")
        else:
            rounded_rect(self, 0, 0, w, h, self.radius, fill=self.fill, outline="", tags="bg")
        self.tag_lower("bg")


class M3Button(tk.Canvas):
    """A Material 3 button: filled, tonal, outlined, text or danger. Drawn on a canvas so the corners are round."""

    LOOK = {
        # style: (fill, text colour, outline)
        "filled": (PRIMARY, ON_PRIMARY, None),
        "tonal": (SECONDARY_CONTAINER, ON_SECONDARY_CONTAINER, None),
        "outlined": (None, PRIMARY, OUTLINE),
        "text": (None, PRIMARY, None),
        "danger": (ERROR_CONTAINER, ON_ERROR_CONTAINER, None),
    }

    def __init__(self, parent, text, command=None, style="tonal", bg=BG, compact=False):
        self._font = tkfont.Font(family=FONT, size=9 if compact else 10, weight="bold")
        self._pad = 14 if compact else 22
        self._h = 32 if compact else 40
        self._text = text
        self._command = command
        self._style = style
        self._bg = bg
        self._enabled = True
        self._hover = False
        self._down = False
        super().__init__(parent, width=self._text_width(), height=self._h, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Enter>", lambda _e: self._flag(hover=True))
        self.bind("<Leave>", lambda _e: self._flag(hover=False, down=False))
        self.bind("<ButtonPress-1>", lambda _e: self._flag(down=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def _text_width(self):
        return self._font.measure(self._text) + 2 * self._pad

    def _flag(self, **flags):
        self._hover = flags.get("hover", self._hover)
        self._down = flags.get("down", self._down)
        self._draw()

    def _release(self, event):
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        fire = self._down and inside and self._enabled
        self._down = False
        self._draw()
        if fire and self._command:
            self._command()

    def configure(self, **kw):
        if "text" in kw:
            self._text = kw.pop("text")
            kw["width"] = self._text_width()
        if "command" in kw:
            self._command = kw.pop("command")
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
            kw["cursor"] = "hand2" if self._enabled else "arrow"
        super().configure(**kw)
        self._draw()

    config = configure

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        fill, fg, outline = self.LOOK[self._style]
        if not self._enabled:
            fg = _mix(fg, self._bg, 0.6)
            fill = _mix(fill, self._bg, 0.88) if fill else None
            outline = _mix(outline, self._bg, 0.6) if outline else None
        layer = 0.0
        if self._enabled:
            layer = 0.12 if self._down else (0.08 if self._hover else 0.0)
        if layer:
            fill = _mix(fill or self._bg, fg, layer)  # the hover and press tint
        if fill or outline:
            rounded_rect(self, 1, 1, w - 1, h - 1, (h - 2) // 2,
                         fill=fill or self._bg, outline=outline or "", width=1)
        self.create_text(w // 2, h // 2, text=self._text, fill=fg, font=self._font)


class M3Switch(tk.Canvas):
    """A Material 3 switch, bound to a BooleanVar."""

    def __init__(self, parent, variable, command=None, bg=BG):
        super().__init__(parent, width=52, height=32, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.var = variable
        self.command = command
        self.bind("<Button-1>", self._click)
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _click(self, _event):
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self):
        self.delete("all")
        on = bool(self.var.get())
        rounded_rect(self, 2, 2, 50, 30, 14, fill=PRIMARY if on else SURFACE_3,
                     outline=PRIMARY if on else OUTLINE, width=2)
        cx, r = (38, 10) if on else (16, 7)
        self.create_oval(cx - r, 16 - r, cx + r, 16 + r, fill=ON_PRIMARY if on else OUTLINE, outline="")


class M3Segmented(tk.Canvas):
    """Choices in one rounded strip, like a Material 3 segmented button. Bound to a StringVar."""

    def __init__(self, parent, choices, variable, command=None, width=320, bg=BG):
        super().__init__(parent, width=width, height=40, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.choices = list(choices)
        self.var = variable
        self.command = command
        self._bg = bg
        self._font = tkfont.Font(family=FONT, size=9, weight="bold")
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda _e: self._draw())
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        seg = w / len(self.choices)
        chosen = self.var.get()
        rounded_rect(self, 1, 1, w - 1, h - 1, (h - 2) // 2, fill=self._bg, outline=OUTLINE, width=1)
        for i, choice in enumerate(self.choices):
            x0, x1 = i * seg, (i + 1) * seg
            on = choice == chosen
            if on:
                rounded_rect(self, x0 + 4, 4, x1 - 4, h - 4, (h - 8) // 2, fill=SECONDARY_CONTAINER, outline="")
            elif i > 0 and self.choices[i - 1] != chosen:
                self.create_line(x0, 10, x0, h - 10, fill=OUTLINE)
            self.create_text((x0 + x1) / 2, h / 2, text=choice,
                             fill=ON_SECONDARY_CONTAINER if on else MUTED, font=self._font)

    def _click(self, event):
        w = max(1, self.winfo_width())
        index = min(len(self.choices) - 1, int(event.x * len(self.choices) / w))
        choice = self.choices[index]
        self.var.set(choice)
        if self.command:
            self.command(choice)


class M3Slider(tk.Canvas):
    """A Material 3 slider: a value from lo to hi, moved with the mouse, in steps."""

    PAD = 12

    def __init__(self, parent, lo, hi, value, command=None, step=0.05, width=200, bg=BG):
        super().__init__(parent, width=width, height=28, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._lo, self._hi, self._step = lo, hi, step
        self._value = self._snap(value)
        self.command = command
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self._draw()

    def _snap(self, value):
        value = max(self._lo, min(self._hi, float(value)))
        return round(round((value - self._lo) / self._step) * self._step + self._lo, 2)

    def get(self):
        return self._value

    def set(self, value):
        self._value = self._snap(value)
        self._draw()

    def _drag(self, event):
        w = max(1, self.winfo_width() - 2 * self.PAD)
        frac = max(0.0, min(1.0, (event.x - self.PAD) / w))
        self._value = self._snap(self._lo + frac * (self._hi - self._lo))
        self._draw()
        if self.command:
            self.command(self._value)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        if w < 4:
            return
        frac = (self._value - self._lo) / (self._hi - self._lo)
        x = self.PAD + frac * (w - 2 * self.PAD)
        self.create_line(self.PAD, 14, w - self.PAD, 14, fill=SURFACE_3, width=4, capstyle="round")
        self.create_line(self.PAD, 14, x, 14, fill=PRIMARY, width=4, capstyle="round")
        self.create_oval(x - 9, 5, x + 9, 23, fill=PRIMARY, outline="")


class M3Field(tk.Frame):
    """A labelled text field drawn as a rounded outline, with a short helper line under it."""

    def __init__(self, parent, label, variable, helper="", width=240, bg=BG):
        super().__init__(parent, bg=bg)
        self._fw = width
        self._focused = False
        tk.Label(self, text=label, font=(FONT, 9), fg=MUTED, bg=bg).pack(anchor="w")
        self._box = tk.Canvas(self, width=width, height=44, bg=bg, highlightthickness=0, bd=0)
        self._box.pack(anchor="w", pady=(4, 0))
        self.entry = tk.Entry(self._box, textvariable=variable, font=(FONT, 10), bg=SURFACE_3, fg=TEXT,
                              insertbackground=TEXT, relief="flat", bd=0, highlightthickness=0)
        self._box.create_window(14, 22, anchor="w", window=self.entry, width=width - 28)
        self.entry.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self._draw()
        if helper:
            tk.Label(self, text=helper, font=(FONT, 8), fg=MUTED, bg=bg, justify="left",
                     wraplength=width).pack(anchor="w", pady=(4, 0))

    def _set_focus(self, on):
        self._focused = on
        self._draw()

    def _draw(self):
        self._box.delete("outline")
        w = self._fw
        if self._focused:
            rounded_rect(self._box, 1, 1, w - 1, 43, 12, fill=SURFACE_3, outline=PRIMARY, width=2, tags="outline")
        else:
            rounded_rect(self._box, 1, 1, w - 1, 43, 12, fill=SURFACE_3, outline=OUTLINE, width=1, tags="outline")
        self._box.tag_lower("outline")


class M3Menu(tk.Canvas):
    """A Material 3 exposed dropdown: shows the chosen value, and a click opens the list."""

    def __init__(self, parent, choices, variable, command=None, width=320, bg=BG):
        super().__init__(parent, width=width, height=44, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.var = variable
        self.command = command
        self._font = tkfont.Font(family=FONT, size=10)
        self._menu = tk.Menu(self, tearoff=0, bg=SURFACE_2, fg=TEXT, activebackground=PRIMARY_CONTAINER,
                             activeforeground=ON_PRIMARY_CONTAINER, font=(FONT, 10), bd=0, relief="flat")
        for choice in choices:
            self._menu.add_command(label=choice, command=lambda c=choice: self._pick(c))
        self.bind("<Button-1>", self._open)
        self.bind("<Configure>", lambda _e: self._draw())
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _open(self, _event):
        try:
            self._menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + 4)
        finally:
            self._menu.grab_release()

    def _pick(self, choice):
        self.var.set(choice)
        if self.command:
            self.command(choice)

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        rounded_rect(self, 1, 1, w - 1, h - 1, 12, fill=SURFACE_3, outline=OUTLINE, width=1)
        self.create_text(16, h // 2, text=self.var.get(), anchor="w", fill=TEXT, font=self._font)
        x = w - 22
        self.create_polygon(x - 6, h // 2 - 3, x + 6, h // 2 - 3, x, h // 2 + 4, fill=MUTED, outline="")


class ScrollPage(tk.Frame):
    """A page that scrolls: its content goes in .body. The window's mouse-wheel handler scrolls the page under the pointer."""

    def __init__(self, parent, bg: str):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=bg)
        inner = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(inner, width=e.width))


class NavItem(tk.Canvas):
    """One page button in the left rail. A pill sits behind the label when it is the current page."""

    def __init__(self, parent, text, command):
        super().__init__(parent, width=112, height=52, bg=BG, highlightthickness=0, bd=0, cursor="hand2")
        self._text = text
        self._command = command
        self._selected = False
        self._hover = False
        self._font = tkfont.Font(family=FONT, size=10, weight="bold")
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self._command())
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def select(self, on):
        self._selected = on
        self._draw()

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        if self._selected:
            rounded_rect(self, 4, 4, w - 4, h - 4, (h - 8) // 2, fill=PRIMARY_CONTAINER, outline="")
            fg = ON_PRIMARY_CONTAINER
        else:
            if self._hover:
                rounded_rect(self, 4, 4, w - 4, h - 4, (h - 8) // 2, fill=_mix(BG, TEXT, 0.08), outline="")
            fg = MUTED
        self.create_text(w // 2, h // 2, text=self._text, fill=fg, font=self._font)


class Snackbar(tk.Canvas):
    """The status message, shown as a Material 3 snackbar along the bottom of the window."""

    def __init__(self, parent):
        super().__init__(parent, height=50, bg=BG, highlightthickness=0, bd=0)
        self._text = ""
        self._font = tkfont.Font(family=FONT, size=9)
        self.bind("<Configure>", lambda _e: self._draw())

    def set_text(self, text):
        self._text = text
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        rounded_rect(self, 0, 0, w, h, 16, fill=INVERSE_SURFACE, outline="")
        self.create_text(22, h // 2, text=self._text, anchor="w", fill=INVERSE_TEXT,
                         font=self._font, width=w - 44)


JC_SIZE = (240, 340)  # the Joy-Con picture, in pixels

BUTTON_NAMES = (
    (BTN_A, "A"), (BTN_B, "B"), (BTN_X, "X"), (BTN_Y, "Y"),
    (BTN_UP, "Up"), (BTN_DOWN, "Down"), (BTN_LEFT, "Left"), (BTN_RIGHT, "Right"),
    (BTN_PLUS, "+"), (BTN_MINUS, "-"), (BTN_HOME, "Home"), (BTN_CAPTURE, "Capture"),
    (BTN_L, "L"), (BTN_R, "R"), (BTN_ZL, "ZL"), (BTN_ZR, "ZR"),
    (BTN_L3, "L3"), (BTN_R3, "R3"),
    (BTN_SL_L, "SL"), (BTN_SR_L, "SR"), (BTN_SL_R, "SL"), (BTN_SR_R, "SR"),
)


def pressed_names(s: JoyConState) -> list:
    """The names of the buttons held down right now on this Joy-Con."""
    if not s.connected:
        return []
    names = []
    for mask, name in BUTTON_NAMES:
        if s.buttons & mask and name not in names:
            names.append(name)
    return names


def draw_joycon(canvas, s: JoyConState, hint=()):
    """A Joy-Con drawn with every button in its place. A held button lights up; a ringed one is a mouse click."""
    canvas.delete("all")
    right = s.side == "R"
    rounded_rect(canvas, 2, 2, JC_SIZE[0] - 2, JC_SIZE[1] - 2, 22, fill=SURFACE_2, outline="")

    def lit(mask):
        return bool(s.connected and (s.buttons & mask))

    def ring(mask):
        return WARN if mask in hint else "#4B5563"

    def key(cx, cy, label, mask, r=14, size=8):
        """A round button."""
        on = lit(mask)
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=PRIMARY if on else s.btn_color,
                           outline=ring(mask), width=3 if mask in hint else 1)
        canvas.create_text(cx, cy, text=label, fill=ON_PRIMARY if on else TEXT, font=(FONT, size, "bold"))

    def pill(cx, cy, hw, hh, label, mask, size=7):
        """A pill-shaped button: shoulders, rail buttons, Home and Capture."""
        on = lit(mask)
        rounded_rect(canvas, cx - hw, cy - hh, cx + hw, cy + hh, min(hw, hh),
                     fill=PRIMARY if on else s.btn_color, outline=ring(mask), width=3 if mask in hint else 1)
        canvas.create_text(cx, cy, text=label, fill=ON_PRIMARY if on else TEXT, font=(FONT, size, "bold"))

    def stick(cx, cy, r, mask, label):
        """The stick well: its dot follows the stick, and the well lights up when the stick is pressed in."""
        on = lit(mask)
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                           fill=PRIMARY_CONTAINER if on else SURFACE_3, outline=OUTLINE)
        nx = max(-1.0, min(1.0, (s.stick_x - s.center_x) / 1750.0)) if s.connected else 0.0
        ny = max(-1.0, min(1.0, (s.stick_y - s.center_y) / 1750.0)) if s.connected else 0.0
        px, py = cx + nx * (r - 12), cy - ny * (r - 12)
        colour = PRIMARY if s.connected else MUTED
        canvas.create_oval(px - 9, py - 9, px + 9, py + 9, fill=colour, outline="")
        canvas.create_text(cx, cy + r + 12, text=label, fill=MUTED, font=(FONT, 7, "bold"))

    # Shoulder buttons sit on top of the body
    if right:
        pill(171, 27, 33, 13, "R", BTN_R)
        pill(99, 27, 29, 13, "ZR", BTN_ZR)
        body_x1, body_x2 = 44, 226
        rail_x1, rail_x2 = 8, 52
        rail_x = 30
    else:
        pill(69, 27, 33, 13, "L", BTN_L)
        pill(141, 27, 29, 13, "ZL", BTN_ZL)
        body_x1, body_x2 = 14, 196
        rail_x1, rail_x2 = 188, 232
        rail_x = 210
    rounded_rect(canvas, body_x1, 34, body_x2, 326, 46, fill=s.body_color, outline=BORDER, width=2)
    rounded_rect(canvas, rail_x1, 56, rail_x2, 304, 16, fill=s.rail_color, outline="")

    # Rail: SL and SR on the inside of the rail, and the SYNC button
    if right:
        pill(rail_x, 107, 11, 13, "SL", BTN_SL_R, 6)
        pill(rail_x, 245, 11, 13, "SR", BTN_SR_R, 6)
    else:
        pill(rail_x, 107, 11, 13, "SL", BTN_SL_L, 6)
        pill(rail_x, 245, 11, 13, "SR", BTN_SR_L, 6)
    canvas.create_oval(rail_x - 6, 66, rail_x + 6, 78, fill=s.btn_color, outline=OUTLINE)  # SYNC

    if right:
        key(196, 115, "A", BTN_A)
        key(160, 151, "B", BTN_B)
        key(160, 79, "X", BTN_X)
        key(124, 115, "Y", BTN_Y)
        key(92, 70, "+", BTN_PLUS, r=12)
        pill(92, 104, 17, 11, "HOME", BTN_HOME, 6)
        stick(118, 246, 38, BTN_R3, "R3")
    else:
        key(80, 79, "↑", BTN_UP, r=12)
        key(80, 151, "↓", BTN_DOWN, r=12)
        key(44, 115, "←", BTN_LEFT, r=12)
        key(116, 115, "→", BTN_RIGHT, r=12)
        key(146, 72, "-", BTN_MINUS, r=12)
        pill(152, 108, 19, 11, "CAPTURE", BTN_CAPTURE, 6)
        stick(122, 246, 38, BTN_L3, "L3")


class JoyConCard(Card):
    """One Joy-Con: a picture with every button, the buttons held right now, live readings and colour settings."""

    def __init__(self, parent, title: str, state: JoyConState, manager):
        super().__init__(parent, bg=BG, fill=SURFACE, pad=18, radius=24)
        self.joycon = state
        self.side = state.side
        self.manager = manager
        body = self.body

        head = tk.Frame(body, bg=SURFACE)
        head.pack(fill="x")
        tk.Label(head, text=title, font=(FONT, 12, "bold"), fg=TEXT, bg=SURFACE).pack(side="left")
        self.status_lbl = make_chip(head, "WAITING", SECONDARY_CONTAINER, ON_SECONDARY_CONTAINER)
        self.status_lbl.pack(side="right")
        self.role_lbl = make_chip(head, "", SURFACE, ON_PRIMARY_CONTAINER)
        self.role_lbl.pack(side="right", padx=(0, 8))

        row = tk.Frame(body, bg=SURFACE)
        row.pack(fill="x", pady=(12, 0))
        self.picture = tk.Canvas(row, width=JC_SIZE[0], height=JC_SIZE[1], bg=SURFACE, highlightthickness=0, bd=0)
        self.picture.pack(side="left", anchor="n")

        column = tk.Frame(row, bg=SURFACE)
        column.pack(side="left", fill="both", expand=True, padx=(14, 0))
        top = tk.Frame(column, bg=SURFACE)
        top.pack(fill="x")
        self.bat_text = tk.Label(top, text="Battery --", font=(FONT, 9), fg=MUTED, bg=SURFACE)
        self.bat_text.pack(side="left")
        self.charge_lbl = tk.Label(top, text="", font=(FONT, 9, "bold"), fg=GOOD, bg=SURFACE)
        self.charge_lbl.pack(side="right")
        self.bat_canvas = tk.Canvas(column, width=170, height=8, bg=SURFACE_3, highlightthickness=0, bd=0)
        self.bat_canvas.pack(fill="x", pady=(5, 0))
        self.pressed_lbl = tk.Label(column, text="Held: nothing", font=(FONT, 9), fg=TEXT, bg=SURFACE,
                                    justify="left", anchor="w", wraplength=170)
        self.pressed_lbl.pack(fill="x", pady=(14, 0))
        self.stick_lbl = tk.Label(column, text="Stick  2048, 2048", font=(MONO, 9), fg=MUTED, bg=SURFACE, anchor="w")
        self.stick_lbl.pack(fill="x", pady=(6, 0))
        self.click_lbl = tk.Label(column, text="", font=(FONT, 8), fg=WARN, bg=SURFACE,
                                  justify="left", anchor="w", wraplength=170)
        self.click_lbl.pack(fill="x", pady=(10, 0))

        motion = Card(column, bg=SURFACE, fill=SURFACE_2, pad=10, radius=14)
        motion.pack(fill="x", pady=(12, 0))
        mb = motion.body
        tk.Label(mb, text="Motion (deg/s)", font=(FONT, 8, "bold"), fg=MUTED, bg=SURFACE_2).pack(anchor="w")
        self.gyro_rows = {}
        for axis in ("X", "Y", "Z"):
            line = tk.Frame(mb, bg=SURFACE_2)
            line.pack(fill="x", pady=(4, 0))
            tk.Label(line, text=axis, width=2, anchor="w", font=(MONO, 8), fg=TEXT, bg=SURFACE_2).pack(side="left")
            bar = tk.Canvas(line, width=80, height=8, bg=SURFACE_3, highlightthickness=0, bd=0)
            bar.pack(side="left", fill="x", expand=True, padx=(4, 6))
            val = tk.Label(line, text="+0.0", width=6, anchor="e", font=(MONO, 8), fg=MUTED, bg=SURFACE_2)
            val.pack(side="left")
            self.gyro_rows[axis] = (bar, val)
        tk.Label(mb, text="Accel (g)", font=(FONT, 8, "bold"), fg=MUTED, bg=SURFACE_2).pack(anchor="w", pady=(8, 0))
        accel_row = tk.Frame(mb, bg=SURFACE_2)
        accel_row.pack(fill="x", pady=(2, 0))
        self.accel_lbls = []
        for _axis in ("X", "Y", "Z"):
            label = tk.Label(accel_row, text=f"{_axis} +0.00", font=(MONO, 7), fg=MUTED, bg=SURFACE_2)
            label.pack(side="left", padx=(0, 3))
            self.accel_lbls.append(label)

        colors = tk.Frame(body, bg=SURFACE)
        colors.pack(fill="x", pady=(14, 0))
        M3Button(colors, "Theme", self._cycle_theme, style="outlined", bg=SURFACE, compact=True).pack(
            side="left", padx=(0, 6))
        for text, attr in (("Body", "body_color"), ("Buttons", "btn_color"), ("Rail", "rail_color")):
            M3Button(colors, text, lambda a=attr, t=text: self._pick_color(a, t), style="text",
                     bg=SURFACE, compact=True).pack(side="left")
        M3Button(colors, "Test rumble", lambda: self.joycon.rumble_request(), style="filled",
                 bg=SURFACE, compact=True).pack(side="right")

    def _cycle_theme(self):
        self.joycon.apply_theme(self.joycon.theme_idx + 1)

    def _pick_color(self, attr: str, label: str):
        # Display-only: changes the on-screen preview, not the controller's stored colors.
        current = getattr(self.joycon, attr)
        chosen = colorchooser.askcolor(color=current, title=f"Pick {self.side} Joy-Con {label} color")
        if chosen and chosen[1]:
            setattr(self.joycon, attr, chosen[1])

    def refresh(self):
        s = self.joycon
        m = self.manager
        if s.connected:
            self.status_lbl.config(text="CONNECTED", fg=ON_GOOD_CONTAINER, bg=GOOD_CONTAINER)
        else:
            self.status_lbl.config(text="WAITING", fg=ON_SECONDARY_CONTAINER, bg=SECONDARY_CONTAINER)

        role = ""
        if s.connected:
            if m._is_mouse(s):
                role = "Mouse"
            elif m.sideways_single and m.active_side == self.side:
                role = "Sideways"
            elif m.mouse_enabled and m.auto_mode:
                role = "Controller"
        if role == "Controller":
            self.role_lbl.config(text=role, bg=SECONDARY_CONTAINER, fg=ON_SECONDARY_CONTAINER)
        else:
            self.role_lbl.config(text=role, bg=PRIMARY_CONTAINER if role else SURFACE, fg=ON_PRIMARY_CONTAINER)

        self.bat_canvas.delete("all")
        w = max(1, self.bat_canvas.winfo_width())
        if s.connected and s.battery_bars > 0:
            self.bat_text.config(text=f"Battery {s.battery_pct}%")
            fill = int(w * s.battery_pct / 100.0)
            color = GOOD if s.battery_pct > 40 else (WARN if s.battery_pct > 15 else BAD)
            self.bat_canvas.create_rectangle(0, 0, fill, 8, fill=color, width=0)
        elif s.connected:
            self.bat_text.config(text="Battery reading...")
        else:
            self.bat_text.config(text="Battery --")
        self.charge_lbl.config(text="Charging" if (s.connected and s.charging) else "")

        names = pressed_names(s)
        self.pressed_lbl.config(text="Held: " + (", ".join(names) if names else "nothing"))
        self.stick_lbl.config(text=f"Stick  {s.stick_x:4d}, {s.stick_y:4d}")
        hint = ()
        if m.mouse_enabled:
            hint = CLICK_BUTTONS[self.side]
            left_name, right_name = ("ZR", "R") if self.side == "R" else ("ZL", "L")
            self.click_lbl.config(text=f"Mouse: {left_name} = left click, {right_name} = right click")
        else:
            self.click_lbl.config(text="")
        draw_joycon(self.picture, s, hint)

        for axis, val in (("X", s.gyro_x), ("Y", s.gyro_y), ("Z", s.gyro_z)):
            bar, label = self.gyro_rows[axis]
            label.config(text=f"{val:+6.1f}")
            bar.delete("all")
            bw = max(1, bar.winfo_width())
            mid = bw // 2
            bar.create_line(mid, 0, mid, 8, fill=OUTLINE)
            if s.connected:
                norm = max(-1.0, min(1.0, val / 250.0))
                end = mid + int(norm * mid)
                bar.create_rectangle(min(mid, end), 1, max(mid, end), 7, fill=ACCENT, width=0)

        for axis, label, value in zip(("X", "Y", "Z"), self.accel_lbls, (s.accel_x, s.accel_y, s.accel_z)):
            label.config(text=f"{axis} {value:+.2f}")


# --- ViGEmBus driver: the virtual controller needs it ---------------------------

VIGEMBUS_API = "https://api.github.com/repos/nefarius/ViGEmBus/releases/latest"
VIGEMBUS_PAGE = "https://github.com/nefarius/ViGEmBus/releases/latest"
VIGEMBUS_PUBLISHER = "Nefarius"  # the installer must be signed by this publisher
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: keeps PowerShell from flashing a console


def vigembus_installed() -> bool:
    """True when the ViGEmBus driver service is registered on this PC."""
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\ViGEmBus")
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def vigembus_running() -> bool:
    """True when the ViGEmBus service is running, so the virtual controller can be created."""
    try:
        run = subprocess.run(
            ["sc", "query", "ViGEmBus"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    out = run.stdout or ""
    state = re.search(r"STATE\s*:\s*(\d+)", out)
    return "RUNNING" in out or (state is not None and state.group(1) == "4")


def find_vigembus_installer() -> tuple[str, str]:
    """(file name, download URL) of the installer in the latest official ViGEmBus release."""
    req = urllib.request.Request(
        VIGEMBUS_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Joy-Con-2-Studio"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        release = json.load(resp)
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.lower().startswith("vigembus") and name.lower().endswith(".exe"):
            return name, asset["browser_download_url"]
    raise RuntimeError("the latest ViGEmBus release has no installer")


def signature_ok(path: str, publisher: str) -> tuple[bool, str]:
    """Checks a file's Authenticode signature. Returns (trusted, name of the signer)."""
    quoted = path.replace("'", "''")
    script = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{quoted}'; "
        "$subject = [string]$s.SignerCertificate.Subject; "
        # ASCII only, so the output reads the same whatever language the PC uses
        "[pscustomobject]@{Status = [string]$s.Status; Subject = ($subject -replace '[^\\x20-\\x7E]', '?')} "
        "| ConvertTo-Json -Compress"
    )
    run = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=NO_WINDOW,
    )
    info = json.loads((run.stdout or "").strip() or "{}")
    subject = info.get("Subject") or ""
    return info.get("Status") == "Valid" and publisher.lower() in subject.lower(), subject


def download_file(url: str, dest: str, progress) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Joy-Con-2-Studio"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(64 * 1024):
            out.write(chunk)
            done += len(chunk)
            if total:
                progress(done * 100 // total)


def run_as_admin(path: str) -> None:
    """Runs the installer elevated. Windows shows the UAC prompt first; raises if it is declined or fails."""
    quoted = path.replace("'", "''")
    # /norestart: the driver install must never restart the PC by itself
    script = f"Start-Process -FilePath '{quoted}' -ArgumentList '/norestart' -Verb RunAs -Wait"
    run = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    if run.returncode != 0:
        raise RuntimeError("the administrator prompt was declined or the installer failed")


def install_vigembus_driver(log, progress) -> bool:
    """Downloads ViGEmBus from its official GitHub release, checks the signature and runs it as administrator.
    Returns True once the driver is registered."""
    name, url = find_vigembus_installer()
    workdir = tempfile.mkdtemp(prefix="JoyCon2Studio-")
    try:
        path = os.path.join(workdir, name)
        log(f"Downloading {name} from the ViGEmBus GitHub release...")
        download_file(url, path, progress)
        trusted, signer = signature_ok(path, VIGEMBUS_PUBLISHER)
        if not trusted:
            raise RuntimeError(f"the installer is not signed by {VIGEMBUS_PUBLISHER} (signer: {signer or 'none'})")
        log("Signature checked. Windows will ask for permission to install the driver.")
        run_as_admin(path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return vigembus_installed()


# --- Update check: asks GitHub for the newest release ---------------------------

APP_VERSION = "0.2.16"
UPDATE_API = "https://api.github.com/repos/lexluther1307-sudo/Joy-con-2-Studio/releases/latest"
RELEASES_PAGE = "https://github.com/lexluther1307-sudo/Joy-con-2-Studio/releases/latest"


def version_tuple(text: str):
    """'v0.1.2' -> (0, 1, 2). None if the text is not a three-part version."""
    parts = text.strip().lstrip("vV").split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)
    return None


UPDATE_DOWNLOAD_PREFIX = "https://github.com/lexluther1307-sudo/Joy-con-2-Studio/releases/download/"
UPDATE_EXE_NAME = "JoyCon2Studio.exe"
UPDATE_SUMS_NAME = "SHA256SUMS.txt"


def can_install_in_app() -> bool:
    """The in-app install replaces the running exe, so it is offered only for the built app on Windows."""
    return IS_WINDOWS and bool(getattr(sys, "frozen", False))


def current_exe() -> str:
    return os.path.abspath(sys.executable)


def check_for_update() -> tuple:
    """(a newer release exists, its tag, its page URL, (exe address, checksum address) or None).
    Raises if GitHub cannot be reached."""
    req = urllib.request.Request(
        UPDATE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Joy-Con-2-Studio"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        release = json.load(resp)
    latest = release.get("tag_name", "")
    latest_v = version_tuple(latest)
    newer = latest_v is not None and latest_v > version_tuple(APP_VERSION)
    return newer, latest, release.get("html_url") or RELEASES_PAGE, update_assets(release)


def update_assets(release: dict):
    """(exe address, checksum address) of a release, or None when it lacks either file.
    Only addresses on the project's own GitHub release downloads are accepted."""
    assets = {a.get("name"): a.get("browser_download_url", "") for a in release.get("assets", [])}
    exe, sums = assets.get(UPDATE_EXE_NAME, ""), assets.get(UPDATE_SUMS_NAME, "")
    if not exe or not sums:
        return None
    if not (exe.startswith(UPDATE_DOWNLOAD_PREFIX) and sums.startswith(UPDATE_DOWNLOAD_PREFIX)):
        return None
    return exe, sums


def download_update(url: str, dest: str, progress=None) -> None:
    """Saves the file at url to dest, reporting the fraction done. Stops if it grows past 80 MB."""
    req = urllib.request.Request(url, headers={"User-Agent": "Joy-Con-2-Studio"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            done += len(chunk)
            if done > 80 * 1024 * 1024:
                raise RuntimeError("the download is larger than expected")
            out.write(chunk)
            if progress and total:
                progress(done / total)


def verify_download(path: str, sums_text: str) -> bool:
    """True when the file's SHA-256 is the one that SHA256SUMS.txt lists for JoyCon2Studio.exe."""
    expected = None
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == UPDATE_EXE_NAME:
            expected = parts[0].lower()
    if expected is None:
        return False
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


def update_helper_script(current: str, new: str, previous: str, pid: int, log: str) -> str:
    """A batch file that waits for the running app to close, puts the new exe in its place, starts it and
    deletes itself. The old exe is kept as the .previous copy."""
    lines = [
        "@echo off",
        f'set "CURRENT={current}"',
        f'set "NEW={new}"',
        f'set "PREVIOUS={previous}"',
        f'set "LOG={log}"',
        f"set PID={pid}",
        ":wait",
        'tasklist /FI "PID eq %PID%" /NH 2>nul | find "%PID%" >nul',
        "if not errorlevel 1 (ping -n 2 127.0.0.1 >nul & goto wait)",
        "set TRIES=0",
        ":swap",
        'copy /Y "%CURRENT%" "%PREVIOUS%" >nul 2>nul',
        'move /Y "%NEW%" "%CURRENT%" >nul 2>nul',
        "if not errorlevel 1 goto started",
        "set /a TRIES+=1",
        "if %TRIES% lss 30 (ping -n 2 127.0.0.1 >nul & goto swap)",
        'echo %date% %time% could not replace the app >> "%LOG%"',
        "exit /b 1",
        ":started",
        'echo %date% %time% installed the update, starting it >> "%LOG%"',
        'start "" "%CURRENT%"',
        '(goto) 2>nul & del "%~f0" >nul 2>nul',
    ]
    return "\r\n".join(lines) + "\r\n"


# --- Keeping the app running: a tray icon, and a second copy that shows the running window ------------

SHOW_PORT = int(os.environ.get("JOYCON2STUDIO_SHOW_PORT", "26790"))  # a second copy connects here and asks the running copy to show; the variable is for tests
TRAY_CLASS = "JoyCon2StudioTray"
WM_APP = 0x8000
WM_TRAY = WM_APP + 1  # the tray icon sends this to our hidden window when it is clicked
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING = 0x0000
TPM_RIGHTBUTTON = 0x0002
IDM_SHOW, IDM_QUIT = 1001, 1002
_WNDPROC = (ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
            if IS_WINDOWS else ctypes.c_void_p)  # a placeholder type on Linux, where the tray is not used


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("style", ctypes.c_uint), ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", ctypes.c_void_p),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p), ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint), ("hIcon", ctypes.c_void_p), ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint), ("dwStateMask", ctypes.c_uint), ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", ctypes.c_uint), ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", ctypes.c_void_p),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint), ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t), ("time", ctypes.c_uint), ("pt", _POINT)]


class TrayIcon:
    """An icon in the notification area with Show and Quit. A hidden window on its own thread receives its
    clicks and passes "show" or "quit" to the app through a queue."""

    def __init__(self, tip: str, events: queue.Queue):
        self.tip = tip
        self.events = events
        self._hwnd = None
        self._user32 = None
        self._shell32 = None
        self._data = None
        self._proc = _WNDPROC(self._on_message)  # kept alive for as long as the window exists
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(5)

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._user32, self._shell32 = user32, shell32
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.LoadIconW.restype = ctypes.c_void_p
        user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        shell32.ExtractIconW.restype = ctypes.c_void_p
        shell32.ExtractIconW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.CreatePopupMenu.restype = ctypes.c_void_p
        user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
        user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        user32.DestroyMenu.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]

        instance = kernel32.GetModuleHandleW(None)
        window_class = _WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(window_class)
        window_class.lpfnWndProc = self._proc
        window_class.hInstance = instance
        window_class.lpszClassName = TRAY_CLASS
        user32.RegisterClassExW(ctypes.byref(window_class))
        self._hwnd = user32.CreateWindowExW(0, TRAY_CLASS, TRAY_CLASS, 0, 0, 0, 0, 0, None, None, instance, None)

        icon = shell32.ExtractIconW(None, sys.executable, 0) or user32.LoadIconW(None, ctypes.c_void_p(32512))
        self._data = _NOTIFYICONDATAW()
        self._data.cbSize = ctypes.sizeof(self._data)
        self._data.hWnd = self._hwnd
        self._data.uID = 1
        self._data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self._data.uCallbackMessage = WM_TRAY
        self._data.hIcon = icon
        self._data.szTip = self.tip[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data))
        self._ready.set()

        message = _MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == WM_TRAY:
            event = int(lparam) & 0xFFFF
            if event == WM_LBUTTONUP:
                self.events.put("show")
            elif event == WM_RBUTTONUP:
                self._show_menu(hwnd)
            return 0
        if message == WM_COMMAND:
            command = int(wparam) & 0xFFFF
            if command == IDM_SHOW:
                self.events.put("show")
            elif command == IDM_QUIT:
                self.events.put("quit")
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self, hwnd):
        user32 = self._user32
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, IDM_SHOW, "Show")
        user32.AppendMenuW(menu, MF_STRING, IDM_QUIT, "Quit")
        point = _POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, point.x, point.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, 0, 0, 0)  # lets the menu close when the mouse is clicked elsewhere
        user32.DestroyMenu(menu)

    def remove(self):
        """Takes the icon out of the notification area."""
        if self._data is not None:
            self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data))


def apply_glass(window) -> bool:
    """Windows 11 acrylic behind the window, with GLASS_KEY made see-through. False if Windows refuses it."""
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())

        class Margins(ctypes.Structure):
            _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]

        dwm = ctypes.windll.dwmapi
        if dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(Margins(-1, -1, -1, -1))) != 0:
            return False
        acrylic = ctypes.c_int(3)  # DWMWA_SYSTEM_BACKDROP_TYPE (38): 3 = acrylic
        if dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(acrylic), ctypes.sizeof(acrylic)) != 0:
            return False
        window.wm_attributes("-transparentcolor", GLASS_KEY)
        return True
    except (OSError, AttributeError, tk.TclError):
        return False


def start_show_server(on_show) -> bool:
    """Lets a second copy of the app show this window instead of starting another copy."""
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", SHOW_PORT))
        server.listen(4)
    except OSError:
        return False

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                try:
                    if conn.recv(16).startswith(b"show"):
                        on_show()
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return True


def ask_running_copy_to_show() -> bool:
    """True when another copy of the app is running, and it has been asked to show its window."""
    try:
        with socket.create_connection(("127.0.0.1", SHOW_PORT), timeout=1.0) as conn:
            conn.sendall(b"show")
        return True
    except OSError:
        return False


class JoyCon2App(tk.Tk):
    def __init__(self):
        super().__init__()
        self._status_queue = queue.Queue()
        self.title("Joy-Con 2 Studio")
        self.geometry("1080x740")
        self.minsize(1000, 700)
        self.configure(bg=BG)
        if GLASS and not apply_glass(self):
            self.configure(bg="#141218")  # Windows refused the backdrop: the window keeps a solid background

        self.status_var = tk.StringVar(value="Hold SYNC on one or two Joy-Cons, then click Scan.")
        self.manager = JoyCon2Manager(self._set_status)

        self._build_header()
        self._build_body()
        self._build_snackbar()
        self._refresh_virtual_pill()
        self._events = queue.Queue()
        self._quitting = False
        self._tray = None
        if IS_WINDOWS:
            try:
                self._tray = TrayIcon("Joy-Con 2 Studio", self._events)
                if self._tray._hwnd is None:
                    self._tray = None
            except Exception:
                self._tray = None
        if self._tray is not None or IS_LINUX:
            # The close button hides the window and keeps the app running. Linux has no tray icon, so start the
            # app again to show the window.
            self.protocol("WM_DELETE_WINDOW", self._close_to_tray)
        else:
            self.protocol("WM_DELETE_WINDOW", self._quit)  # no tray icon: closing quits, as before
        start_show_server(lambda: self._events.put("show"))
        self.after(1000, lambda: self._start_update_check(manual=False))  # once the window is up
        self._tick()
        if self.auto_scan_var.get():
            self.manager.start_scan(12.0)

    def _set_status(self, msg: str):
        # Safe from the link and Bluetooth threads: the window picks the text up in _tick
        self._status_queue.put(msg)

    def _drain_status(self):
        while True:
            try:
                msg = self._status_queue.get_nowait()
            except queue.Empty:
                return
            self.status_var.set(msg)

    def _pill(self, text, bg, fg):
        if getattr(self, "_last_pill", None) == (text, bg, fg):
            return  # the refresh runs often, so the label is only changed when its text changes
        self._last_pill = (text, bg, fg)
        self.virtual_pill.config(text=text, bg=bg, fg=fg)

    def _refresh_virtual_pill(self):
        if IS_LINUX:
            if self.manager.deck_connected():
                self._pill("Virtual controller: off (Deck connected)", SECONDARY_CONTAINER, ON_SECONDARY_CONTAINER)
            elif self.manager.uhid_pad is not None:
                self._pill("Virtual controller: DualShock 4 READY", GOOD_CONTAINER, ON_GOOD_CONTAINER)
            elif self.manager.local_pad_message:
                self._pill(f"Virtual controller: {self.manager.local_pad_message}", ERROR_CONTAINER, ON_ERROR_CONTAINER)
            else:
                self._pill("Virtual controller: starting", WARN_CONTAINER, ON_WARN_CONTAINER)
            return
        if HAS_VGAMEPAD and vigembus_installed():
            if self.manager.deck_connected():
                # The Deck has the controller, so this PC does not use it
                self._pill("Virtual controller: off (Deck connected)", SECONDARY_CONTAINER, ON_SECONDARY_CONTAINER)
            elif vigembus_running():
                self._pill("Virtual controller: READY", GOOD_CONTAINER, ON_GOOD_CONTAINER)
            else:
                # Installed but not active yet. A restart finishes it, but nothing requires one.
                self._pill("Virtual controller: restart suggested", WARN_CONTAINER, ON_WARN_CONTAINER)
            self.install_btn.pack_forget()
        else:
            self._pill("Virtual controller: MISSING ViGEmBus", ERROR_CONTAINER, ON_ERROR_CONTAINER)
            if HAS_VGAMEPAD:
                self.install_btn.pack(side="left", padx=(8, 0))
            else:
                self.install_btn.pack_forget()

    def _install_vigembus(self):
        self.install_btn.config(state="disabled", text="Installing...")

        def worker():
            try:
                ok = install_vigembus_driver(
                    self._set_status,
                    lambda pct: self._set_status(f"Downloading the ViGEmBus driver... {pct}%"),
                )
                if not ok:
                    msg = "The installer finished, but the driver is not registered. Try again. A restart may help."
                elif vigembus_running():
                    msg = "ViGEmBus driver installed and running. The virtual controller is ready."
                else:
                    # Suggested, never required: the Joy-Cons still work without the virtual controller
                    msg = (
                        "ViGEmBus is installed but not running yet. Restarting the PC usually finishes it. "
                        "It is optional; the Joy-Cons work meanwhile."
                    )
            except Exception as e:
                ok = False
                msg = f"ViGEmBus install failed: {e}. You can install it by hand from {VIGEMBUS_PAGE}"
            self.after(0, lambda: self._install_finished(ok, msg))

        threading.Thread(target=worker, daemon=True).start()

    def _install_finished(self, ok: bool, msg: str):
        self.install_btn.config(state="normal", text="Install ViGEmBus driver")
        if ok:
            self.manager._ds4_failed = False  # the virtual pad can be created now
        self._set_status(msg)
        self._refresh_virtual_pill()

    def _start_update_check(self, manual: bool):
        # Runs at startup (quiet unless a newer release exists) and from the Check for updates button.
        # The setting "check_updates" switches the check at startup off; the button still checks.
        if not manual and not load_settings().get("check_updates", True):
            return

        def worker():
            try:
                newer, latest, url, assets = check_for_update()
            except Exception as e:
                if manual:
                    self._set_status(f"Could not check for updates: {e}")
                return
            if not newer:
                if manual:
                    self._set_status(f"You have the latest version ({APP_VERSION}).")
                return
            self.after(0, lambda: self._update_found(latest, url, assets))
            # Installing by itself happens at startup, before the Joy-Cons are in use, and only if the setting asks
            if not manual and assets and can_install_in_app() and load_settings().get("install_updates", False):
                self.after(0, lambda: self._install_update(latest, assets))

        threading.Thread(target=worker, daemon=True).start()

    def _update_found(self, latest: str, url: str, assets):
        if assets and can_install_in_app():
            self.update_btn.config(text=f"Install {latest}",
                                   command=lambda: self._confirm_install(latest, assets))
            self._set_status(f"Version {latest} is available. Press Install {latest} to update the app.")
        else:
            self.update_btn.config(text=f"Get {latest}", command=lambda: webbrowser.open(url))
            self._set_status(f"Update available: {latest} (you have {APP_VERSION}). Use the Get {latest} button.")

    def _confirm_install(self, latest: str, assets):
        if messagebox.askyesno(
            "Install update",
            f"Install version {latest} now?\n\nThe app downloads it, checks the download, and restarts. "
            "The Joy-Cons disconnect while it restarts, so press SYNC on them again once it is running.",
            parent=self,
        ):
            self._install_update(latest, assets)

    def _install_update(self, latest: str, assets):
        exe_url, sums_url = assets
        target = current_exe()
        folder = os.path.dirname(target)
        new_path = os.path.join(folder, "JoyCon2Studio.new.exe")
        previous_path = os.path.join(folder, "JoyCon2Studio.previous.exe")
        self.update_btn.config(text="Installing...", command=lambda: None)

        def worker():
            try:
                self._set_status(f"Downloading version {latest}...")
                download_update(exe_url, new_path, progress=lambda f: self._set_status(
                    f"Downloading version {latest}... {round(f * 100)}%"))
                with urllib.request.urlopen(sums_url, timeout=30) as resp:
                    sums = resp.read(64 * 1024).decode("utf-8", "replace")
                if not verify_download(new_path, sums):
                    raise RuntimeError("the download does not match SHA256SUMS.txt")
                temp = tempfile.gettempdir()
                script = os.path.join(temp, "joycon2studio-update.cmd")
                log = os.path.join(temp, "joycon2studio-update.log")
                with open(script, "w", encoding="mbcs", newline="") as handle:
                    handle.write(update_helper_script(target, new_path, previous_path, os.getpid(), log))
                # The helper runs on its own after this app closes, so it keeps going when the app has quit.
                # Not DETACHED_PROCESS: with it the helper never ran (tested). CREATE_NO_WINDOW alone works.
                subprocess.Popen(["cmd.exe", "/c", script], creationflags=0x08000000 | 0x00000200, close_fds=True)
            except Exception as e:
                try:
                    os.remove(new_path)
                except OSError:
                    pass
                self.after(0, lambda: self.update_btn.config(
                    text=f"Install {latest}", command=lambda: self._confirm_install(latest, assets)))
                self._set_status(f"Update to {latest} failed: {e}. The installed version was kept.")
                return
            self._set_status(f"Installing version {latest}. The app restarts now.")
            self.after(0, self._quit)

        threading.Thread(target=worker, daemon=True).start()

    # ---- layout ---------------------------------------------------------------------

    def _build_header(self):
        head = tk.Frame(self, bg=BG)
        head.pack(side="top", fill="x", padx=24, pady=(18, 6))
        tk.Label(head, text="Joy-Con 2 Studio", font=(FONT, 18, "bold"), fg=TEXT, bg=BG).pack(side="left")
        tk.Label(head, text=f"v{APP_VERSION}", font=(FONT, 10), fg=MUTED, bg=BG).pack(
            side="left", padx=(10, 0), pady=(6, 0))
        self.deck_chip = make_chip(head, "Deck: off", SECONDARY_CONTAINER, ON_SECONDARY_CONTAINER)
        self.deck_chip.pack(side="right", padx=(8, 0))
        self.virtual_pill = make_chip(head, "", WARN_CONTAINER, ON_WARN_CONTAINER)
        self.virtual_pill.pack(side="right")

    def _build_body(self):
        row = tk.Frame(self, bg=BG)
        row.pack(side="top", fill="both", expand=True, padx=16, pady=(8, 8))
        rail = tk.Frame(row, bg=BG)
        rail.pack(side="left", fill="y", padx=(8, 8), pady=(6, 0))
        self.pages = {}
        self.nav = {}
        for name, text in (("controllers", "Controllers"), ("deck", "Steam Deck"), ("settings", "Settings")):
            item = NavItem(rail, text, lambda n=name: self._show_page(n))
            item.pack(anchor="n", pady=2)
            self.nav[name] = item
        self.page_area = tk.Frame(row, bg=BG)
        self.page_area.pack(side="left", fill="both", expand=True, padx=(8, 8))
        self._build_controllers_page()
        self._build_deck_page()
        self._build_settings_page()
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(sequence, self._on_wheel)  # one handler scrolls whichever page the pointer is over
        self._show_page("controllers")
        if self.bridge_var.get():
            self._toggle_bridge()  # connects with the saved address and code

    def _on_wheel(self, event):
        """Scrolls the ScrollPage under the pointer: the Windows wheel, or buttons 4 and 5 on Linux."""
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        while widget is not None and not isinstance(widget, ScrollPage):
            widget = widget.master
        if widget is None:
            return
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        widget.canvas.yview_scroll(step, "units")

    def _show_page(self, name: str):
        for key, page in self.pages.items():
            if key == name:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        for key, item in self.nav.items():
            item.select(key == name)

    def _build_snackbar(self):
        self.snack = Snackbar(self)
        self.snack.pack(side="bottom", fill="x", padx=24, pady=(0, 16))
        self.status_var.trace_add("write", lambda *_: self.snack.set_text(self.status_var.get()))
        self.snack.set_text(self.status_var.get())

    def _build_controllers_page(self):
        page = tk.Frame(self.page_area, bg=BG)
        self.pages["controllers"] = page
        actions = tk.Frame(page, bg=BG)
        actions.pack(fill="x", pady=(4, 14))
        M3Button(actions, "Scan for Joy-Cons", lambda: self.manager.start_scan(12.0),
                 style="filled", bg=BG).pack(side="left")
        M3Button(actions, "Zero sticks & gyro", self._calibrate_all, style="tonal", bg=BG).pack(side="left", padx=8)
        M3Button(actions, "Disconnect all", self.manager.disconnect_all, style="danger", bg=BG).pack(side="left")
        if IS_WINDOWS:
            self.install_btn = M3Button(actions, "Install ViGEmBus driver", self._install_vigembus,
                                        style="tonal", bg=BG)
        cards = tk.Frame(page, bg=BG)
        cards.pack(fill="both", expand=True)
        cards.grid_columnconfigure(0, weight=1, uniform="cards")
        cards.grid_columnconfigure(1, weight=1, uniform="cards")
        self.left_card = JoyConCard(cards, "Left Joy-Con 2", self.manager.left, self.manager)
        self.left_card.grid(row=0, column=0, sticky="new", padx=(0, 8))
        self.right_card = JoyConCard(cards, "Right Joy-Con 2", self.manager.right, self.manager)
        self.right_card.grid(row=0, column=1, sticky="new", padx=(8, 0))

    def _switch_row(self, parent, text, var, command):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=(8, 0))
        M3Switch(row, var, command=command, bg=SURFACE).pack(side="left")
        tk.Label(row, text=text, font=(FONT, 10), fg=TEXT, bg=SURFACE, anchor="w", justify="left",
                 wraplength=330).pack(side="left", padx=(12, 0))
        return row

    def _build_deck_page(self):
        page = ScrollPage(self.page_area, bg=BG)
        self.pages["deck"] = page
        bridge = load_settings()
        self.bridge_var = tk.BooleanVar(value=bool(bridge.get("bridge_on", False)))
        self.bridge_ip_var = tk.StringVar(value=bridge.get("bridge_ip", ""))
        self.deck_code_var = tk.StringVar(value=bridge.get("deck_code", ""))

        card = Card(page.body, bg=BG, fill=SURFACE)
        card.pack(fill="x", anchor="n")
        b = card.body
        tk.Label(b, text="Use this PC's Joy-Cons on a Steam Deck", font=(FONT, 14, "bold"),
                 fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(
            b,
            text="The Joy-Cons stay on this PC. The Deck shows them to Steam as a controller, "
                 "over an encrypted Wi-Fi link.",
            font=(FONT, 10), fg=MUTED, bg=SURFACE, justify="left", wraplength=760,
        ).pack(anchor="w", pady=(4, 10))
        self._switch_row(b, "Connect to the Deck", self.bridge_var, self._toggle_bridge)
        fields = tk.Frame(b, bg=SURFACE)
        fields.pack(anchor="w", pady=(14, 0))
        ip_field = M3Field(fields, "Deck IP address", self.bridge_ip_var,
                           helper="Shown on the Deck in Joy-Con 2 Studio.", width=260, bg=SURFACE)
        ip_field.pack(side="left", anchor="n")
        code_field = M3Field(fields, "Pairing code", self.deck_code_var,
                             helper="Six digits, under Pairing on the Deck. It changes after every disconnect.",
                             width=260, bg=SURFACE)
        code_field.pack(side="left", anchor="n", padx=(18, 0))
        ip_field.entry.bind("<Return>", lambda _e: self._toggle_bridge())
        code_field.entry.bind("<Return>", lambda _e: self._toggle_bridge())
        buttons = tk.Frame(b, bg=SURFACE)
        buttons.pack(anchor="w", pady=(18, 0))
        M3Button(buttons, "Connect", self._connect_deck, style="filled", bg=SURFACE).pack(side="left")
        M3Button(buttons, "Disconnect", self._disconnect_deck, style="outlined", bg=SURFACE).pack(
            side="left", padx=8)
        self.link_lbl = tk.Label(buttons, text="Deck: off", font=(FONT, 10), fg=MUTED, bg=SURFACE)
        self.link_lbl.pack(side="left", padx=(12, 0))

        steps = Card(page.body, bg=BG, fill=SURFACE)
        steps.pack(fill="x", anchor="n", pady=(14, 0))
        sb = steps.body
        tk.Label(sb, text="How to connect", font=(FONT, 12, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
        for line in (
            "1. On the Deck, open Joy-Con 2 Studio from Quick Access and note the pairing code.",
            "2. Enter the Deck's IP address and the pairing code above, then press Connect.",
            "3. Steam on the Deck shows the controller. Disconnect from either side at any time.",
            "4. The code changes after every disconnect, so read the new code before you connect again.",
        ):
            tk.Label(sb, text=line, font=(FONT, 10), fg=MUTED, bg=SURFACE, justify="left",
                     wraplength=760).pack(anchor="w", pady=(6, 0))

    def _build_settings_page(self):
        page = ScrollPage(self.page_area, bg=BG)
        self.pages["settings"] = page
        cols = tk.Frame(page.body, bg=BG)
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=1, uniform="cols")
        cols.grid_columnconfigure(1, weight=1, uniform="cols")
        left = tk.Frame(cols, bg=BG)
        left.grid(row=0, column=0, sticky="new", padx=(0, 8))
        right = tk.Frame(cols, bg=BG)
        right.grid(row=0, column=1, sticky="new", padx=(8, 0))

        # Left column: how the Joy-Cons behave
        self.sideways_var = tk.BooleanVar(value=False)
        self.gyro_aim_var = tk.BooleanVar(value=False)
        self.mouse_var = tk.BooleanVar(value=False)
        self.auto_scan_var = tk.BooleanVar(value=load_settings().get("auto_scan", True))
        self.which_var = tk.StringVar(value="Auto")

        options = Card(left, bg=BG, fill=SURFACE)
        options.pack(fill="x")
        ob = options.body
        tk.Label(ob, text="Joy-Con options", font=(FONT, 12, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(ob, text="Which Joy-Con is used", font=(FONT, 9), fg=MUTED, bg=SURFACE).pack(
            anchor="w", pady=(10, 0))
        M3Segmented(ob, ["Auto", "Left", "Right"], self.which_var, command=self._set_which,
                    width=300, bg=SURFACE).pack(anchor="w", pady=(4, 0))
        tk.Label(ob, text="Auto follows whichever Joy-Con you pick up.", font=(FONT, 8), fg=MUTED,
                 bg=SURFACE).pack(anchor="w", pady=(4, 0))
        self._switch_row(ob, "Use a Joy-Con as a mouse", self.mouse_var, self._toggle_options)
        tk.Label(ob, text="Right Joy-Con: ZR left click, R right click.\n"
                          "Left Joy-Con: ZL left click, L right click.",
                 font=(FONT, 8), fg=MUTED, bg=SURFACE, justify="left").pack(anchor="w", padx=(64, 0), pady=(2, 0))
        self.auto_mode_var = tk.BooleanVar(value=load_settings().get("auto_mode", True))
        self._switch_row(ob, "Switch between mouse and controller automatically", self.auto_mode_var,
                         self._toggle_auto_mode)
        tk.Label(ob, text="Slide a Joy-Con on a surface to use it as a mouse. Press its sticks or buttons "
                          "to use it as a controller again.",
                 font=(FONT, 8), fg=MUTED, bg=SURFACE, justify="left", wraplength=300, anchor="w").pack(
            anchor="w", padx=(64, 0), pady=(2, 0))
        speed = tk.Frame(ob, bg=SURFACE)
        speed.pack(anchor="w", pady=(10, 0))
        tk.Label(speed, text="Mouse speed", font=(FONT, 10), fg=TEXT, bg=SURFACE).pack(side="left")
        self.speed_scale = M3Slider(speed, 0.05, 1.5, 0.3, command=self._set_speed, step=0.05,
                                    width=180, bg=SURFACE)
        self.speed_scale.pack(side="left", padx=(12, 0))
        self.mouse_raw_lbl = tk.Label(ob, text="raw  X 0  Y 0", font=(MONO, 9), fg=MUTED, bg=SURFACE)
        self.mouse_raw_lbl.pack(anchor="w", pady=(8, 0))
        self._switch_row(ob, "Use one Joy-Con held sideways, on its own", self.sideways_var, self._toggle_options)
        self._switch_row(ob, "Gyro aims the right stick", self.gyro_aim_var, self._toggle_options)

        startup = Card(left, bg=BG, fill=SURFACE)
        startup.pack(fill="x", pady=(14, 0))
        tk.Label(startup.body, text="Startup", font=(FONT, 12, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
        self._switch_row(startup.body, "Scan for Joy-Cons when the app opens", self.auto_scan_var,
                         self._toggle_auto_scan)
        tk.Label(startup.body, text="Hold SYNC on each Joy-Con, then open the app.",
                 font=(FONT, 8), fg=MUTED, bg=SURFACE, justify="left", wraplength=300).pack(
            anchor="w", padx=(64, 0), pady=(2, 0))

        # Right column: output, sound and about
        output = Card(right, bg=BG, fill=SURFACE)
        output.pack(fill="x")
        ob2 = output.body
        tk.Label(ob2, text="Virtual controller output", font=(FONT, 12, "bold"), fg=TEXT,
                 bg=SURFACE).pack(anchor="w")
        if IS_LINUX:
            tk.Label(ob2, text="Steam and games see this PC's controller as a DualShock 4, through /dev/uhid.",
                     font=(FONT, 9), fg=MUTED, bg=SURFACE, justify="left", wraplength=400).pack(anchor="w", pady=(2, 10))
            self.output_var = tk.StringVar(value=OUTPUT_LABELS["ds4"])
        else:
            tk.Label(ob2, text="DualShock 4 is what Steam sees. Xbox 360 suits games that expect it.",
                     font=(FONT, 9), fg=MUTED, bg=SURFACE, justify="left", wraplength=400).pack(anchor="w", pady=(2, 10))
            self.output_var = tk.StringVar(value=OUTPUT_LABELS[self.manager.output])
            M3Segmented(ob2, list(OUTPUT_LABELS.values()), self.output_var, command=self._set_output,
                        width=400, bg=SURFACE).pack(anchor="w")

        if GLASS_AVAILABLE:
            look = Card(right, bg=BG, fill=SURFACE)
            look.pack(fill="x", pady=(14, 0))
            lb = look.body
            tk.Label(lb, text="Look", font=(FONT, 12, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
            self.glass_var = tk.BooleanVar(value=load_settings().get("glass", False))
            self._switch_row(lb, "Glass look (Windows 11)", self.glass_var, self._toggle_glass)
            tk.Label(lb, text="The window and the cards become see-through, with Windows' acrylic behind them. "
                              "Takes effect after you restart the app.",
                     font=(FONT, 8), fg=MUTED, bg=SURFACE, justify="left", wraplength=400).pack(anchor="w", pady=(4, 0))

        sound = Card(right, bg=BG, fill=SURFACE)
        sound.pack(fill="x", pady=(14, 0))
        sb = sound.body
        tk.Label(sb, text="Sound", font=(FONT, 12, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
        self.sample_var = tk.StringVar(value=VIBRATION_SAMPLES[1][0])
        tk.Label(sb, text="Rumble test", font=(FONT, 9), fg=MUTED, bg=SURFACE).pack(anchor="w", pady=(10, 0))
        M3Menu(sb, [name for name, _ in VIBRATION_SAMPLES[1:]], self.sample_var, command=self._set_sample,
               width=400, bg=SURFACE).pack(anchor="w", pady=(4, 0))
        self.song_var = tk.StringVar(value=SONGS[0][0])
        tk.Label(sb, text="Song", font=(FONT, 9), fg=MUTED, bg=SURFACE).pack(anchor="w", pady=(10, 0))
        M3Menu(sb, [name for name, _ in SONGS], self.song_var, width=400, bg=SURFACE).pack(anchor="w", pady=(4, 0))
        row = tk.Frame(sb, bg=SURFACE)
        row.pack(anchor="w", pady=(14, 0))
        M3Button(row, "Play song", lambda: self.manager.play_song(self.song_var.get()),
                 style="filled", bg=SURFACE).pack(side="left")
        M3Button(row, "Stop", self.manager.stop_vibration, style="tonal", bg=SURFACE).pack(side="left", padx=8)

        about = Card(right, bg=BG, fill=SURFACE)
        about.pack(fill="x", pady=(14, 0))
        ab = about.body
        tk.Label(ab, text="About", font=(FONT, 12, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(ab, text=f"Joy-Con 2 Studio {APP_VERSION}", font=(FONT, 10), fg=MUTED,
                 bg=SURFACE).pack(anchor="w", pady=(2, 10))
        self.update_btn = M3Button(ab, "Check for updates", lambda: self._start_update_check(manual=True),
                                   style="outlined", bg=SURFACE)
        self.update_btn.pack(anchor="w")
        self.check_updates_var = tk.BooleanVar(value=load_settings().get("check_updates", True))
        self._switch_row(ab, "Check for updates when the app starts", self.check_updates_var,
                         self._toggle_check_updates)
        if can_install_in_app():
            self.install_updates_var = tk.BooleanVar(value=load_settings().get("install_updates", False))
            self._switch_row(ab, "Install updates by itself when the app starts", self.install_updates_var,
                             self._toggle_install_updates)
        if IS_WINDOWS:
            about_text = ("Closing the window keeps the app running in the notification area. "
                          "Right-click its icon to quit.")
        else:
            about_text = ("Closing the window keeps the app running in the background, so the Joy-Cons stay connected. "
                          "Start Joy-Con 2 Studio again to show the window. Quit stops the app.")
        tk.Label(ab, text=about_text, font=(FONT, 8), fg=MUTED, bg=SURFACE, justify="left",
                 wraplength=380).pack(anchor="w", pady=(10, 0))
        M3Button(ab, "Quit", self._quit, style="danger", bg=SURFACE).pack(anchor="w", pady=(8, 0))

    # ---- actions --------------------------------------------------------------------

    def _toggle_options(self):
        self.manager.sideways_single = self.sideways_var.get()
        self.manager.gyro_aim_enabled = self.gyro_aim_var.get()
        self.manager.mouse_enabled = self.mouse_var.get()
        if not self.manager.mouse_enabled:
            self.manager.release_mouse()
        self.manager.set_active_choice(self.manager.active_choice)  # re-checks which Joy-Con is in use
        self.manager._sync_virtual_gamepad()

    def _toggle_check_updates(self):
        settings = load_settings()
        settings["check_updates"] = self.check_updates_var.get()
        save_settings(settings)

    def _toggle_install_updates(self):
        settings = load_settings()
        settings["install_updates"] = self.install_updates_var.get()
        save_settings(settings)

    def _toggle_glass(self):
        settings = load_settings()
        settings["glass"] = self.glass_var.get()
        save_settings(settings)
        self._set_status("The glass look takes effect after you restart the app.")

    def _toggle_auto_mode(self):
        self.manager.set_auto_mode(self.auto_mode_var.get())
        settings = load_settings()
        settings["auto_mode"] = self.auto_mode_var.get()
        save_settings(settings)

    def _set_which(self, label):
        self.manager.set_active_choice({"Auto": "auto", "Left": "L", "Right": "R"}[label])

    def _toggle_bridge(self):
        ip = self.bridge_ip_var.get().strip()
        code = self.deck_code_var.get().strip()
        settings = load_settings()
        settings["bridge_on"] = self.bridge_var.get()
        settings["bridge_ip"] = ip
        settings["deck_code"] = code  # the pairing code is a secret; keep this settings file private
        save_settings(settings)
        if not self.bridge_var.get():
            self.manager.stop_bridge()
            self._set_status("Disconnected from the Deck. This PC's virtual controller is back.")
        elif not ip or not code:
            self.bridge_var.set(False)
            self._set_status("Enter the Deck's address and its pairing code to connect.")
        else:
            problem = self.manager.start_bridge(ip, code)
            if problem:
                self.bridge_var.set(False)
                settings["bridge_on"] = False
                save_settings(settings)
                self._set_status(problem)
            else:
                self._set_status(f"Connecting to the Deck at {ip} (encrypted)...")
        self._refresh_virtual_pill()

    def _connect_deck(self):
        self.bridge_var.set(True)
        self._toggle_bridge()

    def _disconnect_deck(self):
        self.bridge_var.set(False)
        self._toggle_bridge()

    def _show_link_state(self):
        state = self.manager.link.state
        if state != getattr(self, "_shown_link", None):
            self._shown_link = state
            labels = {
                "off": "Deck: off",
                "searching": "Deck: searching",
                "connected": "Deck: connected (PC controls off)",
            }
            chips = {
                "connected": (GOOD_CONTAINER, ON_GOOD_CONTAINER),
                "searching": (WARN_CONTAINER, ON_WARN_CONTAINER),
            }
            text = labels.get(state, state)
            self.link_lbl.config(text=text)
            bg, fg = chips.get(state, (SECONDARY_CONTAINER, ON_SECONDARY_CONTAINER))
            self.deck_chip.config(text=text.split(" (")[0], bg=bg, fg=fg)
            self._refresh_virtual_pill()

    def _toggle_auto_scan(self):
        settings = load_settings()
        settings["auto_scan"] = self.auto_scan_var.get()
        save_settings(settings)

    def _set_output(self, label):
        mode = next(k for k, v in OUTPUT_LABELS.items() if v == label)
        self.manager.set_output(mode)
        settings = load_settings()
        settings["output"] = mode
        save_settings(settings)

    def _set_sample(self, name):
        self.manager.rumble_sample = next(i for n, i in VIBRATION_SAMPLES if n == name)

    def _set_speed(self, value):
        self.manager.mouse_speed = float(value)

    def _calibrate_all(self):
        self.manager.left.calibrate()
        self.manager.right.calibrate()
        self._set_status("Calibrated neutral stick centers and gyroscope zero bias.")

    def _close_to_tray(self):
        """The close button hides the window. The app keeps running, with its Joy-Cons still connected."""
        self.withdraw()

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _handle_events(self):
        """Tray and second-copy requests, handled on the window's own thread."""
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            if event == "show":
                self._show_window()
            elif event == "quit":
                self._quit()
                return

    def _quit(self):
        """Really quits: takes the tray icon away, lets go of the Deck link and the Joy-Cons, and closes."""
        if self._quitting:
            return
        self._quitting = True
        if getattr(self, "_tick_job", None) is not None:
            self.after_cancel(self._tick_job)  # no refresh is left waiting for a window that is gone
        if self._tray is not None:
            self._tray.remove()
        self.manager.stop_bridge()
        self.manager.disconnect_all()
        self.destroy()

    def _tick(self):
        self._handle_events()
        if self._quitting:
            return
        self._drain_status()
        self.left_card.refresh()
        self.right_card.refresh()
        driver = self.manager._state_for(self.manager.active_side) if self.manager.active_side else self.manager.right
        mx, my = driver.mouse_raw
        self.mouse_raw_lbl.config(text=f"raw  X {mx:6d}  Y {my:6d}")
        self._show_link_state()
        if IS_LINUX:
            self._refresh_virtual_pill()  # the uhid device appears after start, so keep the label current
        if getattr(self, "_tick_job", None) is not None:
            self.after_cancel(self._tick_job)  # only one refresh is ever waiting
        self._tick_job = self.after(33, self._tick)


if __name__ == "__main__":
    if ask_running_copy_to_show():
        sys.exit(0)  # the copy that is already running shows its window instead
    app = JoyCon2App()
    app.mainloop()
