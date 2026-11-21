"""Joy-Con 2 Studio backend for Decky Loader on the Steam Deck (runs as root).

The Windows app reads the Joy-Cons over Bluetooth and sends their state to this Deck over Wi-Fi.
The link is encrypted with the pairing code shown on this Deck (see joycon2_link.py). The state is shown
to Steam as a virtual DualShock 4 (a UHID HID device, see uhid_pad.py), and Steam's rumble is sent back to the PC.
The plugin also checks GitHub for a newer release and installs it when no PC is connected (see updater.py).
"""

import asyncio
import os
import socket
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
from uhid_pad import UhidGamepad  # noqa: E402
from updater import UpdateManager  # noqa: E402

VERSION = "0.2.18"  # kept in sync by tools/build.py

BRIDGE_PORT = 26761  # the Windows app connects to this UDP port
SCAN_SECONDS = 5.0  # how long "Scan network" listens


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


def settings_base() -> str:
    return SETTINGS_DIR or os.path.expanduser("~/homebrew/settings/joycon2studio")


def pairing_file() -> str:
    return os.path.join(settings_base(), "pairing.txt")


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
            self.pad = UhidGamepad(self.loop, self._on_pad_output, log=log)
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

    def pc_connected(self) -> bool:
        return self.link is not None and self.link.state == "connected"

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
        self.updates = UpdateManager(
            current=VERSION,
            plugin_dir=PLUGIN_DIR,
            settings_dir=settings_base(),
            can_update=lambda: not self.bridge.pc_connected(),  # an update restarts the plugin, so not mid-session
            log=log,
        )

    async def _main(self):
        try:
            await self.bridge.start()
            self.updates.start(asyncio.get_running_loop())
            log.info("Joy-Con 2 Studio started")
        except Exception as exc:
            self.bridge.say(f"Plugin failed to start: {describe(exc)}")
            log.exception("start failed")

    async def _unload(self):
        self.updates.stop()
        await self.bridge.stop()

    async def _uninstall(self):
        pass

    # Called from the Decky frontend
    async def status(self):
        try:
            data = self.bridge.status()
        except Exception as exc:
            data = {
                "version": VERSION, "pad_ok": False, "pad_message": f"status error: {exc}",
                "link_state": "off", "pc_address": "", "pairing_code": "", "deck_address": "",
                "port": BRIDGE_PORT, "scanning": False, "scan_result": [],
                "message": traceback.format_exc()[-400:],
            }
        data["update"] = self.updates.status()
        return data

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

    async def set_auto_update(self, enabled: bool):
        self.updates.set_auto(bool(enabled))
        return True

    async def check_update_now(self):
        self.updates.request_check()
        return True
