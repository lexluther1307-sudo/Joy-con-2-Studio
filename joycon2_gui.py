import asyncio
import collections
import ctypes
import json
import os
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import colorchooser
from bleak import BleakClient, BleakScanner

try:
    import vgamepad as vg
    HAS_VGAMEPAD = True
except Exception:
    HAS_VGAMEPAD = False

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
BRIDGE_KEY = b"JC2BRDG1"                 # must match the Deck plugin
BRIDGE_MAGIC = b"JCB1"
BRIDGE_HDR = struct.Struct("<4s8sBI")   # magic, key, kind (1 = state to the Deck, 2 = rumble to the PC), sequence
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
        self.rumble_sample = 1
        self.left.rumble_request = lambda: self.rumble_test(self.left)
        self.right.rumble_request = lambda: self.rumble_test(self.right)
        self.output = load_settings().get("output", "ds4")
        self.ds4 = None
        self._ds4_failed = False
        self.rumble_large = 0
        self.rumble_small = 0
        self.bridge_sock = None
        self.bridge_addr = None
        self.bridge_seq = 0
        self.bridge_stop = threading.Event()

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

        # Battery is read from the power channel instead (see _battery_sampler).

        # Bytes 4..8: 32-bit button bitfield
        state.buttons = int.from_bytes(data[4:8], byteorder="little")

        # Bytes 10..15: 12-bit analog sticks
        lx, ly = decode_stick(data[10], data[11], data[12])
        rx, ry = decode_stick(data[13], data[14], data[15])

        if state.side == "L" and (lx != 0 or ly != 0):
            state.stick_x, state.stick_y = lx, ly
        elif state.side == "R" and (rx != 0 or ry != 0):
            state.stick_x, state.stick_y = rx, ry

        # Mouse sensor: bytes 16..19, little-endian int16 X and Y (needs feature bit 0x10)
        if len(data) >= 20:
            state.mouse_raw = struct.unpack_from("<hh", data, 16)
            driver = self.right if self.right.connected else self.left
            if state is driver:
                self._move_cursor(state)
                self._mouse_clicks(state)

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

        self._sync_virtual_gamepad()

    def _move_cursor(self, state: JoyConState):
        mx, my = state.mouse_raw
        prev = state.mouse_prev
        state.mouse_prev = (mx, my)
        if prev is None or not self.mouse_enabled:
            return
        # Wrap-around int16 difference, so a counter rollover does not jump the cursor
        dx = ((mx - prev[0] + 32768) % 65536) - 32768
        dy = ((my - prev[1] + 32768) % 65536) - 32768
        state.mouse_acc[0] += dx * self.mouse_speed
        state.mouse_acc[1] += dy * self.mouse_speed
        ix, iy = int(state.mouse_acc[0]), int(state.mouse_acc[1])
        if ix or iy:
            state.mouse_acc[0] -= ix
            state.mouse_acc[1] -= iy
            ctypes.windll.user32.mouse_event(0x0001, ix, iy, 0, 0)  # MOUSEEVENTF_MOVE

    def _mouse_clicks(self, state: JoyConState):
        if not self.mouse_enabled:
            return
        left = bool(state.buttons & BTN_ZR)
        right = bool(state.buttons & BTN_R)
        if left != state.mouse_left:
            ctypes.windll.user32.mouse_event(0x0002 if left else 0x0004, 0, 0, 0, 0)
            state.mouse_left = left
        if right != state.mouse_right:
            ctypes.windll.user32.mouse_event(0x0008 if right else 0x0010, 0, 0, 0, 0)
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
            if HAS_VGAMEPAD and self.gamepad is None:
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

    def start_bridge(self, ip: str):
        self.stop_bridge()
        if not ip:
            return
        self.bridge_addr = (ip, BRIDGE_PORT)
        self.bridge_stop.clear()
        self.bridge_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.bridge_sock.bind(("", 0))
        sock = self.bridge_sock
        threading.Thread(target=self._bridge_send_loop, args=(sock,), daemon=True).start()
        threading.Thread(target=self._bridge_recv_loop, args=(sock,), daemon=True).start()
        self.log(f"Sending the controller to the Deck at {ip}.")

    def stop_bridge(self):
        self.bridge_stop.set()
        sock, self.bridge_sock = self.bridge_sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _bridge_send_loop(self, sock):
        while not self.bridge_stop.is_set():
            try:
                self.bridge_seq = (self.bridge_seq + 1) & 0xFFFFFFFF
                packet = BRIDGE_HDR.pack(BRIDGE_MAGIC, BRIDGE_KEY, 1, self.bridge_seq) + self._bridge_report()
                sock.sendto(packet, self.bridge_addr)
            except OSError:
                pass
            time.sleep(0.01)

    def _bridge_recv_loop(self, sock):
        while not self.bridge_stop.is_set():
            try:
                data, _ = sock.recvfrom(256)
            except OSError:
                break
            if len(data) < BRIDGE_HDR.size + 2:
                continue
            magic, key, kind, _seq = BRIDGE_HDR.unpack_from(data)
            if magic != BRIDGE_MAGIC or key != BRIDGE_KEY or kind != 2:
                continue
            self.rumble_small = data[BRIDGE_HDR.size]
            self.rumble_large = data[BRIDGE_HDR.size + 1]

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

    def _sync_ds4(self):
        ds4 = self._ensure_ds4()
        if ds4 is None:
            return
        D = vg.DS4_BUTTONS
        ds4.reset()
        b = (self.left.buttons if self.left.connected else 0) | (
            self.right.buttons if self.right.connected else 0
        )
        lx = self._norm_axis(self.left.stick_x, self.left.center_x) if self.left.connected else 0
        ly = self._norm_axis(self.left.stick_y, self.left.center_y) if self.left.connected else 0
        rx = self._norm_axis(self.right.stick_x, self.right.center_x) if self.right.connected else 0
        ry = self._norm_axis(self.right.stick_y, self.right.center_y) if self.right.connected else 0
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
        driver = self.right if self.right.connected else (self.left if self.left.connected else None)
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

    def _sync_virtual_gamepad(self):
        if self.output == "ds4":
            self._sync_ds4()
            return
        if not self.gamepad:
            return

        self.gamepad.reset()
        only_one = self.left.connected ^ self.right.connected

        # Single Joy-Con Sideways Mode (horizontal standalone controller)
        if only_one and self.sideways_single:
            jc = self.left if self.left.connected else self.right
            b = jc.buttons
            sx = self._norm_axis(jc.stick_x, jc.center_x)
            sy = self._norm_axis(jc.stick_y, jc.center_y)

            if jc.side == "L":
                self.gamepad.left_joystick(x_value=-sy, y_value=sx)
                if b & BTN_DOWN:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
                if b & BTN_RIGHT:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_B)
                if b & BTN_LEFT:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
                if b & BTN_UP:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)
                if b & BTN_SL_L:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER)
                if b & BTN_SR_L:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER)
                if b & BTN_MINUS:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_START)
                if b & BTN_L3:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)
            else:
                self.gamepad.left_joystick(x_value=sy, y_value=-sx)
                if b & BTN_X:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
                if b & BTN_A:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_B)
                if b & BTN_Y:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
                if b & BTN_B:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)
                if b & BTN_SL_R:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER)
                if b & BTN_SR_R:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER)
                if b & BTN_PLUS:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_START)
                if b & BTN_R3:
                    self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)

            self.gamepad.update()
            return

        # Standard Unified Mode (works with Left only, Right only, or Both merged)
        b = (self.left.buttons if self.left.connected else 0) | (
            self.right.buttons if self.right.connected else 0
        )

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

        lx = self._norm_axis(self.left.stick_x, self.left.center_x) if self.left.connected else 0
        ly = self._norm_axis(self.left.stick_y, self.left.center_y) if self.left.connected else 0
        rx = self._norm_axis(self.right.stick_x, self.right.center_x) if self.right.connected else 0
        ry = self._norm_axis(self.right.stick_y, self.right.center_y) if self.right.connected else 0

        if self.gyro_aim_enabled:
            active_imu = (
                self.right if self.right.connected else (self.left if self.left.connected else None)
            )
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


# ---- Look and feel ----------------------------------------------------------
BG = "#0E1116"
SURFACE = "#161B22"
SURFACE_2 = "#1C232D"
BORDER = "#2A3240"
TEXT = "#E6EDF3"
MUTED = "#8B95A5"
ACCENT = "#3B82F6"
ACCENT_HOVER = "#2563EB"
GOOD = "#22C55E"
GOOD_BG = "#14321F"
WARN = "#F59E0B"
BAD = "#EF4444"
ACTIVE_L = "#38BDF8"
ACTIVE_R = "#F87171"
FONT = "Segoe UI"
MONO = "Consolas"


def make_button(parent, text, command, style="secondary", size=9, padx=12, pady=5):
    bg, hover, fg = {
        "primary": (ACCENT, ACCENT_HOVER, "#FFFFFF"),
        "secondary": (SURFACE_2, "#263040", TEXT),
        "danger": ("#3A1D24", "#4B2630", "#FCA5A5"),
    }[style]
    btn = tk.Button(
        parent,
        text=text,
        command=command,
        font=(FONT, size, "bold" if style == "primary" else "normal"),
        bg=bg,
        fg=fg,
        activebackground=hover,
        activeforeground=fg,
        relief="flat",
        bd=0,
        padx=padx,
        pady=pady,
        cursor="hand2",
    )
    btn.bind("<Enter>", lambda _e: btn.config(bg=hover))
    btn.bind("<Leave>", lambda _e: btn.config(bg=bg))
    return btn


def make_pill(parent, text, fg, bg):
    return tk.Label(parent, text=text, font=(FONT, 8, "bold"), fg=fg, bg=bg, padx=10, pady=3)


def rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


def draw_shell(canvas, s: JoyConState):
    canvas.delete("all")
    if s.side == "L":
        rounded_rect(canvas, 126, 44, 150, 156, 10, fill=s.rail_color, outline="")
        rounded_rect(canvas, 40, 20, 136, 180, 26, fill=s.body_color, outline=BORDER)
        canvas.create_oval(60, 42, 96, 78, fill=s.btn_color, outline="#4B5563")
        for dx, dy, mask in ((0, -14, BTN_UP), (0, 14, BTN_DOWN), (-14, 0, BTN_LEFT), (14, 0, BTN_RIGHT)):
            active = s.connected and (s.buttons & mask)
            x, y = 78 + dx, 130 + dy
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=ACTIVE_L if active else s.btn_color, outline="")
    else:
        rounded_rect(canvas, 30, 44, 54, 156, 10, fill=s.rail_color, outline="")
        rounded_rect(canvas, 44, 20, 140, 180, 26, fill=s.body_color, outline=BORDER)
        for dx, dy, mask in ((0, -14, BTN_X), (0, 14, BTN_B), (-14, 0, BTN_Y), (14, 0, BTN_A)):
            active = s.connected and (s.buttons & mask)
            x, y = 92 + dx, 62 + dy
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=ACTIVE_R if active else s.btn_color, outline="")
        canvas.create_oval(74, 110, 110, 146, fill=s.btn_color, outline="#4B5563")


def draw_stick(canvas, s: JoyConState):
    canvas.delete("all")
    cx, cy, r = 90, 88, 62
    canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=BORDER, width=2)
    canvas.create_oval(cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2, outline=BORDER)
    canvas.create_line(cx - r, cy, cx + r, cy, fill=BORDER)
    canvas.create_line(cx, cy - r, cx, cy + r, fill=BORDER)
    nx = max(-1.0, min(1.0, (s.stick_x - s.center_x) / 1750.0)) if s.connected else 0.0
    ny = max(-1.0, min(1.0, (s.stick_y - s.center_y) / 1750.0)) if s.connected else 0.0
    px = cx + int(nx * (r - 8))
    py = cy - int(ny * (r - 8))
    dot = s.rail_color if s.connected else MUTED
    canvas.create_line(cx, cy, px, py, fill=dot, width=2)
    canvas.create_oval(px - 8, py - 8, px + 8, py + 8, fill=dot, outline=TEXT)
    canvas.create_text(cx, 186, text=f"Stick {s.stick_x:4d}, {s.stick_y:4d}", fill=MUTED, font=(MONO, 8))


class JoyConCard(tk.Frame):
    """One Joy-Con: status, battery, shell preview, stick, colors, and motion readout."""

    def __init__(self, parent, title: str, state: JoyConState):
        super().__init__(parent, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER, bd=0)
        self.state = state
        self.side = state.side
        pad = 16

        header = tk.Frame(self, bg=SURFACE)
        header.pack(fill="x", padx=pad, pady=(14, 6))
        tk.Label(header, text=title, font=(FONT, 11, "bold"), fg=TEXT, bg=SURFACE).pack(side="left")
        self.status_lbl = make_pill(header, "WAITING", MUTED, SURFACE_2)
        self.status_lbl.pack(side="right")

        battery = tk.Frame(self, bg=SURFACE)
        battery.pack(fill="x", padx=pad, pady=(4, 10))
        top = tk.Frame(battery, bg=SURFACE)
        top.pack(fill="x")
        self.bat_text = tk.Label(top, text="Battery --", font=(FONT, 9), fg=TEXT, bg=SURFACE)
        self.bat_text.pack(side="left")
        self.charge_lbl = tk.Label(top, text="", font=(FONT, 9, "bold"), fg=GOOD, bg=SURFACE)
        self.charge_lbl.pack(side="right")
        self.bat_canvas = tk.Canvas(battery, width=120, height=12, bg=SURFACE_2, highlightthickness=0)
        self.bat_canvas.pack(fill="x", pady=(6, 0))

        visuals = tk.Frame(self, bg=SURFACE)
        visuals.pack(fill="x", padx=pad)
        self.shell_canvas = tk.Canvas(visuals, width=180, height=200, bg=SURFACE_2, highlightthickness=0)
        self.shell_canvas.pack(side="left", expand=True)
        self.stick_canvas = tk.Canvas(visuals, width=180, height=200, bg=SURFACE_2, highlightthickness=0)
        self.stick_canvas.pack(side="right", expand=True)

        colors = tk.Frame(self, bg=SURFACE)
        colors.pack(fill="x", padx=pad, pady=(12, 0))
        make_button(colors, "Theme", self._cycle_theme, size=8, padx=8, pady=3).pack(side="left", padx=(0, 4))
        for label, attr in (("Body", "body_color"), ("Buttons", "btn_color"), ("Rail", "rail_color")):
            make_button(
                colors, label, lambda a=attr, t=label: self._pick_color(a, t), size=8, padx=8, pady=3
            ).pack(side="left", padx=2)
        make_button(
            colors, "Test rumble", lambda: self.state.rumble_request(), style="primary", size=8, padx=8, pady=3
        ).pack(side="right")

        motion = tk.Frame(self, bg=SURFACE_2)
        motion.pack(fill="x", padx=pad, pady=(10, 14))
        tk.Label(motion, text="MOTION  (gyro, deg/s)", font=(FONT, 8, "bold"), fg=MUTED, bg=SURFACE_2).pack(
            anchor="w", padx=12, pady=(8, 4)
        )
        grid = tk.Frame(motion, bg=SURFACE_2)
        grid.pack(fill="x", padx=12)
        self.gyro_rows = {}
        for col, axis in enumerate(("X", "Y", "Z")):
            cell = tk.Frame(grid, bg=SURFACE_2)
            cell.pack(side="left", fill="x", expand=True, padx=(0, 10) if col < 2 else (0, 0))
            tk.Label(cell, text=axis, width=2, anchor="w", font=(MONO, 9), fg=TEXT, bg=SURFACE_2).pack(side="left")
            bar = tk.Canvas(cell, width=30, height=8, bg=SURFACE, highlightthickness=0)
            bar.pack(side="left", fill="x", expand=True, padx=(4, 4))
            val = tk.Label(cell, text="+0.0", width=6, anchor="e", font=(MONO, 9), fg=MUTED, bg=SURFACE_2)
            val.pack(side="left")
            self.gyro_rows[axis] = (bar, val)
        self.accel_lbl = tk.Label(
            motion, text="Accel X +0.00  Y +0.00  Z +0.00 g", font=(MONO, 8), fg=MUTED, bg=SURFACE_2
        )
        self.accel_lbl.pack(anchor="w", padx=12, pady=(6, 10))

    def _cycle_theme(self):
        self.state.apply_theme(self.state.theme_idx + 1)

    def _pick_color(self, attr: str, label: str):
        # Display-only: changes the on-screen preview, not the controller's stored colors.
        current = getattr(self.state, attr)
        chosen = colorchooser.askcolor(color=current, title=f"Pick {self.side} Joy-Con {label} color")
        if chosen and chosen[1]:
            setattr(self.state, attr, chosen[1])

    def refresh(self):
        s = self.state
        if s.connected:
            self.status_lbl.config(text="CONNECTED", fg=GOOD, bg=GOOD_BG)
        else:
            self.status_lbl.config(text="WAITING", fg=MUTED, bg=SURFACE_2)

        self.bat_canvas.delete("all")
        w = max(1, self.bat_canvas.winfo_width())
        if s.connected and s.battery_bars > 0:
            self.bat_text.config(text=f"Battery {s.battery_pct}%   (level {s.battery_bars} of 8)")
            fill = int(w * s.battery_pct / 100.0)
            color = GOOD if s.battery_pct > 40 else (WARN if s.battery_pct > 15 else BAD)
            self.bat_canvas.create_rectangle(0, 0, fill, 12, fill=color, width=0)
        elif s.connected:
            self.bat_text.config(text="Battery reading...")
        else:
            self.bat_text.config(text="Battery --")
        self.charge_lbl.config(text="Charging" if (s.connected and s.charging) else "")

        draw_shell(self.shell_canvas, s)
        draw_stick(self.stick_canvas, s)

        for axis, val in (("X", s.gyro_x), ("Y", s.gyro_y), ("Z", s.gyro_z)):
            bar, label = self.gyro_rows[axis]
            label.config(text=f"{val:+6.1f}")
            bar.delete("all")
            bw = max(1, bar.winfo_width())
            mid = bw // 2
            bar.create_line(mid, 0, mid, 8, fill=BORDER)
            if s.connected:
                norm = max(-1.0, min(1.0, val / 250.0))
                end = mid + int(norm * mid)
                bar.create_rectangle(min(mid, end), 1, max(mid, end), 7, fill=ACCENT, width=0)

        self.accel_lbl.config(
            text=f"Accel  X {s.accel_x:+.2f}  Y {s.accel_y:+.2f}  Z {s.accel_z:+.2f} g"
        )


class JoyCon2App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Joy-Con 2 Studio")
        self.geometry("1000x770")
        self.minsize(940, 720)
        self.configure(bg=BG)

        self.status_var = tk.StringVar(value="Hold SYNC on one or two Joy-Cons, then click Scan.")
        self.manager = JoyCon2Manager(self._set_status)

        self._build_header()
        self._build_toolbar()
        self._build_status_bar()
        self._build_tools()
        self._build_cards()
        self._tick()
        if self.auto_scan_var.get():
            self.manager.start_scan(12.0)

    def _set_status(self, msg: str):
        self.after(0, lambda: self.status_var.set(msg))

    def _build_header(self):
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 0))
        tk.Label(head, text="Joy-Con 2 Studio", font=(FONT, 16, "bold"), fg=TEXT, bg=BG).pack(side="left")
        tk.Label(head, text="controller tools", font=(FONT, 10), fg=MUTED, bg=BG).pack(
            side="left", padx=(10, 0), pady=(5, 0)
        )
        if HAS_VGAMEPAD:
            make_pill(head, "Virtual controller: READY", GOOD, GOOD_BG).pack(side="right")
        else:
            make_pill(head, "Virtual controller: MISSING ViGEmBus", WARN, SURFACE_2).pack(side="right")

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(14, 0))
        make_button(
            bar, "Scan for Joy-Cons", lambda: self.manager.start_scan(12.0), style="primary", padx=14, pady=7
        ).pack(side="left")
        make_button(bar, "Zero sticks & gyro", self._calibrate_all, padx=12, pady=7).pack(side="left", padx=8)
        make_button(bar, "Disconnect all", self.manager.disconnect_all, style="danger", padx=12, pady=7).pack(
            side="left"
        )

    def _build_status_bar(self):
        bar = tk.Frame(self, bg=SURFACE, padx=16, pady=7)
        bar.pack(side="bottom", fill="x")
        tk.Label(bar, textvariable=self.status_var, font=(FONT, 9), fg=MUTED, bg=SURFACE).pack(side="left")

    def _build_tools(self):
        panel = tk.Frame(self, bg=SURFACE_2, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(side="bottom", fill="x", padx=16, pady=(0, 10))

        def section(title):
            frame = tk.Frame(panel, bg=SURFACE_2)
            frame.pack(side="left", fill="both", expand=True, padx=18, pady=14)
            tk.Label(frame, text=title, font=(FONT, 8, "bold"), fg=MUTED, bg=SURFACE_2).pack(anchor="w")
            return frame

        def check(parent, text, var, command):
            return tk.Checkbutton(
                parent,
                text=text,
                variable=var,
                command=command,
                font=(FONT, 9),
                fg=TEXT,
                bg=SURFACE_2,
                selectcolor=SURFACE,
                activebackground=SURFACE_2,
                activeforeground=TEXT,
                highlightthickness=0,
            )

        def dropdown(parent, var, choices, command=None):
            menu = tk.OptionMenu(parent, var, *choices, command=command or (lambda _value: None))
            menu.config(
                font=(FONT, 8),
                bg=SURFACE,
                fg=TEXT,
                activebackground=BORDER,
                activeforeground=TEXT,
                highlightthickness=0,
                relief="flat",
                bd=0,
            )
            menu["menu"].config(font=(FONT, 8), bg=SURFACE, fg=TEXT, activebackground=ACCENT, activeforeground="#FFFFFF")
            return menu

        mouse = section("MOUSE")
        self.mouse_var = tk.BooleanVar(value=False)
        check(mouse, "Use the Joy-Con as a mouse", self.mouse_var, self._toggle_options).pack(anchor="w", pady=(6, 0))
        tk.Label(mouse, text="ZR = left click   R = right click", font=(FONT, 8), fg=MUTED, bg=SURFACE_2).pack(
            anchor="w"
        )
        speed_row = tk.Frame(mouse, bg=SURFACE_2)
        speed_row.pack(anchor="w", pady=(8, 0))
        tk.Label(speed_row, text="Speed", font=(FONT, 9), fg=TEXT, bg=SURFACE_2).pack(side="left")
        self.speed_scale = tk.Scale(
            speed_row,
            from_=0.05,
            to=1.5,
            resolution=0.05,
            orient="horizontal",
            length=150,
            showvalue=True,
            command=self._set_speed,
            bg=SURFACE_2,
            fg=TEXT,
            troughcolor=SURFACE,
            activebackground=ACCENT,
            highlightthickness=0,
            bd=0,
        )
        self.speed_scale.set(0.3)
        self.speed_scale.pack(side="left", padx=(8, 0))
        self.mouse_raw_lbl = tk.Label(mouse, text="raw  X 0  Y 0", font=(MONO, 8), fg=MUTED, bg=SURFACE_2)
        self.mouse_raw_lbl.pack(anchor="w", pady=(6, 0))

        sound = section("SOUND")
        sample_row = tk.Frame(sound, bg=SURFACE_2)
        sample_row.pack(anchor="w", pady=(6, 0))
        tk.Label(sample_row, text="Rumble", width=6, anchor="w", font=(FONT, 9), fg=TEXT, bg=SURFACE_2).pack(
            side="left"
        )
        self.sample_var = tk.StringVar(value=VIBRATION_SAMPLES[1][0])
        dropdown(
            sample_row, self.sample_var, [name for name, _ in VIBRATION_SAMPLES[1:]], self._set_sample
        ).pack(side="left")
        song_row = tk.Frame(sound, bg=SURFACE_2)
        song_row.pack(anchor="w", pady=(6, 0))
        tk.Label(song_row, text="Song", width=6, anchor="w", font=(FONT, 9), fg=TEXT, bg=SURFACE_2).pack(side="left")
        self.song_var = tk.StringVar(value=SONGS[0][0])
        dropdown(song_row, self.song_var, [name for name, _ in SONGS]).pack(side="left")
        btn_row = tk.Frame(sound, bg=SURFACE_2)
        btn_row.pack(anchor="w", pady=(10, 0))
        make_button(
            btn_row,
            "Play song",
            lambda: self.manager.play_song(self.song_var.get()),
            style="primary",
            size=8,
            padx=12,
            pady=4,
        ).pack(side="left")
        make_button(btn_row, "Stop", self.manager.stop_vibration, size=8, padx=12, pady=4).pack(
            side="left", padx=(6, 0)
        )

        options = section("OPTIONS")
        output_row = tk.Frame(options, bg=SURFACE_2)
        output_row.pack(anchor="w", pady=(6, 0))
        tk.Label(output_row, text="Output", width=6, anchor="w", font=(FONT, 9), fg=TEXT, bg=SURFACE_2).pack(
            side="left"
        )
        self.output_var = tk.StringVar(value=OUTPUT_LABELS[self.manager.output])
        dropdown(output_row, self.output_var, list(OUTPUT_LABELS.values()), self._set_output).pack(side="left")
        self.sideways_var = tk.BooleanVar(value=False)
        check(options, "1-Joy-Con sideways mode", self.sideways_var, self._toggle_options).pack(
            anchor="w", pady=(6, 0)
        )
        self.gyro_aim_var = tk.BooleanVar(value=False)
        check(options, "Gyro aims the right stick", self.gyro_aim_var, self._toggle_options).pack(anchor="w")
        self.auto_scan_var = tk.BooleanVar(value=load_settings().get("auto_scan", True))
        check(options, "Scan when the app opens", self.auto_scan_var, self._toggle_auto_scan).pack(anchor="w")
        bridge = load_settings()
        self.bridge_var = tk.BooleanVar(value=bool(bridge.get("bridge_on", False)))
        check(options, "Send controller to a Deck (network bridge)", self.bridge_var, self._toggle_bridge).pack(
            anchor="w", pady=(6, 0)
        )
        ip_row = tk.Frame(options, bg=SURFACE_2)
        ip_row.pack(anchor="w")
        tk.Label(ip_row, text="Deck IP", width=8, anchor="w", font=(FONT, 9), fg=TEXT, bg=SURFACE_2).pack(side="left")
        self.bridge_ip_var = tk.StringVar(value=bridge.get("bridge_ip", ""))
        ip_entry = tk.Entry(
            ip_row, textvariable=self.bridge_ip_var, width=16, font=(FONT, 9),
            bg=SURFACE, fg=TEXT, insertbackground=TEXT, relief="flat",
        )
        ip_entry.pack(side="left")
        ip_entry.bind("<Return>", lambda _e: self._toggle_bridge())
        if self.bridge_var.get():
            self.manager.start_bridge(self.bridge_ip_var.get().strip())

    def _build_cards(self):
        row = tk.Frame(self, bg=BG)
        row.pack(fill="both", expand=True, padx=16, pady=(14, 10))
        row.grid_columnconfigure(0, weight=1, uniform="cards")
        row.grid_columnconfigure(1, weight=1, uniform="cards")
        row.grid_rowconfigure(0, weight=1)
        self.left_card = JoyConCard(row, "Left Joy-Con 2", self.manager.left)
        self.left_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.right_card = JoyConCard(row, "Right Joy-Con 2", self.manager.right)
        self.right_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

    def _toggle_options(self):
        self.manager.sideways_single = self.sideways_var.get()
        self.manager.gyro_aim_enabled = self.gyro_aim_var.get()
        self.manager.mouse_enabled = self.mouse_var.get()
        self.manager._sync_virtual_gamepad()

    def _toggle_bridge(self):
        ip = self.bridge_ip_var.get().strip()
        settings = load_settings()
        settings["bridge_on"] = self.bridge_var.get()
        settings["bridge_ip"] = ip
        save_settings(settings)
        if not self.bridge_var.get():
            self.manager.stop_bridge()
            self._set_status("Stopped sending the controller to the Deck.")
        elif not ip:
            self._set_status("Enter the Deck's IP address to start the bridge.")
        else:
            self.manager.start_bridge(ip)
            self._set_status(f"Sending the controller to the Deck at {ip} on UDP port {BRIDGE_PORT}.")

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

    def _tick(self):
        self.left_card.refresh()
        self.right_card.refresh()
        driver = self.manager.right if self.manager.right.connected else self.manager.left
        mx, my = driver.mouse_raw
        self.mouse_raw_lbl.config(text=f"raw  X {mx:6d}  Y {my:6d}")
        self.after(33, self._tick)


if __name__ == "__main__":
    app = JoyCon2App()
    app.mainloop()
